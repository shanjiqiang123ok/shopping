"""Business API dependencies: services from app.state, plus the internal-token check."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request
from sqlalchemy.engine import Engine

from shopping_backend.core.config import Settings, get_settings
from shopping_backend.services import Services


def _settings_of(request: Request) -> Settings:
    """Always read the *app's* settings: create_app(settings=...) must be honoured in tests."""
    return getattr(request.app.state, "settings", None) or get_settings()


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_services(request: Request) -> Services:
    return request.app.state.services


def require_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Service-to-service credential. Empty config means "open", which is only acceptable locally.

    This is *not* user auth: users are authenticated by MaxKB (frame.md boundary). Here we only
    refuse calls that did not come from a configured peer.
    """
    expected = _settings_of(request).BUSINESS_API_TOKEN.strip()
    if not expected:
        return
    supplied = (x_internal_token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
