"""Record when a profile's API keys last passed the Binance permission check.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07

Additive only: one nullable column, no default and no backfill. Existing rows get NULL, which means "never verified", so
an existing live profile is not allowed to trade until its keys are verified again (set-keys --verify, or the API key
upload). Nothing else is changed or dropped.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_profiles") as b:
        b.add_column(sa.Column("keys_verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_profiles") as b:
        b.drop_column("keys_verified_at")
