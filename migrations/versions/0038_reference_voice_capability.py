"""Separate the audio a model produces from the audio it is conditioned on.

`supports_audio` has always meant native audio *out* — the router reads it for
`requires_native_audio`, and a model declaring it promises a soundtrack. There
was no way at all to say the other thing: that a model accepts a voice or audio
asset *in*, as a reference it conditions on.

The two were therefore indistinguishable in a profile, which is how a model can
end up advertising voice conditioning that no adapter is able to send. The Wan
2.7 contract made that concrete: its I2V mode takes audio alongside images, so
the question "does this model accept a voice reference" had to have an explicit
answer rather than being inferred from a flag that means something else.

The answer for every model shipped today is `false`, which is why this column
defaults false and backfills nothing. Declaring it true is what obliges an
adapter to carry the role on the wire, and the routing-integrity gate refuses
the pair drifting apart.

Revision ID: 0038_reference_voice_capability
Revises: 0037_direct_uploads
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_reference_voice_capability"
down_revision: str | None = "0037_direct_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "model_capability_profiles"
_COLUMN = "supports_reference_voice"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    columns = _columns()
    if not columns or _COLUMN in columns:
        # A recovery database that never reached the capability registry has
        # nothing to alter; earlier migrations skip on the same shape.
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if _COLUMN not in _columns():
        return
    op.drop_column(_TABLE, _COLUMN)
