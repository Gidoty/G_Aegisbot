"""Baseline schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-06

Databases created before migrations existed (by create_all) are adopted with `stamp` by
g_aegisbot.migrate.upgrade(); fresh databases run this file. On Postgres (Supabase) it also turns on
row-level security and revokes the API roles for every table, because Supabase exposes the public
schema through its REST API and these tables hold encrypted exchange keys.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = ['audit_log', 'controls', 'equity_snapshots', 'heartbeats', 'open_trades', 'paper_accounts', 'paper_positions', 'payments', 'trade_log', 'user_profiles']


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=48), nullable=False),
    sa.Column('target', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.String(length=400), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_at'), 'audit_log', ['at'], unique=False)
    op.create_table('controls',
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('flatten', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=True),
    sa.Column('set_by', sa.String(length=64), nullable=True),
    sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('scope')
    )
    op.create_table('equity_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('equity', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_equity_snapshots_at'), 'equity_snapshots', ['at'], unique=False)
    op.create_index(op.f('ix_equity_snapshots_user_id'), 'equity_snapshots', ['user_id'], unique=False)
    op.create_table('heartbeats',
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('info', sa.String(length=200), nullable=True),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('open_trades',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('stop_price', sa.Float(), nullable=False),
    sa.Column('extreme_price', sa.Float(), nullable=False),
    sa.Column('stop_order_id', sa.String(length=64), nullable=True),
    sa.Column('leverage', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_trail_ts', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'symbol', name='uq_open_trade')
    )
    op.create_index(op.f('ix_open_trades_user_id'), 'open_trades', ['user_id'], unique=False)
    op.create_table('paper_accounts',
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('wallet_balance', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('paper_positions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('leverage', sa.Integer(), nullable=False),
    sa.Column('margin', sa.Float(), nullable=False),
    sa.Column('entry_fee', sa.Float(), nullable=False),
    sa.Column('stop_price', sa.Float(), nullable=True),
    sa.Column('stop_order_id', sa.String(length=64), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('next_check_ts', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'symbol', name='uq_paper_position')
    )
    op.create_index(op.f('ix_paper_positions_user_id'), 'paper_positions', ['user_id'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.String(length=96), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('invoice_id', sa.String(length=64), nullable=True),
    sa.Column('payment_id', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('price_usd', sa.Float(), nullable=False),
    sa.Column('days', sa.Integer(), nullable=False),
    sa.Column('credited', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('order_id')
    )
    op.create_index(op.f('ix_payments_payment_id'), 'payments', ['payment_id'], unique=False)
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)
    op.create_table('trade_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('exit_price', sa.Float(), nullable=True),
    sa.Column('pnl', sa.Float(), nullable=True),
    sa.Column('fees', sa.Float(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('exit_reason', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trade_log_user_id'), 'trade_log', ['user_id'], unique=False)
    op.create_table('user_profiles',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('encrypted_api_key', sa.Text(), nullable=True),
    sa.Column('encrypted_api_secret', sa.Text(), nullable=True),
    sa.Column('market_type', sa.String(length=16), nullable=False),
    sa.Column('trading_mode', sa.String(length=16), nullable=False),
    sa.Column('virtual_balance', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('max_wallet_cap_usdt', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('active_coins', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('leverage', sa.Integer(), nullable=False),
    sa.Column('subscription_status', sa.String(length=16), nullable=False),
    sa.Column('subscription_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('daily_loss_tracker', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("market_type = 'futures'", name='ck_market_type'),
    sa.CheckConstraint("subscription_status IN ('active', 'expired')", name='ck_subscription_status'),
    sa.CheckConstraint("trading_mode IN ('live', 'testnet')", name='ck_trading_mode'),
    sa.CheckConstraint('leverage BETWEEN 1 AND 5', name='ck_leverage_range'),
    sa.CheckConstraint('max_wallet_cap_usdt >= 0', name='ck_wallet_cap_nonneg'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_profiles_user_id'), 'user_profiles', ['user_id'], unique=True)

    if op.get_context().dialect.name == "postgresql":
        from g_aegisbot.db.session import postgres_lockdown_sql
        for stmt in postgres_lockdown_sql(TABLES):
            op.execute(sa.text(stmt))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
