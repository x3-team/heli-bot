import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.models import Base

config = context.config
target_metadata = Base.metadata

database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    kwargs: dict = {}
    if str(section.get("sqlalchemy.url", "")).startswith("sqlite"):
        # Deploys migrate while the API keeps running (schedule ticker, docs crawler write to the same file):
        # wait for their write lock instead of failing with «database is locked» after the default 5 s.
        kwargs["connect_args"] = {"timeout": 120}
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **kwargs,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
