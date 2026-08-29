"""Reconcile the Google Flow remote-project ownership index.

The migration history has always declared ``uq_flow_remote_project_owner``:
one Google Flow remote project may belong to only one local binding, including
after that binding is disabled.  The long-lived production volume instead
contains an older ``uq_flow_active_remote_project`` index whose predicate only
covers active rows.  That schema drift permits a disabled binding's remote id
to be claimed again and makes ``alembic check`` fail after an otherwise clean
upgrade.

Fresh databases already have the correct index, so this revision is a no-op
there.  Drifted databases are repaired after a duplicate-owner preflight.

Revision ID: 0060_flow_remote_owner_index
Revises: 0059_timeline_branches
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_flow_remote_owner_index"
down_revision: str | None = "0059_timeline_branches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CORRECT_INDEX = "uq_flow_remote_project_owner"
_DRIFTED_INDEX = "uq_flow_active_remote_project"
_OWNER_PREDICATE = "provider = 'google_flow' AND provider_project_id IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "provider_projects" not in set(inspector.get_table_names()):
        return

    duplicate = bind.execute(
        sa.text(
            "SELECT provider_project_id FROM provider_projects "
            "WHERE provider = 'google_flow' AND provider_project_id IS NOT NULL "
            "GROUP BY provider, provider_project_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            "Google Flow remote project ownership is duplicated; reconcile remote project "
            f"{duplicate!r} before upgrading"
        )

    indexes = {str(index["name"]) for index in inspector.get_indexes("provider_projects")}
    if _DRIFTED_INDEX in indexes:
        op.drop_index(_DRIFTED_INDEX, table_name="provider_projects")
    if _CORRECT_INDEX not in indexes:
        op.create_index(
            _CORRECT_INDEX,
            "provider_projects",
            ["provider", "provider_project_id"],
            unique=True,
            sqlite_where=sa.text(_OWNER_PREDICATE),
            postgresql_where=sa.text(_OWNER_PREDICATE),
        )


def downgrade() -> None:
    # Revision 0025 already declares the owner index, so the schema expected at
    # 0059 is the repaired schema too.  Reintroducing production drift during a
    # rollback would violate that older revision rather than restore it.
    return
