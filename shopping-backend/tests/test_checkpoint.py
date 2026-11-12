"""M4 verification: checkpoint persistence across turns and across process restarts (G6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from shopping_backend.backends.inprocess import InProcessBackend
from shopping_backend.core.config import Settings
from shopping_backend.core.errors import ConfigError
from shopping_backend.graph.builder import compile_graph, graph_config
from shopping_backend.graph.checkpoint import build_sync_checkpointer, open_checkpoint_provider
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import ShoppingAgentState
from shopping_backend.llm.service import NullLLM
from shopping_backend.services import build_services


def _deps(settings: Settings, engine: Engine) -> GraphDeps:
    services = build_services(engine)
    return GraphDeps(settings=settings, backend=InProcessBackend(services), llm=NullLLM(), services=services)


async def _invoke(graph, query: str, thread_id: str) -> dict:
    state = ShoppingAgentState(user_id="u_1001", session_id="s_ckpt", query=query, trace_id="t_ckpt")
    return await graph.ainvoke(state.as_input(), graph_config(thread_id, trace_id="t_ckpt"))


@pytest.mark.asyncio
async def test_sqlite_provider_creates_file(tmp_path: Path, mem_engine: Engine) -> None:
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="sqlite", CHECKPOINT_DB_PATH=tmp_path / "cp.db")
    provider = await open_checkpoint_provider(settings)
    try:
        assert provider.describe()["kind"] == "sqlite"
        assert (tmp_path / "cp.db").exists()

        graph = compile_graph(_deps(settings, mem_engine), provider.saver)
        result = await _invoke(graph, "推荐 2000 元以内的手机", "default:s_ckpt")
        assert result["route_trace"][-1] == "summarize"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_state_survives_provider_reopen(tmp_path: Path, mem_engine: Engine) -> None:
    """Reopen the file the way a restarted service would: turn 2 must still see turn 1's candidates."""
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="sqlite", CHECKPOINT_DB_PATH=tmp_path / "cp2.db")
    thread = "default:s_reopen"

    first_provider = await open_checkpoint_provider(settings)
    graph = compile_graph(_deps(settings, mem_engine), first_provider.saver)
    first = await _invoke(graph, "推荐 2000 元以内的手机", thread)
    seen = list(first["seen_goods_ids"])
    await first_provider.aclose()
    assert len(seen) == 3

    second_provider = await open_checkpoint_provider(settings)
    try:
        snapshot = await compile_graph(_deps(settings, mem_engine), second_provider.saver).aget_state(
            graph_config(thread)
        )
        assert sorted(snapshot.values["seen_goods_ids"]) == sorted(seen)
        assert len(snapshot.values["candidate_memory"]) == 3

        second = await _invoke(
            compile_graph(_deps(settings, mem_engine), second_provider.saver),
            "第二款有货吗",
            thread,
        )
        assert second["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]
        assert second["goods_list"][0]["id"] in seen
    finally:
        await second_provider.aclose()


@pytest.mark.asyncio
async def test_thread_isolation(tmp_path: Path, mem_engine: Engine) -> None:
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="sqlite", CHECKPOINT_DB_PATH=tmp_path / "cp3.db")
    provider = await open_checkpoint_provider(settings)
    try:
        graph = compile_graph(_deps(settings, mem_engine), provider.saver)
        await _invoke(graph, "推荐 2000 元以内的手机", "default:s_a")
        other = await _invoke(graph, "有什么满减活动", "default:s_b")
        assert other["goods_list"] == [], "a different session must not inherit another one's candidates"
        assert other["route_trace"] == ["supervisor", "coupon_worker", "summarize"]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_delete_thread(tmp_path: Path, mem_engine: Engine) -> None:
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="sqlite", CHECKPOINT_DB_PATH=tmp_path / "cp4.db")
    provider = await open_checkpoint_provider(settings)
    try:
        graph = compile_graph(_deps(settings, mem_engine), provider.saver)
        thread = "default:s_del"
        await _invoke(graph, "推荐 2000 元以内的手机", thread)
        assert await provider.delete_thread(thread) is True
        snapshot = await graph.aget_state(graph_config(thread))
        assert not snapshot.values.get("seen_goods_ids")
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_memory_backend_and_unknown_kind() -> None:
    memory = await open_checkpoint_provider(Settings(APP_ENV="test", CHECKPOINT_BACKEND="memory"))
    assert isinstance(memory.saver, type(build_sync_checkpointer()))

    broken = Settings(APP_ENV="test").model_copy(update={"CHECKPOINT_BACKEND": "cockroach"})
    with pytest.raises(ConfigError):
        await open_checkpoint_provider(broken)


@pytest.mark.asyncio
async def test_postgres_backend_reports_the_missing_extra(tmp_path: Path) -> None:
    """The action message must name the command, not just fail."""
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="postgres", POSTGRES_CHECKPOINT_URL="")
    with pytest.raises(ConfigError) as caught:
        await open_checkpoint_provider(settings)
    assert "postgres" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_missing_thread_id_is_rejected(tmp_path: Path, mem_engine: Engine) -> None:
    """Spike finding: with a checkpointer attached, a call without thread_id cannot run."""
    settings = Settings(APP_ENV="test", CHECKPOINT_BACKEND="memory")
    graph = compile_graph(_deps(settings, mem_engine), build_sync_checkpointer())
    with pytest.raises(ValueError):
        await graph.ainvoke({"query": "推荐手机"})
