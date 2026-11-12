"""Persistence plumbing. `engine.py` is the only place that knows about SQL dialects."""

from shopping_backend.db.base import metadata, naming_convention
from shopping_backend.db.engine import create_engine_from_settings, describe_engine
from shopping_backend.db.session import session_scope

__all__ = ["create_engine_from_settings", "describe_engine", "metadata", "naming_convention", "session_scope"]
