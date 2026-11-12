"""Session/transaction helpers. Read paths never hold a transaction open longer than a query."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session

from shopping_backend.core.config import settings
from shopping_backend.core.logging import logger
from shopping_backend.db.engine import create_engine_from_settings


def get_engine() -> Engine:
    return create_engine_from_settings(settings)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Yield a session, commit on success, rollback on error.

    `expire_on_commit=False` is deliberate: read paths hand ORM rows back to callers after the
    block ends (coupon ranking needs `coupon.valid_to`, preferences need `pref_key`). Without it
    every one of those accesses raises DetachedInstanceError.

    Callers in async code must wrap this with `asyncio.to_thread` (ADR A4: the business layer
    stays synchronous and reusable by both services and by tests).
    """
    own_engine = engine is None
    engine = engine or get_engine()
    with Session(engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if own_engine:
                session.close()


def health_check(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("db_health_check_failed", error=str(exc)[:200])
        return False


def table_counts(engine: Engine) -> dict[str, int]:
    from sqlmodel import func, select

    from shopping_backend.domain.models_orm import Coupon, Goods, StockRow

    counts: dict[str, int] = {}
    try:
        with Session(engine) as session:
            for model, key in ((Goods, "goods"), (StockRow, "stock"), (Coupon, "coupon")):
                counts[key] = int(session.exec(select(func.count()).select_from(model)).one())
    except Exception as exc:
        logger.warning("db_count_failed", error=str(exc)[:200])
    return counts
