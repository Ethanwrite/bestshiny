"""Create the V1 production, media, provider and generation schema."""

from alembic import op

from migrations.schema_snapshots.platform_v1 import metadata as V1_METADATA

revision = "0001_platform_v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    V1_METADATA.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    V1_METADATA.drop_all(bind=op.get_bind(), checkfirst=True)
