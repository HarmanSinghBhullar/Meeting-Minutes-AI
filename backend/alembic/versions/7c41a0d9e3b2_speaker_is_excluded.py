"""speaker is_excluded

Adds the flag a human sets when a diarization cluster turns out not to be a
participant at all — a shared video, hold music, a speakerphone in the next room.
Excluded speakers keep their transcript segments but are withheld from the
minutes.

Backfilled false: every existing row is a real participant, since nothing could
have marked one otherwise until now.

Revision ID: 7c41a0d9e3b2
Revises: 2baf545b9e3e
Create Date: 2026-07-16 11:02:47.881204
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '7c41a0d9e3b2'
down_revision: str | None = '2baf545b9e3e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default carries the existing rows; it is then dropped so the
    # application, not the database, owns the default from here on.
    op.add_column(
        'speakers',
        sa.Column(
            'is_excluded',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column('speakers', 'is_excluded', server_default=None)


def downgrade() -> None:
    op.drop_column('speakers', 'is_excluded')
