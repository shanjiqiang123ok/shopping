"""Stock Worker: availability for whatever goods_worker resolved.

It reads ids only from state, so an id that failed the guardrail (or was invented) can never be
looked up — the whitelist and the request share one source.
"""

from __future__ import annotations

from shopping_backend.core.logging import logger
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import ShoppingAgentState, bump_visit, context_of, state_goods, trace


def make_stock_worker(deps: GraphDeps):
    async def stock_worker_node(state: ShoppingAgentState) -> dict:
        goods = state_goods(state)
        goods_ids = [item.id for item in goods]
        if not goods_ids:
            return {
                "stock_map": {},
                "route_trace": trace(state, "stock_worker"),
                "node_visits": bump_visit(state, "stock_worker"),
            }

        result = await deps.backend.get_stock(context_of(state), goods_ids)
        stock_map = {goods_id: row.model_dump(mode="json") for goods_id, row in (result.value or {}).items()}
        out_of_stock = [goods_id for goods_id, row in stock_map.items() if int(row.get("available", 0) or 0) <= 0]

        updates: dict[str, object] = {
            "stock_map": stock_map,
            "out_of_stock_ids": out_of_stock,
            "route_trace": trace(state, "stock_worker"),
            "node_visits": bump_visit(state, "stock_worker"),
            "degraded": state.degraded or bool(result.degraded),
            "dropped_rows": int(state.dropped_rows or 0) + result.dropped_rows,
        }
        if result.error_msg and not stock_map:
            updates["error_msg"] = result.error_msg

        logger.info(
            "stock_worker_done", queried=len(goods_ids), out_of_stock=len(out_of_stock), trace_id=state.trace_id
        )
        return updates

    return stock_worker_node
