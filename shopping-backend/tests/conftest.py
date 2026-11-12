"""Shared fixtures: a SQLite database loaded with the demo fixtures, plus the test doubles in
`tests/fakes.py`. The whole suite runs with no network, no API key and no Docker.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from shopping_backend.core.config import Settings
from shopping_backend.db.base import create_all
from shopping_backend.db.engine import create_engine_from_url
from shopping_backend.seeds.loader import load_seeds
from shopping_backend.services import Services, build_services


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
        DATA_DIR=tmp_path / "data",
        LOG_DIR=tmp_path / "logs",
        CHECKPOINT_BACKEND="memory",
        CHECKPOINT_DB_PATH=tmp_path / "checkpoints.db",
        BACKEND_DRIVER="inprocess",
        AGENT_MODE="rule_based",
        AGENT_INTERNAL_TOKEN="test-token",
        BUSINESS_API_TOKEN="",
        SEED_ON_STARTUP=False,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    eng = create_engine_from_url(settings.resolved_database_url)
    create_all(eng)
    load_seeds(eng, reset=True)
    yield eng
    eng.dispose()


@pytest.fixture
def mem_engine() -> Iterator[Engine]:
    eng = create_engine_from_url("sqlite:///:memory:")
    create_all(eng)
    load_seeds(eng, reset=True)
    yield eng
    eng.dispose()


@pytest.fixture
def services(engine: Engine) -> Services:
    return build_services(engine)
