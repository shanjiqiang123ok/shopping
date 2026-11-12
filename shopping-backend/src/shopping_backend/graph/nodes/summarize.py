"""Summarize node: render from state, then verify before it can leave the service (F5, L7).

It performs no lookups. Everything it can say is bounded by what the workers already fetched, so
an ungrounded sentence has nowhere to come from except a model-authored reason clause — which is
checked on its own before it is allowed into a line.
"""

from __future__ import annotations

from shopping_backend.core.logging import logger
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.nodes.presentation import render
from shopping_backend.graph.state import (
    ShoppingAgentState,
    bump_visit,
    state_coupons,
    state_goods,
    state_stock,
    trace,
)
from shopping_backend.guardrails.grounding import Provenance, verify_answer


def make_summarize(deps: GraphDeps):
    async def summarize_node(state: ShoppingAgentState) -> dict:
        goods = state_goods(state)
        stock = state_stock(state)
        coupons = state_coupons(state)

        reasons: dict[str, str] = {}
        if deps.settings.effective_mode == "llm" and goods:
            preference_block = "; ".join(f"{key}={value}" for key, value in list(state.preference_memory.items())[:8])
            reasons = await deps.llm.reasons_for(
                query=state.query,
                goods_rows=[item.model_dump(mode="json") for item in goods],
                preference_block=preference_block,
            )

        draft = render(state, reasons=reasons or None)
        provenance = Provenance.from_state(
            goods,
            stock,
            [coupon for rows in coupons.values() for coupon in rows],
            extra_ids=state.seen_goods_ids,
        )
        report = verify_answer(draft, provenance)

        degraded = state.degraded or bool(report.dropped_count)
        error_msg = state.error_msg
        if report.fallback_used:
            degraded = True
            error_msg = error_msg or "回答内容未通过事实校验，已回退到安全话术。"

        if report.dropped_count:
            logger.error(
                "guardrail_L7_dropped_lines",
                dropped=report.dropped_count,
                violations=report.violations[:10],
                fallback=report.fallback_used,
                trace_id=state.trace_id,
            )

        return {
            "final_answer": report.answer,
            "reasons": {goods_id: reason for goods_id, reason in reasons.items()},
            "route_trace": trace(state, "summarize"),
            "node_visits": bump_visit(state, "summarize"),
            "degraded": degraded,
            "error_msg": error_msg,
            "grounding_drops": report.dropped_count,
        }

    return summarize_node
