"""Move multimodal memory embeddings to Voyage's official API.

OpenRouter's generic ``/embeddings`` endpoint accepts strings and cannot carry
Voyage's interleaved text/image input. Preserve the old model definition for
historical execution records, create a new official Voyage definition, move the
``MULTIMODAL_EMBEDDING`` binding, and seed the same official list prices under
the provider that now owns the call.

Revision ID: 0071_voyage_official_provider
Revises: 0070_creative_director_screenplay
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0071_voyage_official_provider"
down_revision: str | None = "0070_creative_director_screenplay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_NAME = "voyage-multimodal-3.5-openrouter"
OLD_PROVIDER = "openrouter"
OLD_MODEL = "voyageai/voyage-multimodal-3.5"
NEW_NAME = "voyage-multimodal-3.5-official"
NEW_PROVIDER = "voyage"
NEW_MODEL = "voyage-multimodal-3.5"
ROLE = "MULTIMODAL_EMBEDDING"
CHECKED_AT = datetime(2026, 9, 2, tzinfo=UTC)
SOURCE_URL = "https://docs.voyageai.com/docs/pricing"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _definition(table: sa.Table, *, logical_name: str) -> dict[str, object] | None:
    row = op.get_bind().execute(
        sa.select(table).where(table.c.logical_name == logical_name)
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _ensure_definition(tables: set[str]) -> tuple[str | None, str | None]:
    if "model_definitions" not in tables:
        return None, None
    connection = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("model_definitions", metadata, autoload_with=connection)
    old = _definition(definitions, logical_name=OLD_NAME)
    current = _definition(definitions, logical_name=NEW_NAME)
    if current is None and old is not None:
        now = datetime.now(UTC)
        current = dict(old)
        current.update(
            {
                "id": str(uuid.uuid4()),
                "logical_name": NEW_NAME,
                "provider": NEW_PROVIDER,
                "provider_model_id": NEW_MODEL,
                "enabled": bool(old.get("enabled", True)),
                "live_enabled": bool(old.get("live_enabled", False)),
                "context_window": 32000,
                "metadata_json": {
                    "transport": "voyage",
                    "api_surface": "POST /v1/multimodalembeddings",
                    "natively_multimodal": True,
                    "credential_required": True,
                    "not_for_face_identity_verification": True,
                    "capability_source": (
                        "https://docs.voyageai.com/reference/multimodal-embeddings-api"
                    ),
                },
                "created_at": now,
                "updated_at": now,
            }
        )
        if "display_name" in definitions.c:
            current["display_name"] = "Voyage Multimodal 3.5"
        if "pricing_status" in definitions.c:
            current["pricing_status"] = "VERIFIED"
        if "live_canary_status" in definitions.c:
            current["live_canary_status"] = "NOT_RUN"
            current["live_canary_detail"] = ""
            current["last_verified_at"] = None
            current["last_live_test_at"] = None
        connection.execute(definitions.insert().values(**current))
    if old is not None:
        values: dict[str, object] = {
            "enabled": False,
            "live_enabled": False,
            "updated_at": datetime.now(UTC),
        }
        if "router_enabled" in definitions.c:
            values["router_enabled"] = False
        metadata_json = dict(old.get("metadata_json") or {})
        metadata_json["retired_reason"] = (
            "Official Voyage multimodal transport replaces the string-only OpenRouter embeddings surface."
        )
        values["metadata_json"] = metadata_json
        connection.execute(
            definitions.update().where(definitions.c.id == old["id"]).values(**values)
        )
    return (
        str(old["id"]) if old is not None else None,
        str(current["id"]) if current is not None else None,
    )


def _ensure_profile(tables: set[str], old_id: str | None, new_id: str | None) -> None:
    if not old_id or not new_id or "model_capability_profiles" not in tables:
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    profiles = sa.Table("model_capability_profiles", metadata, autoload_with=connection)
    if connection.execute(
        sa.select(profiles.c.model_definition_id).where(
            profiles.c.model_definition_id == new_id
        )
    ).scalar_one_or_none():
        return
    old = connection.execute(
        sa.select(profiles).where(profiles.c.model_definition_id == old_id)
    ).mappings().one_or_none()
    if old is None:
        return
    values = dict(old)
    values["model_definition_id"] = new_id
    values["profile_version"] = "voyage-multimodal-3.5-official-v1"
    values["provider_metadata"] = {
        "adapter": "voyage",
        "endpoint": "/v1/multimodalembeddings",
        "output_dimensions": [256, 512, 1024, 2048],
    }
    values["source"] = "MANUAL_PRIOR"
    values["created_at"] = datetime.now(UTC)
    values["updated_at"] = values["created_at"]
    connection.execute(profiles.insert().values(**values))


def _move_bindings(tables: set[str], old_id: str | None, new_id: str | None) -> None:
    if not old_id or not new_id or "model_role_bindings" not in tables:
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    bindings = sa.Table("model_role_bindings", metadata, autoload_with=connection)
    connection.execute(
        bindings.update()
        .where(
            bindings.c.role == ROLE,
            bindings.c.model_definition_id == old_id,
        )
        .values(model_definition_id=new_id, updated_at=datetime.now(UTC))
    )


def _ensure_pricing(tables: set[str]) -> None:
    if "model_pricing_profiles" not in tables:
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    pricing = sa.Table("model_pricing_profiles", metadata, autoload_with=connection)
    now = datetime.now(UTC)
    rows = (
        (
            "input_tokens",
            "1M_tokens",
            "0.12",
            "unit_price * input_tokens / 1e6 * usd_per_currency",
            "Text input list price; output is not billed.",
        ),
        (
            "image_input",
            "1B_pixels",
            "0.60",
            "unit_price * input_pixels / 1e9 * usd_per_currency",
            "Image input is billed per billion pixels.",
        ),
    )
    for input_mode, unit, price, formula, note in rows:
        exists = connection.execute(
            sa.select(pricing.c.id).where(
                pricing.c.provider == NEW_PROVIDER,
                pricing.c.provider_model_id == NEW_MODEL,
                pricing.c.input_mode == input_mode,
                pricing.c.resolution == "",
            )
        ).scalar_one_or_none()
        if exists:
            continue
        connection.execute(
            pricing.insert().values(
                id=str(uuid.uuid4()),
                provider=NEW_PROVIDER,
                provider_model_id=NEW_MODEL,
                input_mode=input_mode,
                resolution="",
                currency="USD",
                billing_unit=unit,
                unit_price=price,
                estimate_unit=unit,
                estimate_unit_price=price,
                usd_per_currency="1.0",
                fx_source="",
                fx_checked_at=None,
                estimate_formula=formula.replace("unit_price", "estimate_unit_price"),
                settlement_formula=formula,
                effective_from=CHECKED_AT,
                effective_until=None,
                source_url=SOURCE_URL,
                source_checked_at=CHECKED_AT,
                notes=(
                    f"Official voyage-multimodal-3.5 list price. {note} "
                    "The platform uses this model for advisory retrieval only."
                ),
                created_at=now,
                updated_at=now,
            )
        )


def upgrade() -> None:
    tables = _tables()
    old_id, new_id = _ensure_definition(tables)
    _ensure_profile(tables, old_id, new_id)
    _move_bindings(tables, old_id, new_id)
    _ensure_pricing(tables)


def downgrade() -> None:
    tables = _tables()
    if "model_definitions" not in tables:
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("model_definitions", metadata, autoload_with=connection)
    old = _definition(definitions, logical_name=OLD_NAME)
    new = _definition(definitions, logical_name=NEW_NAME)
    if old is not None:
        connection.execute(
            definitions.update()
            .where(definitions.c.id == old["id"])
            .values(enabled=True, live_enabled=False, updated_at=datetime.now(UTC))
        )
    if old is not None and new is not None and "model_role_bindings" in tables:
        bindings = sa.Table("model_role_bindings", metadata, autoload_with=connection)
        connection.execute(
            bindings.update()
            .where(
                bindings.c.role == ROLE,
                bindings.c.model_definition_id == new["id"],
            )
            .values(model_definition_id=old["id"], updated_at=datetime.now(UTC))
        )
    # Retain the official definition and its prices as disabled historical data:
    # once a live execution record references it, deleting it is forbidden.
    if new is not None:
        connection.execute(
            definitions.update()
            .where(definitions.c.id == new["id"])
            .values(enabled=False, live_enabled=False, updated_at=datetime.now(UTC))
        )
