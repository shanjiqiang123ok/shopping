"""Shared test doubles. Kept out of conftest so tests import them explicitly."""

from __future__ import annotations

from typing import Any

from shopping_backend.backends.base import StorefrontBackend
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.schemas import GoodsQuery


class FakeLLM:
    """Stands in for LLMService so llm-mode branches are testable without a key (plan §9 risk 4)."""

    enabled = True

    def __init__(
        self, decisions: list[dict[str, Any] | None] | None = None, reasons: dict[str, str] | None = None
    ) -> None:
        self.decisions = list(decisions or [])
        self.reasons = reasons or {}
        self.calls: list[dict[str, Any]] = []

    async def call_structured(self, schema: Any, *, system: str = "", user: str = "") -> Any:
        self.calls.append({"kind": "structured", "system": system, "user": user})
        payload = self.decisions.pop(0) if self.decisions else None
        if payload is None:
            return None
        return schema.model_validate(payload)

    async def reasons_for(self, *, query: str, goods_rows: list[dict], preference_block: str = "") -> dict[str, str]:
        self.calls.append({"kind": "reasons", "query": query, "ids": [row.get("id") for row in goods_rows]})
        allowed = {str(row.get("id")) for row in goods_rows}
        return {key: value for key, value in self.reasons.items() if key in allowed}


class RaisingBackend(StorefrontBackend):
    """F7 probe at the adapter boundary.

    It overrides the *protocol* methods, not the public ones, so the exception has to travel
    through `StorefrontBackend._guarded` — exactly the path a real driver takes.
    """

    driver = "raising"

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("business API is down")

    async def _fetch_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> Any:
        raise self._exc

    async def _fetch_stock(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> Any:
        raise self._exc

    async def _fetch_coupon(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> Any:
        raise self._exc
