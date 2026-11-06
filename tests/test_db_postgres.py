"""Postgres checks that need no live database."""
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.session import normalize_url, postgres_lockdown_sql


def test_url_normalization():
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_ddl_compiles_for_postgres_with_jsonb_and_checks():
    ddl = str(CreateTable(UserProfile.__table__).compile(dialect=postgresql.dialect()))
    assert "JSONB" in ddl and "ck_leverage_range" in ddl and "ck_trading_mode" in ddl


def test_lockdown_enables_rls_and_revokes_api_roles():
    stmts = postgres_lockdown_sql(["user_profiles"])
    assert stmts[0] == 'ALTER TABLE "user_profiles" ENABLE ROW LEVEL SECURITY'
    assert "REVOKE ALL" in stmts[1] and "anon" in stmts[1] and "authenticated" in stmts[1]
