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


class SchemaInconsistent(RuntimeError):
    """A database without a migration record whose schema matches no known revision. Nothing was changed."""


# What each revision ADDED to the baseline (0001), so the schema a database must have at any revision is derivable from the
# current models: take the models and remove everything added by later revisions.
_REVISIONS = ("0001", "0002", "0003")
_ADDED = {
    "0002": ({"user_profiles": {"auth_user_id", "login_email"}, "open_trades": {"strategy", "trail_atr_mult"},
              "trade_log": {"strategy"}, "paper_positions": {"strategy"}},
             {"ix_user_profiles_auth_user_id", "ix_user_profiles_login_email"}),
    "0003": ({"user_profiles": {"keys_verified_at"}}, set()),
}


def schema_problems(insp, revision: str) -> list[str]:
    """Every way the database's schema differs from what `revision` creates: missing tables, missing or unexpected columns,
    nullability, missing or unexpected indexes, missing or unexpected named check constraints. Empty list = an exact match.
    Read-only. Extra tables the application does not know about are ignored."""
    from sqlalchemy import CheckConstraint

    from g_aegisbot.db import models, trading_models  # noqa: F401  (register every table)
    from g_aegisbot.db.base import Base
    later = _REVISIONS[_REVISIONS.index(revision) + 1:]
    drop_cols: dict[str, set[str]] = {}
    drop_idx: set[str] = set()
    for rev in later:
        for table, cols in _ADDED[rev][0].items():
            drop_cols.setdefault(table, set()).update(cols)
        drop_idx |= _ADDED[rev][1]
    present = set(insp.get_table_names())
    problems: list[str] = []
    for t in Base.metadata.sorted_tables:
        if t.name not in present:
            problems.append(f"table {t.name} is missing")
            continue
        actual = {c["name"]: c for c in insp.get_columns(t.name)}
        expected = {c.name: c for c in t.columns if c.name not in drop_cols.get(t.name, ())}
        for name in sorted(set(expected) - set(actual)):
            problems.append(f"{t.name}.{name} is missing")
        for name in sorted(set(actual) - set(expected)):
            problems.append(f"{t.name}.{name} is not part of revision {revision}")
        for name in sorted(set(expected) & set(actual)):
            if not expected[name].primary_key and bool(actual[name]["nullable"]) != bool(expected[name].nullable):
                problems.append(f"{t.name}.{name} nullability differs")
        want_idx = {i.name for i in t.indexes if i.name and i.name not in drop_idx}
        have_idx = {i["name"] for i in insp.get_indexes(t.name) if i.get("name")}
        for name in sorted(want_idx - have_idx):
            problems.append(f"index {name} is missing")
        for name in sorted(have_idx - want_idx):
            problems.append(f"index {name} is not part of revision {revision}")
        want_ck = {c.name for c in t.constraints if isinstance(c, CheckConstraint) and c.name}
        have_ck = {c["name"] for c in insp.get_check_constraints(t.name) if c.get("name")}
        for name in sorted(want_ck - have_ck):
            problems.append(f"check constraint {name} is missing")
        for name in sorted(have_ck - want_ck):
            problems.append(f"check constraint {name} is not part of revision {revision}")
    return problems


def detect_revision(insp) -> str:
    """The newest revision whose schema the database matches EXACTLY. Raises SchemaInconsistent (changing nothing) if none."""
    found: dict[str, list[str]] = {}
    for rev in reversed(_REVISIONS):
        problems = schema_problems(insp, rev)
        if not problems:
            return rev
        found[rev] = problems
    closest = min(found, key=lambda r: len(found[r]))
    shown = "; ".join(found[closest][:8]) + (" ..." if len(found[closest]) > 8 else "")
    raise SchemaInconsistent(
        "The database has tables but no migration record, and its schema matches none of the known revisions "
        f"({', '.join(_REVISIONS)}). It is partially migrated or was changed by hand. NOTHING was changed or stamped. "
        f"Closest is revision {closest}, which differs in: {shown}. Restore a backup or repair the schema by hand, then retry.")


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
        insp = inspect(conn)
        names = set(insp.get_table_names())
        if "alembic_version" not in names and "user_profiles" in names:
            # Built by create_all before migrations were tracked. Stamp a revision only if the WHOLE schema matches it (tables,
            # columns, indexes, check constraints); one telltale column proves nothing. Otherwise refuse and change nothing.
            command.stamp(cfg, detect_revision(insp))
        command.upgrade(cfg, "head")
    return current_revision(engine) or ""


def _stamp_head(engine: Engine) -> None:
    with engine.begin() as conn:
        command.stamp(_config(engine, conn), "head")


def ensure_schema(engine: Engine) -> None:
    """Called by every entrypoint. SQLite: create tables. Anything else: demand an up-to-date schema."""
    if engine.dialect.name == "sqlite":
        if inspect(engine).get_table_names():
            upgrade(engine)                       # an existing local database is migrated, never assumed current
        else:
            from g_aegisbot.db import init_db
            init_db(engine)
            _stamp_head(engine)                   # so later migrations apply to it
        return
    cur, head = current_revision(engine), head_revision(engine)
    if cur != head:
        raise SchemaOutOfDate(f"The database schema is at revision {cur or 'none'} but the code needs {head}. "
                              "Back up the database, then run: python -m g_aegisbot.admin migrate")
