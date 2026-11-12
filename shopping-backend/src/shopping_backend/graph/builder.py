"""Graph assembly: five nodes, the hard routing of F1-F4, and the compiled artifact.

Topology lives here and nowhere else. The LLM can influence `intent` (and therefore which branch of
`route_from_supervisor` fires); it cannot add, skip or reorder a step.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.nodes import make_coupon_worker, make_goods_worker, make_stock_worker, make_summarize
from shopping_backend.graph.routing import (
    route_after_coupon,
    route_after_goods,
    route_after_stock,
    route_from_supervisor,
)
from shopping_backend.graph.state import ShoppingAgentState

NODE_NAMES = ("supervisor", "goods_worker", "stock_worker", "coupon_worker", "summarize")


def build_state_graph(deps: GraphDeps) -> StateGraph:
    from shopping_backend.graph.supervisor import make_supervisor

    builder = StateGraph(ShoppingAgentState)
    builder.add_node("supervisor", make_supervisor(deps))
    builder.add_node("goods_worker", make_goods_worker(deps))
    builder.add_node("stock_worker", make_stock_worker(deps))
    builder.add_node("coupon_worker", make_coupon_worker(deps))
    builder.add_node("summarize", make_summarize(deps))

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {"goods_worker": "goods_worker", "coupon_worker": "coupon_worker", "summarize": "summarize"},
    )
    builder.add_conditional_edges(
        "goods_worker",
        route_after_goods,
        {"stock_worker": "stock_worker", "summarize": "summarize"},
    )
    builder.add_conditional_edges(
        "stock_worker",
        route_after_stock,
        {"coupon_worker": "coupon_worker", "summarize": "summarize"},
    )
    builder.add_conditional_edges("coupon_worker", route_after_coupon, {"summarize": "summarize"})
    builder.add_edge("summarize", END)
    return builder


def compile_graph(
    deps: GraphDeps, checkpointer: BaseCheckpointSaver | None = None, *, name: str = "shopping-guide"
) -> CompiledStateGraph:
    return build_state_graph(deps).compile(checkpointer=checkpointer, name=name)


def graph_config(
    ctx_thread_id: str, *, trace_id: str = "", intent: str = "", tags: list[str] | None = None
) -> dict[str, Any]:
    """RunnableConfig for one request.

    Only JSON-safe values go in `configurable`: the checkpointer persists this namespace, and a
    non-serialisable dependency would fail deep inside a checkpoint write.
    """
    return {
        "configurable": {"thread_id": ctx_thread_id},
        "recursion_limit": 12,
        "run_name": "shopping-agent-run",
        "tags": [*(tags or []), f"mode:{intent}"] if intent else (tags or []),
        "metadata": {"trace_id": trace_id, "service": "shopping-backend"},
    }
