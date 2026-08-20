"""Add revocable worker credentials and one-use WebSocket tickets.

Revision ID: 0014_worker_scoped_credentials
Revises: 0013_generation_reservation_ownership
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_worker_scoped_credentials"
down_revision: str | None = "0013_generation_reservation_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "worker_access_credentials" not in tables:
        op.create_table(
            "worker_access_credentials",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("worker_id", sa.String(length=100), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["provider_accounts.id"],
                name="fk_worker_access_credentials_account_id_provider_accounts",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_worker_access_credentials_token_hash"),
        )
        op.create_index(
            "ix_worker_access_credentials_worker_id",
            "worker_access_credentials",
            ["worker_id"],
        )
        op.create_index(
            "ix_worker_access_credentials_provider",
            "worker_access_credentials",
            ["provider"],
        )
        op.create_index(
            "ix_worker_access_credentials_account_id",
            "worker_access_credentials",
            ["account_id"],
        )
        op.create_index(
            "ix_worker_access_credentials_expires_at",
            "worker_access_credentials",
            ["expires_at"],
        )
        op.create_index(
            "ix_worker_access_credentials_revoked_at",
            "worker_access_credentials",
            ["revoked_at"],
        )

    tables = _tables()
    if "worker_socket_tickets" not in tables:
        op.create_table(
            "worker_socket_tickets",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("credential_id", sa.String(length=36), nullable=False),
            sa.Column("worker_id", sa.String(length=100), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["credential_id"],
                ["worker_access_credentials.id"],
                name="fk_worker_socket_ticket_credential",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_worker_socket_tickets_token_hash"),
        )
        op.create_index(
            "ix_worker_socket_tickets_credential_id",
            "worker_socket_tickets",
            ["credential_id"],
        )
        op.create_index(
            "ix_worker_socket_tickets_worker_id",
            "worker_socket_tickets",
            ["worker_id"],
        )
        op.create_index(
            "ix_worker_socket_tickets_expires_at",
            "worker_socket_tickets",
            ["expires_at"],
        )
        op.create_index(
            "ix_worker_socket_tickets_consumed_at",
            "worker_socket_tickets",
            ["consumed_at"],
        )


def downgrade() -> None:
    tables = _tables()
    if "worker_socket_tickets" in tables:
        op.drop_table("worker_socket_tickets")
    if "worker_access_credentials" in tables:
        op.drop_table("worker_access_credentials")
