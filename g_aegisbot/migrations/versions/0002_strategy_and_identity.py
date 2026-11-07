"""Strategy ids on trade state; immutable identity columns on profiles.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07

Additive only (new nullable columns, or NOT NULL columns with a server default), so it is safe on a database that
already holds data. Existing rows keep working: trades default to the 'breakout' strategy, and profiles whose
user_id is an email get that email copied into login_email so the first login can bind the Supabase user id.
No existing column is changed or dropped.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_profiles") as b:
        b.add_column(sa.Column("auth_user_id", sa.String(length=64), nullable=True))
        b.add_column(sa.Column("login_email", sa.String(length=254), nullable=True))
        b.create_index("ix_user_profiles_auth_user_id", ["auth_user_id"], unique=True)
        b.create_index("ix_user_profiles_login_email", ["login_email"], unique=False)
    with op.batch_alter_table("open_trades") as b:
        b.add_column(sa.Column("strategy", sa.String(length=24), nullable=False, server_default="breakout"))
        b.add_column(sa.Column("trail_atr_mult", sa.Float(), nullable=True))
    with op.batch_alter_table("trade_log") as b:
        b.add_column(sa.Column("strategy", sa.String(length=24), nullable=False, server_default="breakout"))
    with op.batch_alter_table("paper_positions") as b:
        b.add_column(sa.Column("strategy", sa.String(length=24), nullable=False, server_default="breakout"))
    op.execute("UPDATE user_profiles SET login_email = lower(user_id) WHERE login_email IS NULL AND user_id LIKE '%@%'")


def downgrade() -> None:
    with op.batch_alter_table("paper_positions") as b:
        b.drop_column("strategy")
    with op.batch_alter_table("trade_log") as b:
        b.drop_column("strategy")
    with op.batch_alter_table("open_trades") as b:
        b.drop_column("trail_atr_mult")
        b.drop_column("strategy")
    with op.batch_alter_table("user_profiles") as b:
        b.drop_index("ix_user_profiles_login_email")
        b.drop_index("ix_user_profiles_auth_user_id")
        b.drop_column("login_email")
        b.drop_column("auth_user_id")
