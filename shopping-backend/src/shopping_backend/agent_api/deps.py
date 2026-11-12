"""Agent API dependencies: bearer/internal token, runtime access."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from shopping_backend.agent_api.runtime import AgentRuntime
from shopping_backend.core.config import Settings, get_settings


def get_runtime(request: Request) -> AgentRuntime:
    runtime: AgentRuntime = request.app.state.runtime
    if not runtime.started:
        raise HTTPException(status_code=503, detail="DB_UNAVAILABLE")
    return runtime


def require_token(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Verify the credential MaxKB passes through (frame.md: MaxKB owns user auth, we own caller auth).

    An unset `AGENT_INTERNAL_TOKEN` means "open", which is only defensible on a laptop.
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    expected = settings.AGENT_INTERNAL_TOKEN.strip()
    if not expected:
        return

    supplied = (x_internal_token or "").strip()
    if not supplied and authorization:
        scheme, _, credential = authorization.partition(" ")
        supplied = credential.strip() if scheme.lower() == "bearer" else authorization.strip()

    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
