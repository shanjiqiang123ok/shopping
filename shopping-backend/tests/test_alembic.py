"""M6 verification: the migration path that a MySQL switch will actually use.

Two things are asserted, because `create_all` in dev can hide a broken migration for months:
`alembic upgrade head` produces every table, and the resulting schema has no drift against the
models (so a future autogenerate diff means a real change, not a missed baseline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import autogenerate, command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import create_engine

from shopping_backend.core.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"goods", "stock", "coupon", "user_preference", "agent_request_log"}


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Point Settings at a fresh file DB, with the settings cache cleared so env.py sees it."""
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    yield Settings()
    get_settings.cache_clear()


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_upgrade_head_creates_every_table(alembic_config: Settings, tmp_path: Path) -> None:
    command.upgrade(_config(), "head")

    engine = create_engine(alembic_config.resolved_database_url)
    found = set(inspect(engine).get_table_names())
    assert found >= EXPECTED_TABLES, f"missing {EXPECTED_TABLES - found}"
    engine.dispose()


def test_upgrade_is_idempotent(alembic_config: Settings) -> None:
    config = _config()
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # dev may already have run create_all; must not error

    engine = create_engine(alembic_config.resolved_database_url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    engine.dispose()


def test_no_drift_between_migration_and_models(alembic_config: Settings) -> None:
    from shopping_backend.db.base import metadata as target_metadata

    command.upgrade(_config(), "head")

    engine = create_engine(alembic_config.resolved_database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection=connection)
        diffs = autogenerate.compare_metadata(context, target_metadata)
    engine.dispose()

    structural = [diff for diff in diffs if diff[0] in ("add_table", "remove_table", "add_column", "remove_column")]
    assert structural == [], f"migration schema drifted from the models: {json.dumps(structural, default=str)[:800]}"


def test_downgrade_removes_the_tables(alembic_config: Settings) -> None:
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(alembic_config.resolved_database_url)
    remaining = EXPECTED_TABLES & set(inspect(engine).get_table_names())
    engine.dispose()
    assert remaining == set()
