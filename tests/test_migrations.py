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
    assert migrate.upgrade(e) == migrate.head_revision(e) == "0001"
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
    assert migrate.upgrade(e) == "0001"
    with e.connect() as c:
        assert c.execute(text("select scope from controls")).scalar() == "global"      # data untouched


def test_upgrade_is_repeatable(tmp_path):
    e = engine_at(tmp_path)
    assert migrate.upgrade(e) == migrate.upgrade(e) == "0001"


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
    monkeypatch.setattr(migrate, "head_revision", lambda e: "0001")
    with pytest.raises(migrate.SchemaOutOfDate, match="admin migrate"):
        migrate.ensure_schema(fake_pg)
    monkeypatch.setattr(migrate, "current_revision", lambda e: "0001")
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
    assert code == 0 and "revision 0001" in out[0]
