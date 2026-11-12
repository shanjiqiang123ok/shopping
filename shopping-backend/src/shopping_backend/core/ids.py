"""Trace identity helpers.

`trace_id` is minted at the API edge and travels through the graph, the backends and the logs;
`new_id` is the dialect-neutral primary key generator (String(36), never AUTOINCREMENT).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def new_trace_id() -> str:
    return f"t_{uuid.uuid4().hex[:10]}"


def set_trace_id(value: str | None) -> Token[str | None]:
    return _trace_id.set(value or new_trace_id())


def get_trace_id() -> str | None:
    return _trace_id.get()


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id.reset(token)
