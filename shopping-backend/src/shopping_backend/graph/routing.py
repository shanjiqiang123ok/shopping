"""Hard routing (F1-F4). Pure functions over state — no IO, no LLM, trivially unit-testable.

`frame.md`'s key constraint is that the business order is enforced by code, not by model memory,
so these are the only edges the graph has: the LLM influences `intent`, never the topology.
"""

from __future__ import annotations

from typing import Any

from shopping_backend.domain.enums import Intent
from shopping_backend.graph.state import ShoppingAgentState, budget_left

_GOODS_FIRST_INTENTS = frozenset({Intent.GOODS_QUERY.value, Intent.COMPOSITE.value, Intent.STOCK_QUERY.value})


def route_from_supervisor(state: ShoppingAgentState | dict[str, Any]) -> str:
    """goods/composite/stock all enter through goods_worker (see ADR A7: resolve ids first)."""
    intent = _field(state, "intent")
    if intent in _GOODS_FIRST_INTENTS:
        return "goods_worker"
    if intent == Intent.COUPON_QUERY.value:
        return "coupon_worker"
    return "summarize"


def route_after_goods(state: ShoppingAgentState | dict[str, Any]) -> str:
    """F1: candidates found -> availability is always checked next. No candidates -> say so."""
    if _field(state, "goods_list"):
        return "stock_worker"
    return "summarize"


def route_after_stock(state: ShoppingAgentState | dict[str, Any]) -> str:
    """F2/F3: in-stock answers skip coupons; a fully sold-out basket must offer coupons.

    `resolve_only` (an availability lookup for one named product) also skips coupons: the user
    asked "有货吗", not "有什么券", and volunteering coupons there is a different conversation.
    """
    if _field(state, "resolve_only"):
        return "summarize"
    if not _field(state, "goods_list"):
        return "summarize"
    if not budget_left(state):
        return "summarize"

    available = [int(row.get("available", 0) or 0) for row in (_field(state, "stock_map") or {}).values()]
    if _field(state, "coupon_needed"):
        return "coupon_worker"
    if available and not any(count > 0 for count in available):
        return "coupon_worker"
    return "summarize"


def route_after_coupon(state: ShoppingAgentState | dict[str, Any]) -> str:
    """F4: coupons always terminate at the summary node."""
    return "summarize"


def _field(state: ShoppingAgentState | dict[str, Any], name: str) -> Any:
    return getattr(state, name, None) if isinstance(state, ShoppingAgentState) else state.get(name)
