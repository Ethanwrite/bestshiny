"""The medium/low findings from the pre-deploy audit, each pinned by a test.

None of these could take the site down, which is why they were not deploy
blockers. Three of them spend the user's money or lose their work, one turns a
recoverable state into a fifteen-minute wedge, one lets a table grow for the
life of the database, and one turns a re-run into a permanent outage.
"""

from __future__ import annotations

from datetime import timedelta

from memory_core.outbox import MemoryIndexOutboxWorker, MemoryIndexOutboxWriter
from production_domain.models import MemoryIndexOutbox, utcnow
from sqlalchemy import select

# --------------------------------------------------------------------------
# An old renderer degrades to text, not to "[object Object]"
# --------------------------------------------------------------------------


def test_the_screenplay_view_carries_flat_text_for_older_renderers() -> None:
    from creative_director_core.service import _flat_texts

    assert _flat_texts(
        [
            {"text": "the phone is never named", "characters": ["mira"]},
            {"text": "  keep the rain  ", "scenes": []},
        ]
    ) == ["the phone is never named", "keep the rain"]
    # A pre-object screenplay revision is already flat, and stays flat.
    assert _flat_texts(["a plain string"]) == ["a plain string"]
    # Nothing usable is nothing, never a stringified object.
    assert _flat_texts([{"characters": ["mira"]}, {"text": "   "}]) == []
    assert _flat_texts(None) == []
    assert _flat_texts("not a list") == []


# --------------------------------------------------------------------------
# Settled outbox rows do not accumulate for the life of the database
# --------------------------------------------------------------------------


def test_settled_outbox_rows_are_pruned_and_live_ones_are_not(container, project) -> None:  # type: ignore[no-untyped-def]
    writer = MemoryIndexOutboxWriter(container.database)
    worker = MemoryIndexOutboxWorker(container.database, memory=None)  # type: ignore[arg-type]

    for index, status in enumerate(("DONE", "FAILED", "PENDING", "CLAIMED")):
        writer.enqueue(
            project.id,
            idempotency_key=f"retention:{status}:{index}",
            source="VISUAL_BIBLE_LOCK",
            memory_type="STYLE",
            text=status,
        )
    old = utcnow() - timedelta(days=90)
    with container.database.session() as session:
        for row in session.scalars(select(MemoryIndexOutbox)):
            row.status = row.payload_json["text"]
            row.updated_at = old
        session.flush()

    assert worker.prune(older_than_days=30) == 2
    with container.database.session() as session:
        left = {row.status for row in session.scalars(select(MemoryIndexOutbox))}
        assert left == {"PENDING", "CLAIMED"}, "unsettled work must never be pruned"

    # A settled row inside the window stays; retention 0 disables pruning.
    assert worker.prune(older_than_days=30) == 0
    assert worker.prune(older_than_days=0) == 0


def test_a_recent_settled_row_survives_the_prune(container, project) -> None:  # type: ignore[no-untyped-def]
    writer = MemoryIndexOutboxWriter(container.database)
    worker = MemoryIndexOutboxWorker(container.database, memory=None)  # type: ignore[arg-type]
    writer.enqueue(
        project.id,
        idempotency_key="retention:recent",
        source="CANDIDATE_COMMIT",
        memory_type="SHOT_RESULT",
        text="indexed an hour ago",
    )
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox))
        row.status = "DONE"
        row.updated_at = utcnow() - timedelta(hours=1)
        session.flush()

    assert worker.prune(older_than_days=30) == 0
    with container.database.session() as session:
        assert session.scalar(select(MemoryIndexOutbox)) is not None


# --------------------------------------------------------------------------
# A dead process's lock step can be released without waiting out the lease
# --------------------------------------------------------------------------


def test_an_operator_can_release_a_lock_step_a_dead_process_left_running(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise a deploy that kills the api wedges a bible for 15 minutes.

    Releasing is safe rather than merely convenient: each step's `discover()`
    re-reads the Canon it would create before creating any, so a released step
    resumes instead of duplicating an identity or an asset version.
    """

    from fastapi.testclient import TestClient
    from production_domain.models import (
        CreativeBriefRevision,
        CreativeLockStep,
        CreativeSession,
        VisualBibleVersion,
    )
    from video_platform_api.main import create_app

    with container.database.session() as session:
        row = CreativeSession(
            project_id=project.id,
            title="wedged by a deploy",
            status="BIBLE_PROPOSED",
            format="SHORT_DRAMA",
        )
        session.add(row)
        session.flush()
        brief = CreativeBriefRevision(
            session_id=row.id,
            revision=1,
            status="APPROVED",
            fields_json={},
            completeness_json={},
            provenance_json={},
            question_state_json={},
            content_hash="a" * 64,
        )
        session.add(brief)
        session.flush()
        bible = VisualBibleVersion(
            session_id=row.id,
            project_id=project.id,
            version=1,
            status="DRAFT",
            brief_id=brief.id,
            content_json={},
            content_hash="f" * 64,
        )
        session.add(bible)
        session.flush()
        session.add(
            CreativeLockStep(
                session_id=row.id,
                bible_id=bible.id,
                step_kind="IDENTITY",
                step_key="character:mira",
                idempotency_key="wedged:identity:mira",
                status="RUNNING",
                attempts=1,
                claimed_at=utcnow() - timedelta(minutes=5),
            )
        )
        session.add(
            CreativeLockStep(
                session_id=row.id,
                bible_id=bible.id,
                step_kind="IDENTITY",
                step_key="character:jun",
                idempotency_key="live:identity:jun",
                status="RUNNING",
                attempts=1,
                claimed_at=utcnow(),
            )
        )
        session.flush()

    headers = {"Authorization": f"Bearer {container.settings.platform_api_key}"}
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/internal/maintenance/release-lock-steps?older_than_seconds=60", headers=headers
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["released"] == 1, "a step claimed seconds ago is still running"
    assert body["steps"][0]["step_key"] == "character:mira"

    with container.database.session() as session:
        states = {
            row.step_key: (row.status, row.claimed_at)
            for row in session.scalars(select(CreativeLockStep))
        }
    assert states["character:mira"][0] == "PENDING"
    assert states["character:mira"][1] is None
    assert states["character:jun"][0] == "RUNNING", "a live claim must not be yanked"


# --------------------------------------------------------------------------
# A re-applied migration is skipped, not turned into a permanent outage
# --------------------------------------------------------------------------


def test_a_new_table_that_already_exists_skips_instead_of_crash_looping(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The api's start command is `alembic upgrade head && uvicorn`.

    Raising here meant the container never reached uvicorn and restarted for
    ever with no health endpoint - a permanent outage needing an SSH session,
    triggered by anything that created the object outside alembic. The desired
    schema state already holds, so the migration skips and says so.
    """

    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'already-there.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    command.upgrade(config, "0076_character_evidence_coverage")
    # Something outside alembic creates 0077's table: a `create_all`, a manual
    # fix, or a restore from a snapshot taken mid-migration.
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE memory_index_outbox (id TEXT PRIMARY KEY)"))
    engine.dispose()

    # This used to raise and take the api down with it.
    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            head = connection.execute(sa.text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()
    assert head is not None and head.startswith("0079")


# --------------------------------------------------------------------------
# A paid turn that lost a race is kept, not thrown away
# --------------------------------------------------------------------------


def test_a_turn_that_loses_the_stage_race_is_recorded_rather_than_discarded(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    """The model call was billed and the user's words were theirs.

    Phase 3 refuses to write the *brief* when the session left dialogue while
    the director was thinking - correctly, since that would supersede an
    APPROVED revision. It used to discard the whole exchange with it.
    """

    from creative_director_core.service import _TurnReasoning
    from production_domain.models import CreativeSession, CreativeTurn

    with container.database.session() as session:
        row = CreativeSession(
            project_id=project.id,
            title="raced by an approval",
            status="BRIEF_APPROVED",
            format="SHORT_DRAMA",
        )
        session.add(row)
        session.flush()
        session_id = row.id

    reasoning = _TurnReasoning(
        result=None,
        reasoner="MODEL:test",
        reason_codes=[],
        audit={"prompt": "…"},
        execution_record_id=None,
        retryable=False,
        skill_version="v1",
        skill_content_hash="e" * 64,
        fallback_message="I had already started answering when the brief was approved.",
    )
    container.creative_director._record_superseded_turn(
        session_id,
        content="one more thing about the ending",
        client_turn_id="turn-abc",
        reasoning=reasoning,
        status_before="BRIEF_PROPOSED",
        status_now="BRIEF_APPROVED",
    )

    with container.database.session() as session:
        turns = list(
            session.scalars(
                select(CreativeTurn)
                .where(CreativeTurn.session_id == session_id)
                .order_by(CreativeTurn.sequence)
            )
        )
        row = session.get(CreativeSession, session_id)

    assert [turn.speaker for turn in turns] == ["USER", "DIRECTOR"]
    assert turns[0].content == "one more thing about the ending"
    assert turns[1].content.startswith("I had already started answering")
    assert turns[1].result_json["superseded"] is True
    assert turns[1].result_json["applied"] is False
    assert turns[1].result_json["status_now"] == "BRIEF_APPROVED"
    # The brief is exactly where the approval left it.
    assert row.status == "BRIEF_APPROVED"
    assert all(turn.brief_revision in (None, 0) for turn in turns), "no brief revision was claimed"


def test_recording_a_superseded_turn_never_turns_a_409_into_a_500(container) -> None:  # type: ignore[no-untyped-def]
    """History is better-than-nothing work; failing to write it is not fatal."""

    from creative_director_core.service import _TurnReasoning

    reasoning = _TurnReasoning(
        result=None,
        reasoner="MODEL:test",
        reason_codes=[],
        audit={},
        execution_record_id=None,
        retryable=False,
        skill_version=None,
        skill_content_hash=None,
        fallback_message="…",
    )
    # No such session: the insert violates the foreign key and is swallowed.
    container.creative_director._record_superseded_turn(
        "00000000-0000-0000-0000-000000000000",
        content="lost to a race",
        client_turn_id=None,
        reasoning=reasoning,
        status_before="BRIEF_PROPOSED",
        status_now="COMPILED",
    )
