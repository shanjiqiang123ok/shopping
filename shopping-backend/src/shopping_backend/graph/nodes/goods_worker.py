"""Goods Worker: candidate search and the only place product ids enter the graph.

Two resolution paths, both provenance-safe:
  * a fresh search through the backend;
  * an ordinal reference (`第二款`) resolved against `candidate_memory` from the previous turn.

What it never does is turn a product *name* into an id — that would be fabrication (ADR A7).
"""

from __future__ import annotations

from shopping_backend.core.logging import logger
from shopping_backend.domain.enums import Intent
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import (
    ShoppingAgentState,
    bump_visit,
    context_of,
    merged_seen_ids,
    trace,
)
from shopping_backend.guardrails.validate_input import try_goods_query


def make_goods_worker(deps: GraphDeps):
    async def goods_worker_node(state: ShoppingAgentState) -> dict:
        slots = dict(state.slots or {})
        ordinal = slots.get("ordinal")

        if ordinal and state.candidate_memory:
            return _resolve_from_candidates(state, ordinal=ordinal)

        # A matched category is a better filter than the raw sentence: "这款卖断了的耳机" would
        # otherwise be searched as free text and match nothing. A product mention still narrows the
        # category, otherwise "华为手机" returns the cheapest phones of every other brand.
        goods_ref = slots.get("goods_ref")
        text_query = goods_ref or ("" if slots.get("category") else state.query)
        limit = 3 if state.resolve_only else min(8, deps.settings.MAX_GOODS_LIMIT)

        parsed, invalid = try_goods_query(
            {
                "query": text_query,
                "category": slots.get("category"),
                "min_price_cents": slots.get("min_price_cents"),
                "max_price_cents": slots.get("max_price_cents"),
                "limit": limit,
            },
            max_limit=deps.settings.MAX_GOODS_LIMIT,
        )
        if parsed is None:
            logger.warning("goods_worker_invalid_query", detail=invalid[:120], trace_id=state.trace_id)
            return {
                "goods_list": [],
                "error_msg": invalid,
                "degraded": True,
                "route_trace": trace(state, "goods_worker"),
                "node_visits": bump_visit(state, "goods_worker"),
            }

        ctx = context_of(state)
        result = await deps.backend.search_goods(ctx, parsed)
        rows = [item.model_dump(mode="json") for item in (result.value or [])]

        updates: dict[str, object] = {
            "goods_list": rows,
            "seen_goods_ids": merged_seen_ids(
                state.seen_goods_ids, [row["id"] for row in rows], cap=deps.settings.MAX_SEEN_GOODS_IDS
            ),
            "candidate_memory": rows or state.candidate_memory,  # keep last candidates when nothing matched
            "route_trace": trace(state, "goods_worker"),
            "node_visits": bump_visit(state, "goods_worker"),
            "degraded": state.degraded or bool(result.degraded),
            "dropped_rows": int(state.dropped_rows or 0) + result.dropped_rows,
        }
        if result.error_msg:
            updates["error_msg"] = result.error_msg
        if not rows and state.intent == Intent.COMPOSITE.value:
            # composite with no candidates: asking for coupons on nothing is noise
            updates["coupon_needed"] = False

        logger.info("goods_worker_done", kept=len(rows), dropped=result.dropped_rows, trace_id=state.trace_id)
        return updates

    def _resolve_from_candidates(state: ShoppingAgentState, *, ordinal: int) -> dict:
        index = ordinal - 1
        candidates = list(state.candidate_memory or [])
        if index < 0 or index >= len(candidates):
            logger.info("goods_worker_ordinal_out_of_range", ordinal=ordinal, available=len(candidates))
            return {
                "goods_list": [],
                "error_msg": f"上一轮只有 {len(candidates)} 款候选，没有第 {ordinal} 款。",
                "route_trace": trace(state, "goods_worker"),
                "node_visits": bump_visit(state, "goods_worker"),
            }
        row = candidates[index]
        return {
            "goods_list": [row],
            "seen_goods_ids": merged_seen_ids(
                state.seen_goods_ids, [str(row["id"])], cap=deps.settings.MAX_SEEN_GOODS_IDS
            ),
            "resolve_only": True,  # an ordinal lookup is an availability question
            "coupon_needed": bool(state.coupon_needed),
            "route_trace": trace(state, "goods_worker"),
            "node_visits": bump_visit(state, "goods_worker"),
        }

    return goods_worker_node
