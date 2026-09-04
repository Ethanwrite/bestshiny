"""Sessions that were mid-flight when 0070 landed can move again.

0070 added ``current_screenplay_revision`` with a server default of 0 and
inserted no screenplay rows, while the service it shipped with requires an
APPROVED screenplay from VISUALS_IN_PROGRESS onwards. A session that was at
VISUALS_IN_PROGRESS, BIBLE_PROPOSED, BIBLE_LOCKED or BEATS_PROPOSED therefore
answered 409 SCREENPLAY_NOT_APPROVED for ever, with no backward transition out.

These tests build a real 0069-era database - the shape production was actually
running the day before 0070 - upgrade it to head, and assert the recovery:
nothing deleted, no screenplay invented, an explicit stage the user can act
from, and key visuals re-bound rather than re-charged.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
STRANDED = ("VISUALS_IN_PROGRESS", "BIBLE_PROPOSED", "BIBLE_LOCKED", "BEATS_PROPOSED")


def _config(database_url: str, monkeypatch) -> Config:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _filled(table: sa.Table, values: dict) -> dict:  # type: ignore[type-arg]
    """`values` plus an empty value for every other NOT NULL column.

    The seed describes only what the test is about; columns the 0069 schema
    happens to require but this scenario does not care about get a neutral
    value, so the fixture does not have to track unrelated schema evolution.
    """

    filled = dict(values)
    for column in table.columns:
        if column.name in filled or column.nullable or column.default is not None:
            continue
        if column.server_default is not None or column.primary_key:
            continue
        python_type = getattr(column.type, "python_type", str)
        try:
            filled[column.name] = python_type()
        except (NotImplementedError, TypeError):
            filled[column.name] = ""
    return filled


def _seed_0069_sessions(engine, *, statuses=STRANDED, compiled: bool = False):  # type: ignore[no-untyped-def]
    """A pre-0070 creative director database, written through reflection.

    The ORM cannot be used here: at 0069 the post-0070 columns do not exist.
    """

    now = datetime.now(UTC)
    metadata = sa.MetaData()
    with engine.begin() as connection:
        tables = {
            name: sa.Table(name, metadata, autoload_with=connection)
            for name in (
                "projects",
                "creative_sessions",
                "creative_briefs",
                "creative_turns",
                "creative_visual_anchors",
                "visual_bibles",
                "episodes",
            )
        }

        def insert(name: str, **values):  # type: ignore[no-untyped-def]
            table = tables[name]
            # Columns a later migration adds do not exist at 0069; the seed
            # describes the whole shape and this drops what this schema
            # predates, so the fixture reads as one story.
            known = {key: value for key, value in values.items() if key in table.c}
            connection.execute(table.insert().values(_filled(table, known)))

        project_id = str(uuid.uuid4())
        insert("projects", id=project_id, title="Legacy project", created_at=now, updated_at=now)
        episode_id = None
        if compiled:
            episode_id = str(uuid.uuid4())
            insert(
                "episodes",
                id=episode_id,
                project_id=project_id,
                title="EP01",
                episode_number=1,
                created_at=now,
                updated_at=now,
            )
        made: dict[str, dict] = {}
        for index, status in enumerate(statuses, 1):
            session_id = str(uuid.uuid4())
            brief_id = str(uuid.uuid4())
            anchor_id = str(uuid.uuid4())
            locked = status in {"BIBLE_LOCKED", "BEATS_PROPOSED"}
            insert(
                "creative_sessions",
                id=session_id,
                project_id=project_id,
                title=f"Legacy {status}",
                status=status,
                format="SHORT_DRAMA",
                current_brief_revision=2,
                current_bible_version=1,
                current_beat_revision=1,
                compiled_episode_id=episode_id,
                created_at=now,
                updated_at=now,
            )
            insert(
                "creative_briefs",
                id=brief_id,
                session_id=session_id,
                revision=2,
                status="APPROVED",
                fields_json={"format": "SHORT_DRAMA", "logline": "a legacy piece"},
                completeness_json={},
                content_hash="a" * 64,
                provenance_json={},
                question_state_json={},
                approved_at=now,
                created_at=now,
                updated_at=now,
            )
            insert(
                "creative_turns",
                id=str(uuid.uuid4()),
                session_id=session_id,
                sequence=1,
                speaker="USER",
                content="make me a legacy short",
                questions_json=[],
                extracted_json={},
                reasoner="USER",
                reason_codes=[],
                brief_revision=1,
                created_at=now,
                updated_at=now,
            )
            insert(
                "creative_visual_anchors",
                id=anchor_id,
                session_id=session_id,
                anchor_key="character:mira",
                kind="CHARACTER",
                title="Mira",
                prompt_json={"subject": "Mira", "look": "black coat"},
                required=True,
                status="READY",
                created_at=now,
                updated_at=now,
            )
            insert(
                "visual_bibles",
                id=str(uuid.uuid4()),
                session_id=session_id,
                project_id=project_id,
                version=1,
                status="LOCKED" if locked else "DRAFT",
                brief_id=brief_id,
                content_json={},
                content_hash="b" * 64,
                locked_at=now if locked else None,
                created_at=now,
                updated_at=now,
            )
            made[status] = {
                "session_id": session_id,
                "brief_id": brief_id,
                "anchor_id": anchor_id,
                "index": index,
            }
        return project_id, made


def _upgraded(tmp_path, monkeypatch, name: str, **seed):  # type: ignore[no-untyped-def]
    database_url = f"sqlite:///{tmp_path / name}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0069_production_budget")
    engine = sa.create_engine(database_url)
    project_id, made = _seed_0069_sessions(engine, **seed)
    engine.dispose()
    command.upgrade(config, "head")
    return database_url, config, project_id, made


def _rows(database_url: str, table: str, **where):  # type: ignore[no-untyped-def]
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    with engine.connect() as connection:
        target = sa.Table(table, metadata, autoload_with=connection)
        query = sa.select(target)
        for column, value in where.items():
            query = query.where(target.c[column] == value)
        result = [dict(row._mapping) for row in connection.execute(query)]
    engine.dispose()
    return result


def test_a_stranded_legacy_session_lands_in_a_stage_it_can_act_from(tmp_path, monkeypatch) -> None:
    database_url, _config_, _project, made = _upgraded(
        tmp_path, monkeypatch, "legacy-recovery.db"
    )
    for status, seeded in made.items():
        session_row = _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]
        # Recovered to the one stage the director can write a screenplay from.
        assert session_row["status"] == "BRIEF_APPROVED", (status, session_row["status"])
        assert session_row["current_screenplay_revision"] == 0
        # And no screenplay was invented on the user's behalf.
        assert _rows(database_url, "creative_screenplays", session_id=seeded["session_id"]) == []
        # The recovery is on record, with the stage it came from.
        turns = _rows(database_url, "creative_turns", session_id=seeded["session_id"])
        recovery = [turn for turn in turns if turn["reasoner"] == "MIGRATION"]
        assert len(recovery) == 1
        assert recovery[0]["reason_codes"] == ["LEGACY_SESSION_RECOVERED"]
        assert recovery[0]["context_json"]["recovered_from_status"] == status
        assert recovery[0]["speaker"] == "DIRECTOR"
        # Nothing was deleted: the user's own turn, brief, anchor and bible stay.
        assert any(turn["speaker"] == "USER" for turn in turns)
        assert _rows(database_url, "creative_briefs", session_id=seeded["session_id"])
        assert _rows(database_url, "creative_visual_anchors", session_id=seeded["session_id"])
        assert _rows(database_url, "visual_bibles", session_id=seeded["session_id"])


def test_legacy_key_visuals_are_rebound_by_hash_not_recharged(tmp_path, monkeypatch) -> None:
    """0070 left prompt_hash empty, which would supersede and re-bill every anchor."""

    database_url, _config_, _project, made = _upgraded(tmp_path, monkeypatch, "legacy-anchors.db")
    seeded = made["VISUALS_IN_PROGRESS"]
    anchor = _rows(database_url, "creative_visual_anchors", id=seeded["anchor_id"])[0]
    assert len(anchor["prompt_hash"]) == 64
    # It is the hash the service itself would compute from the stored prompt,
    # so an unchanged depiction is re-used rather than regenerated.
    from creative_director_core.schemas import ANCHOR_PROMPT_VERSION

    expected = hashlib.sha256(
        json.dumps(
            {"version": ANCHOR_PROMPT_VERSION, **anchor["prompt_json"]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert anchor["prompt_hash"] == expected


def test_a_session_that_already_compiled_is_left_completely_alone(tmp_path, monkeypatch) -> None:
    database_url, _config_, _project, made = _upgraded(
        tmp_path, monkeypatch, "legacy-compiled.db", statuses=("BEATS_PROPOSED",), compiled=True
    )
    seeded = made["BEATS_PROPOSED"]
    session_row = _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]
    assert session_row["status"] == "BEATS_PROPOSED"
    assert session_row["compiled_episode_id"]
    turns = _rows(database_url, "creative_turns", session_id=seeded["session_id"])
    assert [turn for turn in turns if turn["reasoner"] == "MIGRATION"] == []


def test_the_recovery_is_reversible_and_a_fresh_database_is_untouched(
    tmp_path, monkeypatch
) -> None:
    database_url, config, _project, made = _upgraded(tmp_path, monkeypatch, "legacy-down.db")
    command.downgrade(config, "0074_creative_lock_steps")
    for status, seeded in made.items():
        session_row = _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]
        assert session_row["status"] == status
        # The recovery turn is dialogue history and stays in both directions.
        turns = _rows(database_url, "creative_turns", session_id=seeded["session_id"])
        assert [turn for turn in turns if turn["reasoner"] == "MIGRATION"]
    command.upgrade(config, "head")
    for seeded in made.values():
        assert (
            _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]["status"]
            == "BRIEF_APPROVED"
        )
        # Re-running the recovery does not append a second recovery turn.
        turns = _rows(database_url, "creative_turns", session_id=seeded["session_id"])
        assert len([turn for turn in turns if turn["reasoner"] == "MIGRATION"]) == 1


def test_an_empty_database_upgrades_to_head_unchanged(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "head")
    command.check(config)
    assert _rows(database_url, "creative_sessions") == []


@pytest.mark.postgres_only
def test_the_recovery_runs_on_postgresql_too(postgres_test_database, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The matrix's other half: the same repair against the real engine.

    A throwaway *database* rather than the usual throwaway schema: alembic's
    config reads the URL through configparser, which would try to interpolate
    the ``%3D`` of a search_path option.
    """

    name = "video_platform_mig_" + uuid.uuid4().hex[:16]
    server = sa.engine.make_url(postgres_test_database).set(database="postgres")
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    database_url = sa.engine.make_url(postgres_test_database).set(database=name).render_as_string(
        hide_password=False
    )
    try:
        with sa.create_engine(database_url, isolation_level="AUTOCOMMIT").connect() as connection:
            connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        config = _config(database_url, monkeypatch)
        command.upgrade(config, "0069_production_budget")
        engine = sa.create_engine(database_url)
        _project, made = _seed_0069_sessions(engine)
        engine.dispose()
        command.upgrade(config, "head")
        for status, seeded in made.items():
            row = _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]
            assert row["status"] == "BRIEF_APPROVED", status
            assert _rows(database_url, "creative_screenplays", session_id=seeded["session_id"]) == []
            anchor = _rows(database_url, "creative_visual_anchors", id=seeded["anchor_id"])[0]
            assert len(anchor["prompt_hash"]) == 64
        command.downgrade(config, "0074_creative_lock_steps")
        for status, seeded in made.items():
            assert (
                _rows(database_url, "creative_sessions", id=seeded["session_id"])[0]["status"]
                == status
            )
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"
                ),
                {"name": name},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
