"""LLM wrapper: timeout, bounded retries, structured output, and a no-model twin.

The contract with callers is "you always get a usable answer or None": the graph must keep running
when the model is slow, wrong or absent, so every failure path returns a fallback instead of
raising (F7 applies to the model too, not just to business data).
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from pydantic import BaseModel, Field

from shopping_backend.core.config import Settings
from shopping_backend.core.logging import logger
from shopping_backend.llm.registry import LLMRegistry, build_registry


class ReasonItem(BaseModel):
    goods_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(default="", max_length=80)


class ReasonsPayload(BaseModel):
    """Per-product justification. Numbers are refused downstream (O9), prose only here."""

    items: list[ReasonItem] = Field(default_factory=list, max_length=8)


class BaseLLM(Protocol):
    enabled: bool

    async def call_structured(self, schema: type[BaseModel], *, system: str, user: str) -> Any | None: ...

    async def reasons_for(
        self, *, query: str, goods_rows: list[dict], preference_block: str = ""
    ) -> dict[str, str]: ...


class NullLLM:
    """What `rule_based` mode uses: no network, no model, no reasons, no exceptions."""

    enabled = False

    async def call_structured(self, schema: type[BaseModel], *, system: str, user: str) -> Any | None:
        return None

    async def reasons_for(self, *, query: str, goods_rows: list[dict], preference_block: str = "") -> dict[str, str]:
        return {}


class LLMService:
    def __init__(self, settings: Settings, registry: LLMRegistry | None = None) -> None:
        self._settings = settings
        self._registry = registry or build_registry(settings)

    @property
    def enabled(self) -> bool:
        return self._registry.enabled

    @property
    def registry(self) -> LLMRegistry:
        return self._registry

    async def call_structured(self, schema: type[BaseModel], *, system: str, user: str) -> Any | None:
        if not self.enabled:
            return None

        configs = self._registry.configs
        if not configs:
            return None
        models = len(configs)
        max_attempts = max(1, self._settings.MAX_LLM_CALL_RETRIES + 1) * models
        last_error: str = "no attempt made"

        for attempt in range(1, max_attempts + 1):
            config = configs[(attempt - 1) % models]  # cycle: retry primary, then fall through the list
            try:
                model = self._registry.chat_model(config)
                structured = model.with_structured_output(schema)
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_invoke, structured, system, user), timeout=self._settings.LLM_TIMEOUT_S
                )
                return raw if isinstance(raw, schema) else schema.model_validate(raw)
            except Exception as exc:  # timeout, rate limit, malformed output, missing optional extra
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "llm_attempt_failed", model=config.model, attempt=attempt, of=max_attempts, error=last_error[:200]
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2.0, 0.3 * 2 ** (attempt - 1)))

        logger.error("llm_unavailable", schema=schema.__name__, error=last_error[:200])
        return None

    async def reasons_for(self, *, query: str, goods_rows: list[dict], preference_block: str = "") -> dict[str, str]:
        if not self.enabled or not goods_rows:
            return {}

        lines = [
            f"- {row.get('id')}: {row.get('title')} ¥{(row.get('price_cents') or 0) / 100:.2f}"
            for row in goods_rows[:8]
        ]
        system = (
            "你是电商导购助手。为每个候选商品写一句推荐理由（不超过 30 字），"
            "只描述它与用户需求（预算、品类、用途）的匹配程度。"
            "禁止在理由中出现任何价格、库存数量、折扣数字或券码；禁止编造商品。"
            f"\n用户需求：{query}"
        )
        if preference_block:
            system = f"{system}\n已知偏好：{preference_block}"
        user = "候选商品：\n" + "\n".join(lines) + "\n请为每个 goods_id 给出一句理由。"

        payload = await self.call_structured(ReasonsPayload, system=system, user=user)
        if payload is None:
            return {}
        allowed_ids = {str(row.get("id")) for row in goods_rows}
        return {item.goods_id: item.reason for item in payload.items if item.goods_id in allowed_ids and item.reason}


def _invoke(structured: Any, system: str, user: str) -> Any:
    """langchain's `.invoke` is sync; run it in a thread so the event loop stays responsive."""
    return structured.invoke([("system", system), ("human", user)])


def build_llm(settings: Settings) -> BaseLLM:
    """rule_based (or keyless) mode gets NullLLM, so no node needs an `if settings...` branch."""
    if settings.effective_mode != "llm":
        return NullLLM()
    service = LLMService(settings)
    return service if service.enabled else NullLLM()
