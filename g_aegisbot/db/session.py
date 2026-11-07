from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from g_aegisbot.config import get_settings
from g_aegisbot.db.base import Base


def normalize_url(url: str) -> str:
    """Accept the postgres:// URLs Supabase shows and route them to the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = normalize_url(url or get_settings().database_url)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {"future": True}
    if url.startswith("postgresql"):
        kwargs["pool_pre_ping"] = True
        if ":6543" in url:  # Supabase transaction pooler (pgbouncer) has no prepared statements
            kwargs["connect_args"] = {"prepare_threshold": None}
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")          # a committed row (the order journal) survives power loss
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


def postgres_lockdown_sql(tables) -> list[str]:
    """Supabase exposes public tables through its REST API. Enable row-level security with no
    policies and revoke API roles, so the anon/authenticated keys can never read these tables
    (they hold encrypted exchange keys). The engine connects as the postgres role, which bypasses RLS."""
    stmts = []
    for t in tables:
        stmts.append(f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY')
        stmts.append(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE \"{t}\" FROM anon'; END IF; "
            "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE \"{t}\" FROM authenticated'; END IF; "
            "END $$"
        )
    return stmts


def init_db(engine: Engine | None = None) -> None:
    from g_aegisbot.db import models, trading_models  # noqa: F401  (register tables)

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for stmt in postgres_lockdown_sql(Base.metadata.tables):
                conn.execute(text(stmt))
