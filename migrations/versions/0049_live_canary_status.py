"""Record where each model stands in the live canary sequence.

Contract and pricing can be audited from a desk. A live canary cannot: it needs
the provider to accept one real request, and that can be refused by things this
repository does not control — an account privacy setting, an empty balance, a
permission not granted. `openai/gpt-image-2` is in exactly that position: its ID,
wire format and price are all confirmed, and its one paid attempt was refused by
OpenRouter's router because the account ignores every upstream provider for it.

Without somewhere to write that down, a blocked model stalls the audit of every
model behind it, and — worse — a model that was never actually proven can later
be mistaken for one that was, because both simply lack a success record.

    NOT_RUN                no canary attempted yet
    VERIFIED_LIVE          one real generation completed and reconciled
    LIVE_BLOCKED_EXTERNAL  attempted, refused outside this codebase; detail says by what
    CONTRACT_INVALID       the provider rejected the request we build

`live_canary_detail` carries the reason, so a blocked model can be retried when
its blocker is cleared rather than re-investigated from nothing.

Revision ID: 0049_live_canary_status
Revises: 0048_seedream_wan
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_live_canary_status"
down_revision: str | None = "0048_seedream_wan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BLOCKED = "LIVE_BLOCKED_EXTERNAL"
GPT_IMAGE_2 = "gpt-image-2-openrouter"
GPT_IMAGE_2_DETAIL = (
    "2026-08-26: one paid canary refused by OpenRouter's router — "
    "'All providers have been ignored'. openai/gpt-image-2 has a single upstream "
    "endpoint (OpenAI) and the account excludes it. Account usage was unchanged "
    "across the attempt, so nothing was billed. Clear at "
    "https://openrouter.ai/settings/privacy, then re-run the canary."
)


def upgrade() -> None:
    connection = op.get_bind()
    if "model_definitions" not in set(sa.inspect(connection).get_table_names()):
        return
    columns = {item["name"] for item in sa.inspect(connection).get_columns("model_definitions")}
    if "live_canary_status" not in columns:
        op.add_column(
            "model_definitions",
            sa.Column(
                "live_canary_status", sa.String(32), nullable=False, server_default="NOT_RUN"
            ),
        )
        op.create_index(
            "ix_model_definitions_live_canary_status", "model_definitions", ["live_canary_status"]
        )
    if "live_canary_detail" not in columns:
        op.add_column(
            "model_definitions",
            sa.Column("live_canary_detail", sa.String(500), nullable=False, server_default=""),
        )

    connection.execute(
        sa.text(
            "update model_definitions set live_canary_status = :status, "
            "live_canary_detail = :detail where logical_name = :name"
        ),
        {"status": BLOCKED, "detail": GPT_IMAGE_2_DETAIL, "name": GPT_IMAGE_2},
    )


def downgrade() -> None:
    connection = op.get_bind()
    if "model_definitions" not in set(sa.inspect(connection).get_table_names()):
        return
    columns = {item["name"] for item in sa.inspect(connection).get_columns("model_definitions")}
    if "live_canary_status" in columns:
        op.drop_index("ix_model_definitions_live_canary_status", table_name="model_definitions")
        op.drop_column("model_definitions", "live_canary_status")
    if "live_canary_detail" in columns:
        op.drop_column("model_definitions", "live_canary_detail")
