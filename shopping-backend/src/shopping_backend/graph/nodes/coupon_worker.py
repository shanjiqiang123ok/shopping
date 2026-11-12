"""Coupon Worker: usable promotions per candidate, or the general list when there are none.

F3 routes here when everything is sold out, so this node is also the "still help the user" branch:
out of stock plus a coupon is a better answer than out of stock plus silence (R-S4 / O5).
"""

from __future__ import annotations

from shopping_backend.core.logging import logger
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import ShoppingAgentState, bump_visit, context_of, trace
from shopping_backend.services import GENERAL_KEY


def make_coupon_worker(deps: GraphDeps):
    async def coupon_worker_node(state: ShoppingAgentState) -> dict:
        goods_ids = [str(row.get("id")) for row in (state.goods_list or [])]
        ctx = context_of(state)

        result = await deps.backend.get_coupon(ctx, goods_ids)
        bundle = result.value
        coupon_map: dict[str, list[dict]] = bundle.as_state() if bundle is not None else {}

        # A goods-less coupon query (R-C4) must not keep candidates the user never asked about.
        if not goods_ids:
            coupon_map = {key: rows for key, rows in coupon_map.items() if key == GENERAL_KEY}
        else:
            coupon_map = {key: rows for key, rows in coupon_map.items() if key == GENERAL_KEY or key in goods_ids}

        updates: dict[str, object] = {
            "coupon_map": coupon_map,
            "route_trace": trace(state, "coupon_worker"),
            "node_visits": bump_visit(state, "coupon_worker"),
            "degraded": state.degraded or bool(result.degraded),
            "dropped_rows": int(state.dropped_rows or 0) + result.dropped_rows,
        }
        if result.error_msg and not coupon_map:
            updates["error_msg"] = result.error_msg

        logger.info(
            "coupon_worker_done",
            keys=len(coupon_map),
            dropped=result.dropped_rows,
            without_stock=bool(state.out_of_stock_ids),
            trace_id=state.trace_id,
        )
        return updates

    return coupon_worker_node
