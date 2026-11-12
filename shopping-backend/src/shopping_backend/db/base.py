"""Shared metadata with an explicit naming convention.

Fixed index/constraint names keep generated DDL identical across SQLite/MySQL/PostgreSQL, which
is what makes `alembic revision --autogenerate` produce portable migrations.
"""

from __future__ import annotations

from sqlmodel import SQLModel

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

SQLModel.metadata.naming_convention = naming_convention  # type: ignore[attr-defined]

metadata = SQLModel.metadata


def create_all(engine) -> None:
    """Dev/test convenience. Production switches to `alembic upgrade head` (see docs)."""
    metadata.create_all(engine)
