"""Schema management.

SQLite (development): tables are created directly. Postgres (Supabase, production): the schema is
changed only by Alembic migrations, and every entrypoint refuses to start if the database is not at
the latest revision, so code and schema can never silently drift apart.

    python -m g_aegisbot.admin migrate        # bring the database up to date
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaOutOfDate(RuntimeError):
    pass


def _config(engine: Engine, connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    cfg.attributes["connection"] = connection
    return cfg


def head_revision(engine: Engine) -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(engine: Engine) -> str | None:
    from alembic.migration import MigrationContext
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine: Engine) -> str:
    """Migrate to the latest revision. A database made by create_all before migrations existed is
    adopted (stamped at the baseline) first. Returns the resulting revision."""
    with engine.begin() as conn:
        cfg = _config(engine, conn)
        names = set(inspect(conn).get_table_names())
        if "alembic_version" not in names and "user_profiles" in names:
            command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")
    return current_revision(engine) or ""


def ensure_schema(engine: Engine) -> None:
    """Called by every entrypoint. SQLite: create tables. Anything else: demand an up-to-date schema."""
    if engine.dialect.name == "sqlite":
        from g_aegisbot.db import init_db
        init_db(engine)
        return
    cur, head = current_revision(engine), head_revision(engine)
    if cur != head:
        raise SchemaOutOfDate(f"The database schema is at revision {cur or 'none'} but the code needs {head}. "
                              "Back up the database, then run: python -m g_aegisbot.admin migrate")
