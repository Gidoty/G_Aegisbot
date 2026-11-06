"""Alembic environment. The database URL comes from the caller (g_aegisbot.migrate) or AEGIS_DATABASE_URL."""
from alembic import context
from sqlalchemy import create_engine

from g_aegisbot.db import Base
from g_aegisbot.db import models, trading_models  # noqa: F401  (register every table)
from g_aegisbot.db.session import normalize_url

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        from g_aegisbot.config import get_settings
        url = get_settings().database_url
    return normalize_url(url)


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:                      # supplied by g_aegisbot.migrate
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
