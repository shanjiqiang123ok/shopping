"""Session context and the single time convention.

Timestamps are timezone-aware UTC everywhere. SQLModel's default column type (`UTCDateTime`)
enforces that, and it is the safer choice across dialects: the value written is always UTC wall
time regardless of the DB server's session timezone. Display conversion happens at the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalise anything that may arrive naive (JSON fixtures, callers, MySQL round-trips)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ShoppingSessionContext:
    user_id: str
    session_id: str
    tenant_id: str = "default"
    trace_id: str = ""
    now: datetime = field(default_factory=utcnow)

    @property
    def thread_id(self) -> str:
        """Checkpoint namespace; tenant-prefixed so multi-tenancy is an env change, not a refactor."""
        return f"{self.tenant_id}:{self.session_id}"

    def log_fields(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
        }
