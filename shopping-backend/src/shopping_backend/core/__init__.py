"""Cross-cutting infrastructure: settings, logging, ids, errors."""

from shopping_backend.core.config import Settings, get_settings, settings
from shopping_backend.core.errors import (
    DEGRADED_FALLBACK_TEXT,
    ConfigError,
    GuardrailViolation,
    NotOffered,
    ShoppingError,
    Unavailable,
    error_code,
    friendly_message,
)
from shopping_backend.core.ids import get_trace_id, new_id, new_trace_id
from shopping_backend.core.logging import bind_request, clear_request_context, configure_logging, logger

__all__ = [
    "ConfigError",
    "DEGRADED_FALLBACK_TEXT",
    "GuardrailViolation",
    "NotOffered",
    "Settings",
    "ShoppingError",
    "Unavailable",
    "bind_request",
    "clear_request_context",
    "configure_logging",
    "error_code",
    "friendly_message",
    "get_settings",
    "get_trace_id",
    "logger",
    "new_id",
    "new_trace_id",
    "settings",
]
