"""FREE-plan model targets and the counters behind its hard usage gates.

Two things move here, both server-side facts the browser can only observe:

1. ``doubao-free-reasoner`` — the model every FREE reasoning-role binding
   already points at — was seeded with the ``CONFIGURE_DOUBAO_MODEL_ID``
   placeholder and left disabled because no deployable Doubao ID existed at
   the time. The operator has since named one: ``doubao-seed-2-0-lite-260428``
   (Ark; token pricing seeded by 0051). The update is guarded on the
   placeholder so an operator-configured value is never overwritten —
   ``ensure_defaults()`` is create-only by design and cannot make this move.

2. ``workspace_usage_counters`` — one row per workspace holding the FREE
   plan's metered totals (currently deep prompt optimizations). Image totals
   are counted from generation_jobs and director rounds from creative_turns,
   so neither needs a column here.

Revision ID: 0064_free_tier_defaults
Revises: 0063_qa_result_producer_run
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_free_tier_defaults"
down_revision: str | None = "0063_qa_result_producer_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLACEHOLDER = "CONFIGURE_DOUBAO_MODEL_ID"
_FREE_CHAT_MODEL_ID = "doubao-seed-2-0-lite-260428"


def upgrade() -> None:
    op.create_table(
        "workspace_usage_counters",
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "prompt_optimizations", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "prompt_optimizations >= 0", name="ck_workspace_usage_prompt_optimizations"
        ),
    )
    if "model_definitions" in set(sa.inspect(op.get_bind()).get_table_names()):
        # Recovery snapshots restored without the model registry get the
        # repoint from ensure_defaults() seeding a fresh catalogue instead.
        op.get_bind().execute(
            sa.text(
                "UPDATE model_definitions SET provider_model_id = :model_id, enabled = :enabled "
                "WHERE logical_name = 'doubao-free-reasoner' AND provider_model_id = :placeholder"
            ),
            {"model_id": _FREE_CHAT_MODEL_ID, "enabled": True, "placeholder": _PLACEHOLDER},
        )


def downgrade() -> None:
    if "model_definitions" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.get_bind().execute(
            sa.text(
                "UPDATE model_definitions SET provider_model_id = :placeholder, enabled = :enabled "
                "WHERE logical_name = 'doubao-free-reasoner' AND provider_model_id = :model_id"
            ),
            {"model_id": _FREE_CHAT_MODEL_ID, "enabled": False, "placeholder": _PLACEHOLDER},
        )
    op.drop_table("workspace_usage_counters")
