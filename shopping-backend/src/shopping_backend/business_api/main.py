"""Business API application factory.

`create_app(engine=...)` exists so tests can inject an in-memory database instead of the file in
`data/`; uvicorn uses the no-argument form via `shopping_backend.business_api.main:app`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from sqlalchemy.engine import Engine

from shopping_backend import __version__
from shopping_backend.business_api.routes import router
from shopping_backend.core.config import Settings, get_settings
from shopping_backend.core.http import install_middleware_and_handlers
from shopping_backend.core.logging import configure_logging, logger
from shopping_backend.db.base import create_all
from shopping_backend.db.engine import create_engine_from_settings, describe_engine
from shopping_backend.db.session import health_check, table_counts
from shopping_backend.seeds.loader import count_rows, load_seeds
from shopping_backend.services import Services, build_services


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        settings.ensure_dirs()
        create_all(app.state.engine)
        if settings.SEED_ON_STARTUP and settings.is_dev:
            existing = count_rows(app.state.engine)
            if existing.get("goods", 0) == 0:
                report = load_seeds(app.state.engine, reset=False)
                logger.info("startup_seeded", goods=report.goods, coupons=report.coupons)
        logger.info("business_api_startup", **settings.safe_summary())
        yield
        logger.info("business_api_shutdown")

    app = FastAPI(
        title="shopping-backend mock business API",
        version=__version__,
        description="商品/库存/优惠券业务接口，充当 frame.md 中 Java 后端的位置。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine or create_engine_from_settings(settings)
    app.state.services = build_services(app.state.engine)
    install_middleware_and_handlers(app, settings)
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    def health(response: Response) -> dict:
        reachable = health_check(app.state.engine)
        if not reachable:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if reachable else "degraded",
            "version": __version__,
            "db": "ok" if reachable else "unavailable",
            "engine": describe_engine(app.state.engine),
            "rows": table_counts(app.state.engine),
        }

    @app.get("/", tags=["ops"])
    def index() -> dict:
        return {"service": "business-api", "version": __version__, "docs": "/docs", "health": "/health"}

    return app


def get_services_of(app: FastAPI) -> Services:
    return app.state.services  # type: ignore[attr-defined]


app = create_app()
