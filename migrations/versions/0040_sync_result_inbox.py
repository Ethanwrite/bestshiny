"""Make a synchronous provider's result survive the process that received it.

A synchronous image API answers with the artefact in the response body. There
is no remote job to re-read and no URL to fetch, so once the submission is
confirmed the bytes exist in exactly one place. That place was a dictionary on
the Gateway object, between the confirmation and the poll that consumes it —
both inside one `process()` call. Process death in that window lost an artefact
the workspace had already been billed for. It was never a silent success or a
silent refund: `get_job` reported `OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE`
with `submitted=True` and the credit moved to `RECONCILIATION_REQUIRED`. It was
still a paid result that no longer existed.

The result is now written in the same transaction that confirms the
submission — so it exists for exactly the outcomes the confirmation exists for
— and deleted by the completion that consumes it.

Rows are transient by construction: one per in-flight synchronous job, removed
on completion, and cascaded away with the job. `content` is `bytea` rather than
an object-storage key because this is an inbox, not the media plane; the bytes
move into the media plane the moment the poll completes, through the same
content validation a downloaded artefact passes.

Revision ID: 0040_sync_result_inbox
Revises: 0039_integrity_errcodes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_sync_result_inbox"
down_revision: str | None = "0039_integrity_errcodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESULTS = "provider_synchronous_results"
_OUTPUTS = "provider_synchronous_result_outputs"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if _RESULTS not in tables:
        op.create_table(
            _RESULTS,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "generation_job_id",
                sa.String(36),
                sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider_job_id", sa.String(500), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(40), nullable=False),
            sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
            sa.Column("output_url", sa.Text()),
            sa.Column("output_mime_type", sa.String(120)),
            sa.Column("error", sa.Text()),
            sa.Column("raw_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("generation_job_id", name="uq_provider_sync_result_job"),
            sa.CheckConstraint("attempt_number >= 1", name="ck_provider_sync_result_attempt"),
        )
        op.create_index(
            "ix_provider_synchronous_results_generation_job_id", _RESULTS, ["generation_job_id"]
        )
    if _OUTPUTS not in tables:
        op.create_table(
            _OUTPUTS,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "result_id",
                sa.String(36),
                sa.ForeignKey(f"{_RESULTS}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("mime_type", sa.String(120), nullable=False),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("result_id", "ordinal", name="uq_provider_sync_output_ordinal"),
            sa.CheckConstraint("ordinal >= 0", name="ck_provider_sync_output_ordinal"),
            sa.CheckConstraint("length(content_sha256) = 64", name="ck_provider_sync_output_digest"),
        )
        op.create_index("ix_provider_synchronous_result_outputs_result_id", _OUTPUTS, ["result_id"])
        op.create_index("ix_provider_synchronous_result_outputs_created_at", _OUTPUTS, ["created_at"])


def downgrade() -> None:
    tables = _tables()
    if _OUTPUTS in tables:
        op.drop_table(_OUTPUTS)
    if _RESULTS in tables:
        op.drop_table(_RESULTS)
