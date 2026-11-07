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
    assert migrate.upgrade(e) == migrate.head_revision(e) == "0003"
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
    assert migrate.upgrade(e) == "0003"
    with e.connect() as c:
        assert c.execute(text("select scope from controls")).scalar() == "global"      # data untouched


def test_upgrade_is_repeatable(tmp_path):
    e = engine_at(tmp_path)
    assert migrate.upgrade(e) == migrate.upgrade(e) == "0003"


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
    monkeypatch.setattr(migrate, "head_revision", lambda e: "0003")
    with pytest.raises(migrate.SchemaOutOfDate, match="admin migrate"):
        migrate.ensure_schema(fake_pg)
    monkeypatch.setattr(migrate, "current_revision", lambda e: "0003")
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
    assert code == 0 and "revision 0003" in out[0]


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
    assert migrate.upgrade(e) == "0003"
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
    assert migrate.current_revision(e) == "0003"
    assert "strategy" in {c["name"] for c in inspect(e).get_columns("trade_log")}


def test_a_fresh_sqlite_entrypoint_database_is_stamped_at_head(tmp_path):
    e = engine_at(tmp_path)
    migrate.ensure_schema(e)
    assert migrate.current_revision(e) == migrate.head_revision(e) == "0003"


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


# ---------------------------------------------------------------- 0003: keys_verified_at (isolated temporary databases only)
def _profile_cols(e):
    return {c["name"]: c for c in inspect(e).get_columns("user_profiles")}


def test_0003_adds_one_nullable_utc_column_and_nothing_else(tmp_path):
    e = engine_at(tmp_path)
    with e.begin() as conn:
        command.upgrade(migrate._config(e, conn), "0002")
    before_tables = tables(e)
    before = _profile_cols(e)
    assert "keys_verified_at" not in before
    assert migrate.upgrade(e) == "0003"
    after = _profile_cols(e)
    assert set(after) - set(before) == {"keys_verified_at"} and set(before) <= set(after)
    assert after["keys_verified_at"]["nullable"] is True and after["keys_verified_at"]["default"] is None
    assert tables(e) == before_tables


def test_0003_keeps_existing_rows_and_leaves_them_unverified(tmp_path):
    e = engine_at(tmp_path)
    with e.begin() as conn:
        command.upgrade(migrate._config(e, conn), "0002")
    with e.begin() as c:
        c.execute(text("insert into user_profiles (user_id, is_admin, market_type, trading_mode, virtual_balance,"
                       " max_wallet_cap_usdt, active_coins, leverage, subscription_status, daily_loss_tracker,"
                       " created_at, updated_at, encrypted_api_key, encrypted_api_secret) values ('old', 0, 'futures', 'live', 1,"
                       " 500, '[]', 3, 'active', '{}', '2026-01-01', '2026-01-01', 'k', 's')"))
    assert migrate.upgrade(e) == "0003"
    with e.connect() as c:
        row = c.execute(text("select user_id, trading_mode, max_wallet_cap_usdt, encrypted_api_key, keys_verified_at"
                             " from user_profiles")).one()
    assert tuple(row)[:4] == ("old", "live", 500, "k") and row[4] is None       # unverified: it cannot trade until re-verified


def test_0003_downgrade_drops_only_its_column(tmp_path):
    e = engine_at(tmp_path)
    migrate.upgrade(e)
    with e.begin() as conn:
        command.downgrade(migrate._config(e, conn), "0002")
    cols = _profile_cols(e)
    assert "keys_verified_at" not in cols and "auth_user_id" in cols and "login_email" in cols
    assert migrate.current_revision(e) == "0002"
    assert migrate.upgrade(e) == "0003"                                       # and it re-applies cleanly


def test_a_pre_migration_database_made_from_the_current_models_is_stamped_at_0003(tmp_path):
    e = engine_at(tmp_path)
    init_db(e)
    assert "keys_verified_at" in _profile_cols(e)
    assert migrate.upgrade(e) == "0003"


def test_a_naive_sqlite_timestamp_round_trips_and_is_read_as_utc(tmp_path):
    from datetime import datetime, timezone

    from g_aegisbot.db import get_session_factory
    from g_aegisbot.db.models import UserProfile
    from g_aegisbot.services import as_utc
    e = engine_at(tmp_path)
    migrate.upgrade(e)
    sf = get_session_factory(e)
    stamp = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
    with sf() as s:
        s.add(UserProfile(user_id="t", keys_verified_at=stamp))
        s.commit()
    with sf() as s:
        got = s.query(UserProfile).one().keys_verified_at
    assert as_utc(got) == stamp                                               # SQLite hands back a naive value; it means UTC


def test_postgres_sql_for_0003_is_one_additive_column():
    cfg = Config()
    cfg.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/db")
    cfg.output_buffer = io.StringIO()
    command.upgrade(cfg, "0002:0003", sql=True)                               # offline: renders SQL, connects to nothing
    sql = cfg.output_buffer.getvalue().upper()
    assert "KEYS_VERIFIED_AT" in sql and "TIMESTAMP WITH TIME ZONE" in sql
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "UPDATE USER_PROFILES"):
        assert forbidden not in sql, forbidden


# ---------------------------------------------------------------- adoption: stamp a revision only when the WHOLE schema matches it
# (every test below uses its own temporary SQLite file; nothing touches a real database)
import pytest

from g_aegisbot.migrate import SchemaInconsistent


def _unrecorded_at(tmp_path, revision, name):
    """A database whose schema is exactly `revision` but which has no alembic_version table (never tracked)."""
    e = engine_at(tmp_path, name)
    with e.begin() as conn:
        command.upgrade(migrate._config(e, conn), revision)
    with e.begin() as c:
        c.execute(text("drop table alembic_version"))
    return e


def _snapshot(e):
    insp = inspect(e)
    return {t: sorted((c["name"], bool(c["nullable"])) for c in insp.get_columns(t)) for t in sorted(insp.get_table_names())}


def _insert_profile(e, user, stamp=None, with_column=True):
    cols = ("user_id, is_admin, market_type, trading_mode, virtual_balance, max_wallet_cap_usdt, active_coins, leverage,"
            " subscription_status, daily_loss_tracker, created_at, updated_at")
    vals = "'%s', 0, 'futures', 'testnet', 1, 0, '[]', 3, 'expired', '{}', '2026-01-01', '2026-01-01'" % user
    if with_column and stamp:
        cols, vals = cols + ", keys_verified_at", vals + f", '{stamp}'"
    with e.begin() as c:
        c.execute(text(f"insert into user_profiles ({cols}) values ({vals})"))


def _verified(e):
    with e.connect() as c:
        return dict(c.execute(text("select user_id, keys_verified_at from user_profiles")).all())


@pytest.mark.parametrize("revision", ["0001", "0002"])
def test_a_valid_untracked_pre_0003_database_is_adopted_and_upgraded_without_inventing_verification(tmp_path, revision):
    e = _unrecorded_at(tmp_path, revision, "pre.db")
    _insert_profile(e, "old", with_column=False)
    assert migrate.upgrade(e) == "0003"
    assert _verified(e) == {"old": None}                           # still unverified: no timestamp was fabricated


def test_a_database_that_fully_matches_0003_is_adopted_and_keeps_its_real_timestamps(tmp_path):
    e = engine_at(tmp_path)
    init_db(e)                                                      # the current models: complete 0003 schema, no version table
    _insert_profile(e, "verified", stamp="2026-03-01 12:00:00.000000")
    _insert_profile(e, "never")
    assert "alembic_version" not in tables(e)
    assert migrate.upgrade(e) == "0003"
    got = _verified(e)
    assert str(got["verified"]).startswith("2026-03-01 12:00:00") and got["never"] is None


def test_a_migrated_0003_database_without_its_version_table_is_adopted(tmp_path):
    e = _unrecorded_at(tmp_path, "0003", "full.db")
    assert migrate.upgrade(e) == "0003"


def test_only_the_keys_verified_at_column_present_is_never_stamped_as_0003(tmp_path):
    e = _unrecorded_at(tmp_path, "0001", "partial.db")             # baseline schema: no 0002 columns, no 0002 indexes
    with e.begin() as c:
        c.execute(text("alter table user_profiles add column keys_verified_at DATETIME"))
    before = _snapshot(e)
    with pytest.raises(SchemaInconsistent) as exc:
        migrate.upgrade(e)
    assert "NOTHING was changed" in str(exc.value) and "keys_verified_at is not part of revision 0001" in str(exc.value)
    assert "alembic_version" not in tables(e) and _snapshot(e) == before         # no stamp, no repair


def _break(e, sql):
    with e.begin() as c:
        c.execute(text(sql))


@pytest.mark.parametrize("sql,needle", [
    ("drop table controls", "table controls is missing"),
    ("drop index ix_user_profiles_login_email", "ix_user_profiles_login_email is missing"),
    ("alter table trade_log drop column strategy", "trade_log.strategy is missing"),
    ("alter table user_profiles add column surprise TEXT", "surprise"),
])
def test_a_database_that_matches_the_models_except_for_one_element_is_refused_untouched(tmp_path, sql, needle):
    e = engine_at(tmp_path)
    init_db(e)
    _break(e, sql)
    before = _snapshot(e)
    with pytest.raises(SchemaInconsistent) as exc:
        migrate.upgrade(e)
    assert needle in str(exc.value) and "NOTHING was changed" in str(exc.value)
    assert "alembic_version" not in tables(e) and _snapshot(e) == before


def test_a_missing_check_constraint_is_detected(tmp_path):
    from alembic.operations import Operations
    e = engine_at(tmp_path)
    init_db(e)
    with e.begin() as c:
        with Operations(MigrationContext.configure(c)).batch_alter_table("user_profiles") as b:
            b.drop_constraint("ck_trading_mode", type_="check")
    assert "ck_trading_mode" not in {x["name"] for x in inspect(e).get_check_constraints("user_profiles")}
    with pytest.raises(SchemaInconsistent) as exc:
        migrate.upgrade(e)
    assert "check constraint ck_trading_mode is missing" in str(exc.value)
    assert "alembic_version" not in tables(e)


def test_a_0002_database_with_an_unexpected_extra_column_matches_no_revision(tmp_path):
    e = _unrecorded_at(tmp_path, "0002", "extra.db")
    _break(e, "alter table user_profiles add column keys_verified_at DATETIME")      # 0003's column without 0003's state
    _break(e, "alter table open_trades add column surprise TEXT")
    with pytest.raises(SchemaInconsistent):
        migrate.upgrade(e)
    assert "alembic_version" not in tables(e)


def test_the_admin_migrate_command_reports_an_inconsistent_schema_clearly_and_changes_nothing(tmp_path):
    from g_aegisbot.admin import Admin, run
    from g_aegisbot.db import get_session_factory
    from g_aegisbot.security.crypto import SecretBox, generate_key
    e = engine_at(tmp_path)
    init_db(e)
    _break(e, "drop index ix_user_profiles_auth_user_id")
    lines = []
    code = run(["migrate"], Admin(get_session_factory(e), SecretBox([generate_key()]), out=lines.append))
    assert code == 1 and any("NOTHING was changed" in x for x in lines) and "alembic_version" not in tables(e)


def test_downgrade_and_reupgrade_still_work_after_an_adopted_database(tmp_path):
    e = _unrecorded_at(tmp_path, "0002", "round.db")
    assert migrate.upgrade(e) == "0003"
    with e.begin() as conn:
        command.downgrade(migrate._config(e, conn), "0002")
    assert "keys_verified_at" not in _profile_cols(e)
    assert migrate.upgrade(e) == "0003" and "keys_verified_at" in _profile_cols(e)
