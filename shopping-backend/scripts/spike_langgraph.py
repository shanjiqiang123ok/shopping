"""LangGraph 1.x API spike (M4 prerequisite).

Verifies, against the *installed* langgraph version, the four assumptions the design
in docs/ARCHITECTURE.md rests on:

1. ``StateGraph`` accepts a Pydantic model as the state schema and merges partial updates.
2. ``add_conditional_edges`` with a plain path map implements the hard routing of F1-F4.
3. ``AsyncSqliteSaver`` works with a long-lived ``aiosqlite`` connection and persists
   dict-typed state fields (no Pydantic objects, no ``set``) across two invocations
   sharing a ``thread_id``.
4. ``aget_state`` restores those fields so a follow-up turn can reuse ``seen_goods_ids``.

Run:  uv run python scripts/spike_langgraph.py
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


class SpikeState(BaseModel):
    query: str = ""
    intent: str = "other"
    goods_list: list[dict[str, Any]] = Field(default_factory=list)
    stock_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    seen_goods_ids: list[str] = Field(default_factory=list)
    route_trace: list[str] = Field(default_factory=list)
    final_answer: str | None = None


GOODS = {
    "g_0001": {"id": "g_0001", "title": "Redmi Note 13 Pro", "price_cents": 169900},
    "g_0002": {"id": "g_0002", "title": "QCY Earbuds 3", "price_cents": 19900},
}
AVAILABLE = {"g_0001": 12, "g_0002": 0}  # g_0002 is the sold-out sample driving F3


def supervisor(state: SpikeState) -> dict[str, Any]:
    intent = "goods_query" if any(k in state.query for k in ("手机", "耳机")) else "other"
    return {"intent": intent, "route_trace": [*state.route_trace, "supervisor"]}


def route_from_supervisor(state: SpikeState) -> str:
    return "goods_worker" if state.intent == "goods_query" else "summarize"


def goods_worker(state: SpikeState) -> dict[str, Any]:
    picked = [GOODS["g_0002"]] if "耳机" in state.query else [GOODS["g_0001"]]
    return {
        "goods_list": picked,
        "seen_goods_ids": [g["id"] for g in picked],
        "route_trace": [*state.route_trace, "goods_worker"],
    }


def stock_worker(state: SpikeState) -> dict[str, Any]:
    stock = {g["id"]: {"available": AVAILABLE[g["id"]]} for g in state.goods_list}
    return {"stock_map": stock, "route_trace": [*state.route_trace, "stock_worker"]}


def route_after_stock(state: SpikeState) -> str:
    return "summarize" if any(v["available"] > 0 for v in state.stock_map.values()) else "coupon_worker"


def coupon_worker(state: SpikeState) -> dict[str, Any]:
    return {"route_trace": [*state.route_trace, "coupon_worker"], "final_answer": "缺货，可领券"}


def summarize(state: SpikeState) -> dict[str, Any]:
    answer = f"找到 {len(state.goods_list)} 款，route={'>'.join(state.route_trace)}"
    return {"final_answer": answer, "route_trace": [*state.route_trace, "summarize"]}


def build_graph(checkpointer: Any):
    builder = StateGraph(SpikeState)
    builder.add_node("supervisor", supervisor)
    builder.add_node("goods_worker", goods_worker)
    builder.add_node("stock_worker", stock_worker)
    builder.add_node("coupon_worker", coupon_worker)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {"goods_worker": "goods_worker", "summarize": "summarize"},
    )
    builder.add_edge("goods_worker", "stock_worker")
    builder.add_conditional_edges(
        "stock_worker",
        route_after_stock,
        {"summarize": "summarize", "coupon_worker": "coupon_worker"},
    )
    builder.add_edge("coupon_worker", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(checkpointer=checkpointer)


async def check(label: str, condition: Any, detail: str = "") -> bool:
    verdict = bool(condition)
    print(f"[{'PASS' if verdict else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return verdict


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows console defaults to GBK
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from importlib.metadata import version as pkg_version

    print(f"langgraph {pkg_version('langgraph')} / python {sys.version.split()[0]}")
    ok = True

    # --- 1: pydantic state schema + partial merge ---
    async with aiosqlite.connect(":memory:") as raw_conn:
        saver = AsyncSqliteSaver(raw_conn)
        setup = saver.setup()
        if inspect.isawaitable(setup):
            await setup
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "default:s_demo"}}

        first = await graph.ainvoke({"query": "推荐 2000 元以内的手机"}, config)
        ok &= await check(
            "pydantic state + conditional routing",
            first.get("intent") == "goods_query" and first.get("final_answer"),
            f"intent={first.get('intent')} trace={first.get('route_trace')}",
        )
        ok &= await check(
            "F2 branch: stock>0 skips coupon_worker",
            "coupon_worker" not in first.get("route_trace", []),
            f"trace={first.get('route_trace')}",
        )

        second = await graph.ainvoke({"query": "电视"}, config)
        ok &= await check(
            "other intent goes straight to summarize",
            second.get("route_trace", [None])[-2:] == ["supervisor", "summarize"],
            f"trace={second.get('route_trace')}",
        )

        snapshot = await graph.aget_state(config)
        restored = snapshot.values if isinstance(snapshot.values, dict) else dict(snapshot.values)
        ok &= await check(
            "aget_state restores values",
            bool(restored),
            f"keys={sorted(restored)[:4]}",
        )

    # --- 2: cross-process persistence with a file DB (dict/list fields survive msgpack) ---
    tmp = Path(tempfile.mkdtemp(prefix="spike_"))
    try:
        db = tmp / "checkpoints.db"
        async with aiosqlite.connect(str(db), check_same_thread=False) as conn:
            saver = AsyncSqliteSaver(conn)
            setup = saver.setup()
            if inspect.isawaitable(setup):
                await setup
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": "default:s_persist"}}
            await graph.ainvoke({"query": "推荐 2000 元以内的手机"}, config)

        async with aiosqlite.connect(str(db), check_same_thread=False) as conn:
            saver2 = AsyncSqliteSaver(conn)
            graph2 = build_graph(saver2)
            snap = await graph2.aget_state(config)
            values = snap.values or {}
            ok &= await check(
                "sqlite checkpoint persists dict-typed state",
                values.get("seen_goods_ids") == ["g_0001"] and isinstance(values.get("goods_list"), list),
                f"seen={values.get('seen_goods_ids')} goods_type={type(values.get('goods_list')).__name__}",
            )
            ok &= await check(
                "restored nested dicts are plain dicts",
                isinstance(values.get("goods_list", [None])[0], dict),
                f"type={type(values.get('goods_list', [None])[0]).__name__}",
            )

        # sync saver + delete_thread availability (used by scripts/reset_db.py)
        ok &= await check("SqliteSaver.delete_thread available", hasattr(SqliteSaver, "delete_thread"))
        ok &= await check("AsyncSqliteSaver.from_conn_string available", hasattr(AsyncSqliteSaver, "from_conn_string"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 3: F3 branch (all stock zero) must reach coupon_worker ---
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(MemorySaver())
    result = await graph.ainvoke({"query": "推荐缺货的耳机"}, {"configurable": {"thread_id": "default:s_mem"}})
    ok &= await check(
        "F3 branch: all stock zero reaches coupon_worker",
        "coupon_worker" in result.get("route_trace", []),
        f"trace={result.get('route_trace')}",
    )

    # Real finding pinned down: a compiled graph WITH a checkpointer refuses calls that
    # carry no thread_id, so every production invocation must set one.
    try:
        await graph.ainvoke({"query": "手机"})
        ok &= await check("thread_id is mandatory when checkpointer is set", False, "call unexpectedly succeeded")
    except ValueError as exc:
        ok &= await check(
            "thread_id is mandatory when checkpointer is set",
            "thread_id" in str(exc),
            str(exc).splitlines()[0],
        )

    zero_state = SpikeState(goods_list=[{"id": "g_0002"}])
    ok &= await check(
        "route_after_stock returns coupon_worker when all zero",
        route_after_stock(zero_state) == "coupon_worker",
    )

    print("\n" + ("SPIKE OK — design assumptions hold" if ok else "SPIKE FAILED — adjust the design before M4"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
