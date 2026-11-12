"""The single place that knows about SQL dialects.

Switching SQLite -> MySQL/PostgreSQL touches only `DATABASE_URL` and this file; nothing above
the data layer may branch on dialect.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from shopping_backend.core.config import Settings

SQLITE_BUSY_TIMEOUT_MS = 5000


def create_engine_from_settings(settings: Settings, *, echo: bool = False, memory: bool = False) -> Engine:
    url = "sqlite:///:memory:" if memory else settings.resolved_database_url
    return create_engine_from_url(url, echo=echo)


def create_engine_from_url(url: str, *, echo: bool = False) -> Engine:
    dialect = make_url(url).get_backend_name()
    kwargs: dict[str, Any] = {"echo": echo, "future": True}

    if dialect == "sqlite":
        # check_same_thread: FastAPI runs sync endpoints on the threadpool.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool  # one shared connection, otherwise each view sees an empty db
        engine = create_engine(url, **kwargs)
        _attach_sqlite_pragmas(engine)
        return engine

    # MySQL / PostgreSQL: long-lived server connections need recycling and liveness checks.
    kwargs.update(pool_pre_ping=True, pool_recycle=3600, pool_size=5, max_overflow=10)
    return create_engine(url, **kwargs)


def _attach_sqlite_pragmas(engine: Engine) -> None:
    """WAL so the two services can share one file, plus busy_timeout and FK enforcement."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def describe_engine(engine: Engine) -> dict[str, Any]:
    url = engine.url
    info: dict[str, Any] = {
        "dialect": url.get_backend_name(),
        "url": url.render_as_string(hide_password=True),
    }
    try:
        with engine.connect() as conn:
            info["server_version"] = str(conn.engine.dialect.server_version_info)
            conn.execute(text("SELECT 1"))
        info["reachable"] = True
    except Exception as exc:  # health reporting must not raise
        info["reachable"] = False
        info["error"] = str(exc)[:200]
    return info


def is_sqlite(engine: Engine) -> bool:
    return engine.url.get_backend_name() == "sqlite"
