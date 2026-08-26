"""Correct Seedance 2.5 to the model Volcengine Ark actually publishes.

The registry carried `seedance-2.5` as the provider model ID. That string is not
a model on any provider; it was the internal logical name leaking into the field
that names an execution target. Ark answered the first live submission with
"The model or endpoint seedance-2.5 does not exist or you do not have access to
it", which cost a reservation and an operator reconciliation rather than a clip.

Ark publishes this model as `doubao-seedance-2-5-260628`. The neighbouring
`dreamina-seedance-2-5-260628` is BytePlus, a different provider namespace on a
different host, and must not be written here.

Three facts move with the ID, all from the Ark model card and pricing page
(queried 2026-08-26):

* **Duration** is `[4, 30]` seconds. The profile said 1–15, so it both admitted
  a duration Ark rejects and refused two thirds of the range Ark allows.
* **Resolution** is 480p / 720p / 1080p. The profile listed 720p/1080p only.
* **Ratio** adds `4:3`, `3:4`, `21:9` and `adaptive` to the three already there.

And the one that was quietly losing money: **price**. The profile carried
`estimated_per_second = 0.09` USD with no recorded provenance. Ark's published
typical price for this model at 720p, 16:9, no video input, is 7.56 CNY per 5-second
video — 1.512 CNY/s. At the PBOC/CFETS central parity for 2026-08-26
(1 CNY = 0.14743 USD) that is **0.2229 USD/s**, so every video quote was being
issued at roughly 40% of what the clip actually costs. Billing is by
`usage.completion_tokens` rather than by the second; this per-second figure is
the platform's estimate for reserving credits up front, and it is now anchored to
a published number with a date and a source rather than to a round guess.

Sources, recorded on the profile so the next person does not have to re-derive
them: https://www.volcengine.com/docs/82379/1330310 (model card, ID, duration,
resolution) and https://www.volcengine.com/docs/82379/1544106 (pricing).

Revision ID: 0043_seedance_25_ark
Revises: 0042_admin_console
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_seedance_25_ark"
down_revision: str | None = "0042_admin_console"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOGICAL_NAME = "seedance-2.5-official"
ARK_MODEL_ID = "doubao-seedance-2-5-260628"
PLACEHOLDER_MODEL_ID = "seedance-2.5"

RATIOS = ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]
RESOLUTIONS = ["480p", "720p", "1080p"]
MIN_DURATION = 4.0
MAX_DURATION = 30.0

# 7.56 CNY per 5s at 720p / 16:9 / no video input, x 0.14743 USD per CNY.
ARK_USD_PER_SECOND = 0.2229
PRICING_SOURCE = {
    "provider": "volcengine_ark",
    "model_card": "https://www.volcengine.com/docs/82379/1330310",
    "pricing": "https://www.volcengine.com/docs/82379/1544106",
    "queried_on": "2026-08-26",
    "basis": "720p 16:9 no-video-input typical price 7.56 CNY / 5s",
    "cny_per_second": 1.512,
    "usd_per_cny": 0.14743,
    "fx_source": "PBOC/CFETS central parity 2026-08-26",
    "billing_unit": "usage.completion_tokens",
}


def _definition_row(connection):  # type: ignore[no-untyped-def]
    return connection.execute(
        sa.text(
            "select id, provider_model_id from model_definitions where logical_name = :name"
        ),
        {"name": LOGICAL_NAME},
    ).first()


def upgrade() -> None:
    connection = op.get_bind()
    # This is a data correction, not a schema change. Some histories reach this
    # revision with the registry tables deliberately absent (the partial-schema
    # recovery tests build exactly that), and there is nothing to correct there.
    tables = set(sa.inspect(connection).get_table_names())
    if not {"model_definitions", "model_capability_profiles"} <= tables:
        return
    row = _definition_row(connection)
    if row is None:
        # A database that never seeded this model has nothing to correct.
        return
    definition_id, current_model_id = row[0], row[1]

    # Only replace the placeholder. An operator who has already pointed this at a
    # specific Ark endpoint ID (`ep-...`) has made a deployment decision, and a
    # migration is the wrong place to overrule it.
    if current_model_id == PLACEHOLDER_MODEL_ID:
        connection.execute(
            sa.text("update model_definitions set provider_model_id = :id where id = :row"),
            {"id": ARK_MODEL_ID, "row": definition_id},
        )
    connection.execute(
        sa.text(
            "update model_definitions set max_duration = :max_duration, "
            "supported_aspect_ratios = :ratios where id = :row"
        ).bindparams(sa.bindparam("ratios", type_=sa.JSON())),
        {"max_duration": MAX_DURATION, "ratios": RATIOS, "row": definition_id},
    )

    profile = connection.execute(
        sa.text(
            "select provider_metadata from model_capability_profiles "
            "where model_definition_id = :row"
        ),
        {"row": definition_id},
    ).first()
    if profile is None:
        return
    metadata = dict(profile[0] or {})
    cost = dict(metadata.get("cost") or {})
    cost["estimated_per_second"] = ARK_USD_PER_SECOND
    metadata["cost"] = cost
    metadata["pricing_source"] = PRICING_SOURCE
    connection.execute(
        sa.text(
            "update model_capability_profiles set min_duration = :min_duration, "
            "max_duration = :max_duration, supported_resolutions = :resolutions, "
            "supported_aspect_ratios = :ratios, provider_metadata = :metadata, "
            "supports_end_frame = :true_value, supports_start_end = :true_value "
            "where model_definition_id = :row"
        ).bindparams(
            sa.bindparam("resolutions", type_=sa.JSON()),
            sa.bindparam("ratios", type_=sa.JSON()),
            sa.bindparam("metadata", type_=sa.JSON()),
        ),
        {
            "min_duration": MIN_DURATION,
            "max_duration": MAX_DURATION,
            "resolutions": RESOLUTIONS,
            "ratios": RATIOS,
            "metadata": metadata,
            "true_value": True,
            "row": definition_id,
        },
    )


def downgrade() -> None:
    # Deliberately not reinstating `seedance-2.5`: it names nothing on any
    # provider, and restoring it would restore a live failure.
    return
