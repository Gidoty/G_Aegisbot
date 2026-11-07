import io
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from g_aegisbot import migrate
from g_aegisbot.db import Base, init_db
from g_aegisbot.db import models, trading_models  # noqa: F401


def engine_at(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{tmp_path}/{name}")


def tables(engine):
    return set(inspect(engine).get_table_names())


def test_fresh_database_migrates_to_head_and_matches_the_models_exactly(tmp_path):
    e = engine_at(tmp_path)
    assert migrate.upgrade(e) == migrate.head_revision(e) == "0002"
    assert tables(e) == set(Base.metadata.tables) | {"alembic_version"}
    with e.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"the models changed without a migration: {diff}"


def test_constraints_survive_the_migration(tmp_path):
    e = engine_at(tmp_path)
    migrate.upgrade(e)
    with e.begin() as c:
        with pytest.raises(IntegrityError):
            c.execute(text("insert into user_profiles (user_id, is_admin, market_type, trading_mode, virtual_balance,"
                           " max_wallet_cap_usdt, active_coins, leverage, subscription_status, daily_loss_tracker,"
                           " created_at, updated_at) values ('x', 0, 'futures', 'testnet', 1, 0, '[]', 9, 'expired',"
                           " '{}', '2026-01-01', '2026-01-01')"))


def test_database_created_before_migrations_is_adopted_not_recreated(tmp_path):
    e = engine_at(tmp_path)
    init_db(e)                                                   # how databases were made before this phase
    with e.begin() as c:
        c.execute(text("insert into controls (scope, paused, flatten, set_at) values ('global', 1, 0, '2026-01-01')"))
    assert "alembic_version" not in tables(e)
    assert migrate.upgrade(e) == "0002"
    with e.connect() as c:
        assert c.execute(text("select scope from controls")).scalar() == "global"      # data untouched


def test_upgrade_is_repeatable(tmp_path):
    e = engine_at(tmp_path)
    assert migrate.upgrade(e) == migrate.upgrade(e) == "0002"


def test_downgrade_removes_everything_the_baseline_created(tmp_path):
    e = engine_at(tmp_path)
    migrate.upgrade(e)
    with e.begin() as conn:
        command.downgrade(migrate._config(e, conn), "base")
    assert tables(e) <= {"alembic_version"}


def test_sqlite_entrypoints_create_tables_directly(tmp_path):
    e = engine_at(tmp_path)
    migrate.ensure_schema(e)
    assert set(Base.metadata.tables) <= tables(e)


def test_postgres_entrypoints_refuse_to_run_on_an_out_of_date_schema(monkeypatch):
    fake_pg = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(migrate, "current_revision", lambda e: None)
    monkeypatch.setattr(migrate, "head_revision", lambda e: "0002")
    with pytest.raises(migrate.SchemaOutOfDate, match="admin migrate"):
        migrate.ensure_schema(fake_pg)
    monkeypatch.setattr(migrate, "current_revision", lambda e: "0002")
    migrate.ensure_schema(fake_pg)                               # up to date: no error


def test_postgres_migration_sql_has_jsonb_and_the_row_level_security_lockdown():
    cfg = Config()
    cfg.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/db")
    cfg.output_buffer = io.StringIO()
    command.upgrade(cfg, "head", sql=True)
    sql = cfg.output_buffer.getvalue()
    assert "JSONB" in sql and "CREATE TABLE user_profiles" in sql
    for table in ("user_profiles", "open_trades", "payments", "audit_log", "controls", "paper_accounts"):
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in sql, table
    assert sql.count("REVOKE ALL") >= 10 and "anon" in sql and "authenticated" in sql


def test_admin_migrate_command(tmp_path):
    from g_aegisbot.admin import Admin, run
    from g_aegisbot.db import get_session_factory
    e = engine_at(tmp_path)
    out = []
    code = run(["migrate"], Admin(get_session_factory(e), None, out=out.append))
    assert code == 0 and "revision 0002" in out[0]


def test_a_database_at_revision_0001_upgrades_in_place_keeping_its_data_and_backfilling_identity(tmp_path):
    e = engine_at(tmp_path)
    with e.begin() as conn:
        command.upgrade(migrate._config(e, conn), "0001")
    with e.begin() as c:
        c.execute(text("insert into user_profiles (user_id, is_admin, market_type, trading_mode, virtual_balance,"
                       " max_wallet_cap_usdt, active_coins, leverage, subscription_status, daily_loss_tracker,"
                       " created_at, updated_at) values ('Alice@Example.com', 0, 'futures', 'testnet', 1, 0, '[]', 3,"
                       " 'expired', '{}', '2026-01-01', '2026-01-01'), ('plainhandle', 0, 'futures', 'testnet', 1, 0, '[]', 3,"
                       " 'expired', '{}', '2026-01-01', '2026-01-01')"))
        c.execute(text("insert into open_trades (user_id, symbol, side, entry_price, stop_price, extreme_price, leverage, mode, opened_at)"
                       " values ('Alice@Example.com', 'BTC/USDT:USDT', 'buy', 1, 1, 1, 3, 'testnet', '2026-01-01')"))
    assert migrate.upgrade(e) == "0002"
    with e.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text("select user_id, login_email, auth_user_id from user_profiles"))}
        assert rows["Alice@Example.com"] == ("alice@example.com", None)        # lowercased copy; the id binds at first login
        assert rows["plainhandle"] == (None, None)
        assert c.execute(text("select strategy from open_trades")).scalar() == "breakout"       # old trades keep their strategy


def test_downgrade_from_0002_removes_only_what_it_added(tmp_path):
    e = engine_at(tmp_path)
    migrate.upgrade(e)
    with e.begin() as conn:
        command.downgrade(migrate._config(e, conn), "0001")
    cols = {c["name"] for c in inspect(e).get_columns("user_profiles")}
    assert "auth_user_id" not in cols and "login_email" not in cols and "user_id" in cols
    assert "strategy" not in {c["name"] for c in inspect(e).get_columns("open_trades")}
    assert migrate.current_revision(e) == "0001"


def test_sqlite_entrypoint_migrates_an_existing_old_database_instead_of_assuming_it_is_current(tmp_path):
    e = engine_at(tmp_path)
    with e.begin() as conn:
        command.upgrade(migrate._config(e, conn), "0001")                      # an old local database
    migrate.ensure_schema(e)
    assert migrate.current_revision(e) == "0002"
    assert "strategy" in {c["name"] for c in inspect(e).get_columns("trade_log")}


def test_a_fresh_sqlite_entrypoint_database_is_stamped_at_head(tmp_path):
    e = engine_at(tmp_path)
    migrate.ensure_schema(e)
    assert migrate.current_revision(e) == migrate.head_revision(e) == "0002"


def test_postgres_sql_for_0002_is_additive_only():
    cfg = Config()
    cfg.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/db")
    cfg.output_buffer = io.StringIO()
    command.upgrade(cfg, "0001:0002", sql=True)
    sql = cfg.output_buffer.getvalue().upper()
    for needle in ("AUTH_USER_ID", "LOGIN_EMAIL", "STRATEGY", "TRAIL_ATR_MULT"):
        assert needle in sql, needle
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in sql, forbidden
