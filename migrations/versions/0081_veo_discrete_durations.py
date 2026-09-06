"""Veo's discrete durations become a routing fact on existing profiles.

Google publishes ``durationSeconds`` for Veo 3.1 as the discrete set
``[4, 6, 8]``. The registry recorded that set only as inert
``metadata_json.supported_durations_seconds`` on the model definition, with a
note that the capability profile could hold nothing but ``min_duration`` /
``max_duration`` - so a 5-second request was admitted, quoted and reserved,
then refused upstream.

The router now reads ``provider_metadata.supported_durations`` from the
capability profile (``model_registry_core.duration``): a request between the
steps runs at the next step up, and one over the ceiling is rejected before a
reservation exists, with the SPLIT_SHOT plan that would fit. Catalogue
defaults seed new databases only; this migration writes the declaration onto
the three OpenRouter Veo profiles a deployed database already holds, and
retires the "gap" note on the definition. Rows that do not exist are skipped;
nothing else is touched.

Revision ID: 0081_veo_discrete_durations
Revises: 0080_creative_turn_claims
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0081_veo_discrete_durations"
down_revision: str | None = "0080_creative_turn_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOGICAL_NAMES = (
    "veo-3.1-openrouter",
    "veo-3.1-fast-openrouter",
    "veo-3.1-lite-openrouter",
)
SUPPORTED_DURATIONS = [4, 6, 8]
GAP_KEY = "duration_admission_gap"
NOTE_KEY = "duration_admission_note"
GAP_TEXT = (
    "Google publishes durationSeconds as the discrete set [4, 6, 8]; the capability "
    "profile holds only min/max, so intermediate values are quotable here and refused "
    "upstream. Expressing a discrete set needs a schema change and is not done here."
)
NOTE_TEXT = (
    "Google publishes durationSeconds as the discrete set [4, 6, 8]. The capability "
    "profile declares it as provider_metadata.supported_durations, so the router runs a "
    "request between the steps at the next step up and refuses one over the ceiling "
    "before a reservation exists (migration 0081 wrote it onto existing rows)."
)


def _tables() -> tuple[sa.Table, sa.Table] | None:
    connection = op.get_bind()
    names = set(sa.inspect(connection).get_table_names())
    if not {"model_definitions", "model_capability_profiles"} <= names:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return None
    metadata = sa.MetaData()
    return (
        sa.Table("model_definitions", metadata, autoload_with=connection),
        sa.Table("model_capability_profiles", metadata, autoload_with=connection),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes)):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _json_value(column: sa.Column, value: dict[str, Any]) -> Any:
    # Reflected as JSON on both engines this platform runs; a TEXT reflection
    # (an old hand-made schema) still gets valid JSON text.
    return value if isinstance(column.type, sa.JSON) else json.dumps(value, ensure_ascii=False)


def _rewrite(profile_metadata: Any, definition_metadata: Any, *, upgrade: bool) -> None:
    tables = _tables()
    if tables is None:
        return
    definitions, profiles = tables
    connection = op.get_bind()
    for logical_name in LOGICAL_NAMES:
        definition = connection.execute(
            sa.select(definitions.c.id, definitions.c.metadata_json).where(
                definitions.c.logical_name == logical_name
            )
        ).first()
        if definition is None:
            continue
        profile = connection.execute(
            sa.select(profiles.c.provider_metadata).where(
                profiles.c.model_definition_id == definition.id
            )
        ).first()
        if profile is not None:
            declared = profile_metadata(_as_dict(profile.provider_metadata))
            connection.execute(
                profiles.update()
                .where(profiles.c.model_definition_id == definition.id)
                .values(provider_metadata=_json_value(profiles.c.provider_metadata, declared))
            )
        recorded = definition_metadata(_as_dict(definition.metadata_json))
        connection.execute(
            definitions.update()
            .where(definitions.c.id == definition.id)
            .values(metadata_json=_json_value(definitions.c.metadata_json, recorded))
        )


def upgrade() -> None:
    def profile_metadata(current: dict[str, Any]) -> dict[str, Any]:
        return {**current, "supported_durations": list(SUPPORTED_DURATIONS)}

    def definition_metadata(current: dict[str, Any]) -> dict[str, Any]:
        if GAP_KEY not in current:
            return current
        rewritten = dict(current)
        rewritten.pop(GAP_KEY)
        rewritten[NOTE_KEY] = NOTE_TEXT
        return rewritten

    _rewrite(profile_metadata, definition_metadata, upgrade=True)


def downgrade() -> None:
    def profile_metadata(current: dict[str, Any]) -> dict[str, Any]:
        rewritten = dict(current)
        rewritten.pop("supported_durations", None)
        return rewritten

    def definition_metadata(current: dict[str, Any]) -> dict[str, Any]:
        if NOTE_KEY not in current:
            return current
        rewritten = dict(current)
        rewritten.pop(NOTE_KEY)
        rewritten[GAP_KEY] = GAP_TEXT
        return rewritten

    _rewrite(profile_metadata, definition_metadata, upgrade=False)
