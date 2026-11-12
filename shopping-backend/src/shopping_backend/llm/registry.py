"""Model registry.

Order = fallback priority. With no API key the registry is empty and `enabled` is False, which is
what lets `AGENT_MODE=llm` degrade to `rule_based` instead of failing at import (G7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shopping_backend.core.config import Settings


@dataclass(frozen=True, slots=True)
class LLMConfig:
    name: str
    model: str
    temperature: float = 0.1
    max_output_tokens: int = 512


def build_registry(settings: Settings) -> LLMRegistry:
    return LLMRegistry(settings)


class LLMRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._configs: list[LLMConfig] = []
        if settings.llm_enabled:
            self._configs.append(LLMConfig(name="primary", model=settings.LLM_MODEL))
            if settings.LLM_FALLBACK_MODEL.strip():
                self._configs.append(LLMConfig(name="fallback", model=settings.LLM_FALLBACK_MODEL))

    @property
    def enabled(self) -> bool:
        return bool(self._configs)

    @property
    def configs(self) -> list[LLMConfig]:
        return list(self._configs)

    def chat_model(self, config: LLMConfig) -> Any:
        """Instantiate lazily so importing this module never requires the optional extra."""
        from langchain.chat_models import init_chat_model

        kwargs: dict[str, Any] = {
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
        }
        if self._settings.OPENAI_BASE_URL.strip():
            kwargs["openai_api_base"] = self._settings.OPENAI_BASE_URL
        if self._settings.OPENAI_API_KEY.strip():
            kwargs["api_key"] = self._settings.OPENAI_API_KEY
        provider, _, model = config.model.partition(":")
        if model:  # "anthropic:claude-..." style
            return init_chat_model(provider, model=model, **kwargs)
        return init_chat_model(config.model, **kwargs)
