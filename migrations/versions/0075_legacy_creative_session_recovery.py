"""Give the sessions 0070 stranded a way forward again.

0070 added ``creative_sessions.current_screenplay_revision`` with a server
default of 0 and inserted no screenplay rows, so every session that already
existed landed at revision 0 with no ``creative_screenplays`` row at all. The
service it shipped alongside requires an APPROVED screenplay from
VISUALS_IN_PROGRESS onwards, so a session that was mid-flight when 0070 was
applied answers 409 SCREENPLAY_NOT_APPROVED to ``bible/propose``,
``beats/propose`` and ``beats/approve`` for ever, and there is no backward
transition out of those stages: ``propose_screenplay`` accepts only
BRIEF_APPROVED or SCREENPLAY_PROPOSED. Production ran at 0069 the day before
0070 was applied, so this is not hypothetical.

The repair is deliberately conservative:

* Nothing is deleted. Every turn, brief, anchor, visual bible, beat and paid
  generation job stays exactly as it is.
* No screenplay is fabricated. Writing a machine-authored screenplay and
  marking it APPROVED would put words in the user's mouth on the one artefact
  the product treats as their approval, so this migration writes no
  ``creative_screenplays`` row at all.
* Each stranded session is rewound to BRIEF_APPROVED - the one stage from
  which the director can actually write the missing screenplay - and a
  DIRECTOR turn is appended saying what happened and which stage it came
  from, so the state is explicit, recoverable and on record rather than
  silently different.
* A session that has already compiled an episode is left completely alone: its
  source is history and must not be rewritten.
* Legacy anchors were given ``prompt_hash = ''`` by 0070, which is not the
  hash of anything. Their hash is backfilled with the same formula the service
  uses over the prompt they actually recorded, so ``_derive_anchors`` can
  compare like with like: an anchor whose recorded depiction still hashes to
  what the current derivation produces is *re-bound* rather than regenerated.
  That is true for anchors written under the current ``creative-anchor-v2``
  prompt - the sessions 0070 stranded for any reason other than age. An anchor
  written under the older prompt shape describes a different depiction under
  today's schema, so it gets a new version and the old row is kept as history:
  the deliberate "otherwise" branch, never a silent overwrite.

The downgrade restores each session's original stage from the recovery turn it
wrote, and leaves the turn itself in place - it is dialogue history.

Revision ID: 0075_legacy_creative_session_recovery
Revises: 0074_creative_lock_steps
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0075_legacy_creative_session_recovery"
down_revision: str | None = "0074_creative_lock_steps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Stages that require an APPROVED screenplay under the post-0070 service and
#: therefore cannot be left at screenplay revision 0.
STRANDED_STATUSES = (
    "SCREENPLAY_PROPOSED",
    "SCREENPLAY_APPROVED",
    "VISUALS_IN_PROGRESS",
    "BIBLE_PROPOSED",
    "BIBLE_LOCKED",
    "BEATS_PROPOSED",
)
#: The one stage a stranded session can move to and act from: the brief is
#: already approved, so the director can write the screenplay that is missing.
RECOVERY_STATUS = "BRIEF_APPROVED"
RECOVERY_REASON = "LEGACY_SESSION_RECOVERED"
RECOVERY_MESSAGE = (
    "This conversation started before the screenplay stage existed, so it has no approved "
    "screenplay. Nothing has been lost - your brief, your key visuals and your visual bible "
    "are all still here. Ask me to write the screenplay from your approved brief, approve it, "
    "and we carry straight on. A key visual is re-used when the new screenplay describes it the "
    "same way; where the description changes it is generated again, and the old one is kept."
)

#: Mirrors creative_director_core.schemas.ANCHOR_PROMPT_VERSION and
#: AnchorSpec.prompt_hash. Duplicated here on purpose: a migration must not
#: import application code, because the application it runs against is the one
#: being upgraded. If the anchor prompt version ever changes, this constant
#: stays pinned to the version these legacy rows were written under.
ANCHOR_PROMPT_VERSION = "creative-anchor-v2"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _anchor_prompt_hash(prompt: dict) -> str:  # type: ignore[type-arg]
    encoded = json.dumps(
        {"version": ANCHOR_PROMPT_VERSION, **prompt},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    tables = _tables()
    if "creative_sessions" not in tables or "creative_screenplays" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    sessions = sa.Table("creative_sessions", metadata, autoload_with=connection)
    turns = sa.Table("creative_turns", metadata, autoload_with=connection)
    screenplays = sa.Table("creative_screenplays", metadata, autoload_with=connection)
    anchors = sa.Table("creative_visual_anchors", metadata, autoload_with=connection)

    briefs = sa.Table("creative_briefs", metadata, autoload_with=connection)

    stranded = connection.execute(
        sa.select(sessions.c.id, sessions.c.status).where(
            sessions.c.current_screenplay_revision == 0,
            sessions.c.status.in_(STRANDED_STATUSES),
            sessions.c.compiled_episode_id.is_(None),
            # A stage literally named BRIEF_APPROVED must not be written onto a
            # session that has no approved brief. Every consumer of that stage
            # calls ``_approved_brief`` and answers 409 BRIEF_NOT_APPROVED
            # without one - including ``propose_screenplay``, the single action
            # this recovery exists to enable. Such a session would be
            # relabelled as approved, told in writing that it had been
            # recovered, and still be just as dead.
            sa.exists(
                sa.select(briefs.c.id).where(
                    briefs.c.session_id == sessions.c.id,
                    briefs.c.status == "APPROVED",
                )
            ),
        )
    ).all()
    now = datetime.now(UTC)
    for session_id, status in stranded:
        has_screenplay = connection.execute(
            sa.select(sa.func.count())
            .select_from(screenplays)
            .where(screenplays.c.session_id == session_id)
        ).scalar()
        if has_screenplay:
            # Not a 0070 casualty: it has screenplay rows and a head pointer
            # that disagrees with them. Leave it for a human.
            continue
        already = connection.execute(
            sa.select(sa.func.count())
            .select_from(turns)
            .where(
                turns.c.session_id == session_id,
                turns.c.reasoner == "MIGRATION",
                # Keyed on *this* revision, the way the downgrade below already
                # filters. On ``reasoner`` alone, the next data migration to
                # reuse the MIGRATION marker would make this one silently skip
                # writing its own recovery turn while still rewinding the
                # stage: a stage change with no record of where it came from,
                # and nothing left for the downgrade to restore.
                turns.c.context_json["migration"].as_string() == revision,
            )
        ).scalar()
        if already:
            # Recovered once already (a downgrade/upgrade round trip). Move the
            # stage back, but never append a second recovery turn.
            connection.execute(
                sessions.update()
                .where(sessions.c.id == session_id)
                .values(status=RECOVERY_STATUS, updated_at=now)
            )
            continue
        next_sequence = int(
            connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(turns.c.sequence), 0)).where(
                    turns.c.session_id == session_id
                )
            ).scalar()
            or 0
        ) + 1
        connection.execute(
            turns.insert().values(
                id=str(uuid.uuid4()),
                session_id=session_id,
                sequence=next_sequence,
                speaker="DIRECTOR",
                content=RECOVERY_MESSAGE,
                questions_json=[],
                extracted_json=[],
                reasoner="MIGRATION",
                reason_codes=[RECOVERY_REASON],
                brief_revision=0,
                # The stage this session came from, so the downgrade can put it
                # back and a human can see exactly what was changed.
                context_json={
                    "migration": revision,
                    "recovered_from_status": status,
                    "recovered_at": now.isoformat(),
                },
                result_json={},
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sessions.update()
            .where(sessions.c.id == session_id)
            .values(status=RECOVERY_STATUS, updated_at=now)
        )

    # Legacy anchors carry an empty prompt hash, which would supersede every one
    # of them on the next derivation and re-charge the user. Give them the hash
    # their own recorded prompt produces, so an unchanged depiction is re-bound.
    #
    # Scoped to sessions that can still act. An earlier revision of this
    # migration selected every empty-hash anchor in the database, which reached
    # sessions this migration promises to leave completely alone - on
    # bestshiny.com it rewrote three anchors belonging to a COMPILED session.
    # The value it wrote there was harmless (a compiled session re-derives
    # nothing) and it is not undone here, because rewriting it back to '' would
    # be a second unasked-for edit to the same rows. New databases get the
    # scoped behaviour the docstring describes.
    for anchor_id, prompt_json in connection.execute(
        sa.select(anchors.c.id, anchors.c.prompt_json)
        .select_from(
            anchors.join(sessions, sessions.c.id == anchors.c.session_id)
        )
        .where(
            sa.or_(anchors.c.prompt_hash == "", anchors.c.prompt_hash.is_(None)),
            sessions.c.compiled_episode_id.is_(None),
        )
    ).all():
        prompt = prompt_json if isinstance(prompt_json, dict) else {}
        connection.execute(
            anchors.update()
            .where(anchors.c.id == anchor_id)
            .values(prompt_hash=_anchor_prompt_hash(prompt))
        )


def downgrade() -> None:
    tables = _tables()
    if "creative_sessions" not in tables or "creative_turns" not in tables:
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    sessions = sa.Table("creative_sessions", metadata, autoload_with=connection)
    turns = sa.Table("creative_turns", metadata, autoload_with=connection)
    # Put each recovered session back at the stage it was found in. The
    # recovery turn itself stays: it is dialogue history, and this migration
    # does not delete history in either direction.
    for context, session_id in connection.execute(
        sa.select(turns.c.context_json, turns.c.session_id).where(turns.c.reasoner == "MIGRATION")
    ).all():
        payload = context if isinstance(context, dict) else {}
        if payload.get("migration") != revision:
            continue
        original = payload.get("recovered_from_status")
        if not original:
            continue
        connection.execute(
            sessions.update()
            .where(sessions.c.id == session_id, sessions.c.status == RECOVERY_STATUS)
            .values(status=original)
        )
