"""Alembic environment.

Two things differ from the template project this borrows its shape from:

1. the URL comes from `Settings` (so SQLite/MySQL/PostgreSQL is one env var) and is passed straight
   to `context.configure`, never written into alembic.ini where `%` in a password would break
   ConfigParser interpolation;
2. checkpoint tables are *not* managed here — they belong to langgraph-checkpoint, which runs its
   own `setup()`. They never enter `SQLModel.metadata`, so autogenerate cannot see them.
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from shopping_backend.core.config import get_settings
from shopping_backend.db.base import metadata as target_metadata  # SQLModel.metadata
from shopping_backend.domain import models_orm  # noqa: F401 - registers every table on the metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
database_url = settings.resolved_database_url
is_sqlite = database_url.startswith("sqlite")

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # the ini deliberately leaves sqlalchemy.url empty; supply the resolved one
        url=database_url,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
