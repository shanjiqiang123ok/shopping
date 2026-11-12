"""Agent service application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.engine import Engine

from shopping_backend import __version__
from shopping_backend.agent_api.routes import router
from shopping_backend.agent_api.runtime import AgentRuntime
from shopping_backend.core.config import Settings, get_settings
from shopping_backend.core.http import install_middleware_and_handlers
from shopping_backend.core.logging import configure_logging, logger
from shopping_backend.db.base import create_all
from shopping_backend.db.engine import create_engine_from_settings
from shopping_backend.seeds.loader import count_rows, load_seeds


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        settings.ensure_dirs()

        runtime: AgentRuntime = app.state.runtime
        db_engine = runtime.engine or app.state.engine
        runtime.engine = db_engine
        create_all(db_engine)
        if settings.SEED_ON_STARTUP and settings.is_dev and count_rows(db_engine).get("goods", 0) == 0:
            report = load_seeds(db_engine, reset=False)
            logger.info("startup_seeded", goods=report.goods, coupons=report.coupons)
        await runtime.startup()

        if settings.AGENT_MODE == "llm" and not settings.llm_enabled:
            logger.warning(
                "llm_mode_without_key", detail="falling back to rule_based; set OPENAI_API_KEY to enable the model"
            )
        if not settings.AGENT_INTERNAL_TOKEN.strip():
            logger.warning("agent_token_unset", detail="AGENT_INTERNAL_TOKEN is empty, the run endpoint is open")

        logger.info("agent_api_startup", **settings.safe_summary())
        yield
        await runtime.shutdown()
        logger.info("agent_api_shutdown")

    app = FastAPI(
        title="shopping-backend agent service",
        version=__version__,
        description="LangGraph 导购 Agent 服务，供 MaxKB 外部 HTTP 工具调用。",
        lifespan=lifespan,
    )
    default_engine = engine or create_engine_from_settings(settings)
    app.state.settings = settings
    app.state.engine = default_engine
    app.state.runtime = runtime or AgentRuntime(settings=settings, engine=default_engine)

    install_middleware_and_handlers(app, settings)
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    async def health(response: Response) -> dict[str, Any]:
        payload = await app.state.runtime.health()
        payload["version"] = __version__
        if payload["status"] != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return payload

    @app.get("/chat", include_in_schema=False)
    def chat_page() -> HTMLResponse:
        """Local debugging UI only. Kept out of the OpenAPI contract on purpose: that document is
        what gets imported into MaxKB as the external tool definition, and a chat page is not part
        of it. The real conversation UI belongs to MaxKB."""
        html = _CHAT_PAGE
        if html is None:
            return HTMLResponse(
                "<h3>chat.html 未随包安装</h3><p>请从仓库根目录启动服务，或重新执行 uv sync。</p>",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return HTMLResponse(html)

    @app.get("/", tags=["ops"])
    def index() -> dict[str, Any]:
        return {
            "service": "shopping-agent-api",
            "version": __version__,
            "run": "/api/shopping-agent/run",
            "chat": "/chat",
            "docs": "/docs",
            "health": "/health",
            "mode": settings.effective_mode,
            "backend_driver": settings.BACKEND_DRIVER,
        }

    return app


_CHAT_FILE = Path(__file__).resolve().parents[1] / "web" / "chat.html"
_CHAT_PAGE = _CHAT_FILE.read_text(encoding="utf-8") if _CHAT_FILE.is_file() else None


app = create_app()
