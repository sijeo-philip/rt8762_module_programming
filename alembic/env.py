"""Alembic migration environment for the station database."""

from __future__ import annotations
import os

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from stationapp.config import get_settings
from stationapp.data.base import Base

# Import models so every mapped table is registered with Base.metadata.
from stationapp.data import models  # noqa: F401


config = context.config


if config.config_file_name is not None:
    fileConfig(config.config_file_name)


settings = get_settings()

#configured_url = config.get_main_option("sqlalchemy.url")

#if(not configured_url or configured_url == "driver://user:pass@localhost/dbname"):
#    config.set_main_option("sqlalchemy.url", settings.database_url)
database_url = os.getenv("STATIONAPP_MIGRATION_DATABASE_URL", settings.database_url)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating a live DB connection."""

    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={
            "paramstyle": "named",
        },
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using a live database connection."""

    connectable = engine_from_config(
        config.get_section(
            config.config_ini_section,
            {}
        ),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()