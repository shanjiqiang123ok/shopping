"""Dependency container for the graph.

Nodes are built as closures over this container (`make_goods_worker(deps)`), so:

* tests inject fakes without monkeypatching module globals;
* nothing non-serialisable ever lands in `config["configurable"]`, which the checkpointer writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine

from shopping_backend.backends.base import StorefrontBackend, build_backend
from shopping_backend.core.config import Settings, get_settings
from shopping_backend.llm.service import BaseLLM, build_llm
from shopping_backend.services import Services, build_services


@dataclass(slots=True)
class GraphDeps:
    settings: Settings
    backend: StorefrontBackend
    llm: BaseLLM
    services: Services | None = None

    async def aclose(self) -> None:
        await self.backend.aclose()

    def describe(self) -> dict[str, Any]:
        registry = getattr(self.llm, "registry", None)
        return {
            "mode": self.settings.effective_mode,
            "backend": self.backend.driver,
            "llm_enabled": self.llm.enabled,
            "models": [config.model for config in getattr(registry, "configs", [])],
        }


def build_deps(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    backend: StorefrontBackend | None = None,
    llm: BaseLLM | None = None,
    services: Services | None = None,
) -> GraphDeps:
    settings = settings or get_settings()
    if services is None and (engine is not None or settings.BACKEND_DRIVER == "inprocess"):
        from shopping_backend.db.engine import create_engine_from_settings

        services = build_services(engine or create_engine_from_settings(settings))

    resolved_backend = backend or build_backend(settings, services=services)
    return GraphDeps(
        settings=settings,
        backend=resolved_backend,
        llm=llm if llm is not None else build_llm(settings),
        services=services,
    )
