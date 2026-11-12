"""M1 verification: the dialect-neutrality rules that make the MySQL switch cheap.

docs/EXTENSION_DB_DOCKER.md §2 lists nine portability rules; the mechanically checkable ones are
asserted here, so a future column cannot quietly break the switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from shopping_backend.core.config import Settings
from shopping_backend.db.base import create_all
from shopping_backend.db.engine import create_engine_from_settings, create_engine_from_url, describe_engine, is_sqlite
from shopping_backend.db.session import health_check, table_counts
from shopping_backend.domain.models_orm import TABLE_MODELS

MONEY_COLUMN_SUFFIX = "_cents"


def _columns() -> list[sa.Column]:
    return [column for table in SQLModel.metadata.tables.values() for column in table.columns]


@pytest.fixture
def file_engine(tmp_path: Path) -> Engine:
    settings = Settings(APP_ENV="test", DATABASE_URL=f"sqlite:///{(tmp_path / 'biz.db').as_posix()}")
    engine = create_engine_from_settings(settings)
    create_all(engine)
    yield engine
    engine.dispose()


def test_settings_absoluteizes_sqlite_path(tmp_path: Path) -> None:
    settings = Settings(APP_ENV="test", DATABASE_URL="sqlite:///./data/business.db")
    resolved = settings.resolved_database_url
    assert resolved.startswith("sqlite:///")
    assert Path(resolved.removeprefix("sqlite:///")).is_absolute()


def test_dialect_reported(file_engine: Engine) -> None:
    assert is_sqlite(file_engine)
    assert describe_engine(file_engine)["dialect"] == "sqlite"
    assert describe_engine(file_engine)["reachable"] is True


def _pragma(engine: Engine, statement: str):
    with engine.connect() as conn:
        return conn.exec_driver_sql(statement).scalar()


def test_sqlite_wal_and_busy_timeout(file_engine: Engine) -> None:
    """Two services share one file: WAL plus busy_timeout is what prevents 'database is locked'."""
    assert str(_pragma(file_engine, "PRAGMA journal_mode")).lower() == "wal"
    assert int(_pragma(file_engine, "PRAGMA busy_timeout")) >= 5000


def test_memory_engine_is_reachable(mem_engine: Engine) -> None:
    assert health_check(mem_engine) is True


def test_counts_after_seeding(engine: Engine) -> None:
    counts = table_counts(engine)
    assert counts["goods"] == 12
    assert counts["stock"] == 11
    assert counts["coupon"] == 9


def _rendered(column: sa.Column) -> list[tuple[str, str]]:
    """Compiled DDL type per dialect — what actually gets created, not the Python class name
    (SQLModel wraps strings in `AutoString` and datetimes in `UTCDateTime`)."""
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    return [
        ("mysql", str(column.type.compile(dialect=mysql.dialect())).upper()),
        ("postgres", str(column.type.compile(dialect=postgresql.dialect())).upper()),
        ("sqlite", str(column.type.compile(dialect=sqlite.dialect())).upper()),
    ]


def test_money_columns_are_integers() -> None:
    offenders = [
        column.name
        for column in _columns()
        if column.name.endswith(MONEY_COLUMN_SUFFIX)
        and not all(rendered in ("INTEGER", "BIGINT") for _, rendered in _rendered(column))
    ]
    assert offenders == [], f"money columns must be INTEGER cents: {offenders}"


def test_no_float_columns_anywhere() -> None:
    offenders = [
        column.name
        for column in _columns()
        if any(
            any(bad in rendered for bad in ("FLOAT", "DOUBLE", "NUMERIC", "DECIMAL", "REAL"))
            for _, rendered in _rendered(column)
        )
    ]
    assert offenders == [], "floats are forbidden: rounding drift would corrupt price comparisons"


def test_primary_keys_are_strings_without_autoincrement() -> None:
    for table in SQLModel.metadata.tables.values():
        for column in table.primary_key.columns:
            assert all(rendered.startswith("VARCHAR") for _, rendered in _rendered(column)), (
                f"{table.name}.{column.name} must be VARCHAR"
            )
            assert column.type.length in (36, 64), f"{table.name}.{column.name} length {column.type.length}"
            assert column.autoincrement is not True, f"{table.name}.{column.name} must not autoincrement"


def test_no_dialect_specific_types() -> None:
    """Rule 3: JSON/ARRAY/etc. would break the MySQL switch; Text + app-level json instead."""
    offenders = [
        f"{column.name}:{type(column.type).__name__}"
        for column in _columns()
        if type(column.type).__module__.startswith("sqlalchemy.dialects")
    ]
    assert offenders == [], f"dialect-specific column types found: {offenders}"


def test_no_native_enum_columns() -> None:
    offenders = [
        column.name
        for column in _columns()
        if isinstance(column.type, sa.Enum) or any("ENUM" in rendered for _, rendered in _rendered(column))
    ]
    assert offenders == [], "enums are VARCHAR + application validation (rule 5)"


def test_indexed_string_columns_fit_mysql_utf8mb4() -> None:
    """utf8mb4 index prefix limit is 767 bytes on the older InnoDB row format (rule 7 / §3.6)."""
    offenders: list[str] = []
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            for column in index.columns:
                if isinstance(column.type, sa.String) and column.type.length and column.type.length * 4 > 767:
                    offenders.append(f"{table.name}.{column.name}({column.type.length})")
    assert offenders == [], f"indexed VARCHAR too wide for utf8mb4: {offenders}"


def test_sql_keywords_avoided_in_column_names() -> None:
    reserved = {"order", "group", "key", "value", "select", "from", "table", "index"}
    offenders = [column.name for column in _columns() if column.name in reserved]
    assert offenders == [], f"rename to avoid reserved words (rule 8): {offenders}"


def test_index_names_follow_convention() -> None:
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            assert index.name.startswith(("ix_", "uq_")), f"{table.name}: {index.name}"


def test_create_all_is_idempotent(mem_engine: Engine) -> None:
    create_all(mem_engine)  # second call must not raise
    assert set(SQLModel.metadata.tables) >= {"goods", "stock", "coupon", "user_preference", "agent_request_log"}


def test_wal_is_not_applied_to_memory_db() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    mode = str(_pragma(engine, "PRAGMA journal_mode")).lower()
    assert mode in {"memory", "wal"}


def test_foreign_keys_are_enforced(file_engine: Engine) -> None:
    assert int(_pragma(file_engine, "PRAGMA foreign_keys")) == 1


def test_models_cover_expected_tables() -> None:
    assert {model.__tablename__ for model in TABLE_MODELS} == {
        "goods",
        "stock",
        "coupon",
        "user_preference",
        "agent_request_log",
    }
