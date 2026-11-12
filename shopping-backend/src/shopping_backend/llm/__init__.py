"""Model access: registry + a resilience wrapper. No key means no model, never a crash."""

from shopping_backend.llm.registry import LLMRegistry, build_registry
from shopping_backend.llm.service import BaseLLM, LLMService, NullLLM, build_llm

__all__ = ["BaseLLM", "LLMRegistry", "LLMService", "NullLLM", "build_llm", "build_registry"]
