"""Checkpoint storage.

`data/checkpoints.db` is deliberately a *separate* file from the business database: switching
business storage to MySQL must not force the checkpointer to move with it, and vice versa
(ADR A3). Postgres is the alternative for multi-replica deployments.

A missing or broken checkpointer is fatal at startup, never silently degraded: without it the
service loses cross-turn candidate memory and the resume path, so "running" would be a lie (the
same stance template-langgraph takes).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shopping_backend.core.config import Settings
from shopping_backend.core.errors import ConfigError


@dataclass(slots=True)
class CheckpointProvider:
    """`teardown` rather than a raw connection: sqlite closes a connection, postgres exits an
    async context manager, and memory does neither."""

    saver: Any
    kind: str
    detail: str = ""
    teardown: Callable[[], Awaitable[None]] | None = None

    async def setup(self) -> None:
        result = self.saver.setup() if hasattr(self.saver, "setup") else None
        if inspect.isawaitable(result):
            await result

    async def aclose(self) -> None:
        if self.teardown is not None:
            await self.teardown()
            self.teardown = None

    async def delete_thread(self, thread_id: str) -> bool:
        """Used by scripts/reset_db.py; SQLite and Postgres savers both expose a variant of this."""
        if hasattr(self.saver, "adelete_thread"):
            await self.saver.adelete_thread(thread_id)
            return True
        if hasattr(self.saver, "delete_thread"):
            self.saver.delete_thread(thread_id)
            return True
        return False

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.detail}


async def open_checkpoint_provider(settings: Settings) -> CheckpointProvider:
    kind = settings.CHECKPOINT_BACKEND

    if kind == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        provider = CheckpointProvider(saver=InMemorySaver(), kind="memory", detail="in-process")
        await provider.setup()
        return provider

    if kind == "sqlite":
        import aiosqlite

        path = settings.resolved_checkpoint_db_path
        conn = await aiosqlite.connect(str(path))
        # WAL: the agent process and scripts/ share this file; default rollback journalling would
        # hit "database is locked" under concurrent turns.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        provider = CheckpointProvider(
            saver=AsyncSqliteSaver(conn),
            kind="sqlite",
            detail=str(path),
            teardown=conn.close,
        )
        await provider.setup()
        return provider

    if kind == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:
            msg = "CHECKPOINT_BACKEND=postgres needs: uv sync --extra postgres"
            raise ConfigError(msg) from exc

        if not settings.POSTGRES_CHECKPOINT_URL.strip():
            msg = "CHECKPOINT_BACKEND=postgres requires POSTGRES_CHECKPOINT_URL"
            raise ConfigError(msg)

        context = AsyncPostgresSaver.from_conn_string(settings.POSTGRES_CHECKPOINT_URL)
        saver = await context.__aenter__()
        provider = CheckpointProvider(
            saver=saver,
            kind="postgres",
            detail="postgres checkpoint",
            teardown=lambda: context.__aexit__(None, None, None),
        )
        await provider.setup()
        return provider

    msg = f"unknown CHECKPOINT_BACKEND: {kind!r} (expected sqlite | postgres | memory)"
    raise ConfigError(msg)


def build_sync_checkpointer() -> Any:
    """In-memory saver for unit tests that assemble a graph without any file IO."""
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()
