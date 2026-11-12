"""Graph state.

Two decisions here are load-bearing, both found by `scripts/spike_langgraph.py`:

* **Facts are stored as plain dicts.** The checkpointer serialises state; Pydantic objects survive
  only if you trust the serializer's class registry, and a restored dict never surprises anyone.
  Read helpers re-validate at the boundary, so type safety is not lost, just deferred.
* **Per-turn fields are reset explicitly.** Checkpoint state *accumulates* across calls sharing a
  thread_id, so `route_trace` from turn 1 would otherwise appear in turn 2 and the routing
  assertions would be meaningless. The supervisor resets the working set; provenance and the
  candidate memory deliberately persist.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.enums import Intent
from shopping_backend.domain.schemas import CouponOut, GoodsOut, StockOut

# Written fresh every turn by supervisor_node.
PER_TURN_FIELDS: tuple[str, ...] = (
    "intent",
    "slots",
    "coupon_needed",
    "resolve_only",
    "goods_list",
    "stock_map",
    "coupon_map",
    "route_trace",
    "node_visits",
    "degraded",
    "error_msg",
    "final_answer",
    "reasons",
)

MAX_NODE_VISITS = 4  # F6

# The supervisor merges this into every turn. Without it, checkpointed state from a previous
# request would still be visible in the next one (spike finding, docs/ARCHITECTURE.md §3.2).
PER_TURN_RESET: dict[str, Any] = {
    "intent": Intent.OTHER.value,
    "slots": {},
    "coupon_needed": False,
    "resolve_only": False,
    "goods_list": [],
    "stock_map": {},
    "coupon_map": {},
    "out_of_stock_ids": [],
    "route_trace": [],
    "node_visits": {},
    "reasons": {},
    "dropped_rows": 0,
    "grounding_drops": 0,
    "degraded": False,
    "error_msg": None,
    "final_answer": None,
}


class ShoppingAgentState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # --- input ---
    user_id: str = ""
    session_id: str = ""
    tenant_id: str = "default"
    query: str = ""
    trace_id: str = ""

    # --- context ---
    preference_memory: dict[str, str] = Field(default_factory=dict)

    # --- orchestration (per turn) ---
    intent: str = Intent.OTHER.value
    slots: dict[str, Any] = Field(default_factory=dict)
    coupon_needed: bool = False
    resolve_only: bool = False
    node_visits: dict[str, int] = Field(default_factory=dict)

    # --- business facts (per turn, validated rows only) ---
    goods_list: list[dict[str, Any]] = Field(default_factory=list)
    stock_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    coupon_map: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    out_of_stock_ids: list[str] = Field(default_factory=list)

    # --- provenance and cross-turn memory (persist on purpose) ---
    seen_goods_ids: list[str] = Field(default_factory=list)
    candidate_memory: list[dict[str, Any]] = Field(default_factory=list)
    last_intent: str = ""

    # --- output / observability (per turn) ---
    route_trace: list[str] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)
    dropped_rows: int = 0
    grounding_drops: int = 0
    degraded: bool = False
    error_msg: str | None = None
    final_answer: str | None = None

    def as_input(self) -> dict[str, Any]:
        """The dict LangGraph should merge into the checkpoint at invoke time."""
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "query": self.query,
            "trace_id": self.trace_id,
            "preference_memory": self.preference_memory,
        }


def state_goods(state: ShoppingAgentState | dict[str, Any]) -> list[GoodsOut]:
    rows = _get(state, "goods_list")
    return [GoodsOut.model_validate(row) for row in rows]


def state_stock(state: ShoppingAgentState | dict[str, Any]) -> dict[str, StockOut]:
    raw = _get(state, "stock_map")
    return {key: StockOut.model_validate(value) for key, value in raw.items()}


def state_coupons(state: ShoppingAgentState | dict[str, Any]) -> dict[str, list[CouponOut]]:
    raw = _get(state, "coupon_map")
    return {key: [CouponOut.model_validate(row) for row in rows] for key, rows in raw.items()}


def merged_seen_ids(existing: list[str], new: list[str], *, cap: int = 50) -> list[str]:
    """Append-dedupe with a cap: the whitelist must not grow without bound across a long session."""
    merged = list(dict.fromkeys([*existing, *new]))
    return merged[-cap:]


def visits(state: ShoppingAgentState | dict[str, Any]) -> dict[str, int]:
    return dict(_get(state, "node_visits") or {})


def bump_visit(state: ShoppingAgentState | dict[str, Any], node: str) -> dict[str, int]:
    counts = visits(state)
    counts[node] = counts.get(node, 0) + 1
    return counts


def budget_left(state: ShoppingAgentState | dict[str, Any]) -> bool:
    return sum(visits(state).values()) < MAX_NODE_VISITS


def trace(state: ShoppingAgentState | dict[str, Any], node: str) -> list[str]:
    return [*(_get(state, "route_trace") or []), node]


def context_of(state: ShoppingAgentState | dict[str, Any]) -> ShoppingSessionContext:
    """Identity for a backend call: always derived from state, never from tool arguments."""
    return ShoppingSessionContext(
        user_id=str(_get(state, "user_id") or ""),
        session_id=str(_get(state, "session_id") or ""),
        tenant_id=str(_get(state, "tenant_id") or "default"),
        trace_id=str(_get(state, "trace_id") or ""),
    )


def _get(state: ShoppingAgentState | dict[str, Any], name: str) -> Any:
    return getattr(state, name, None) if isinstance(state, BaseModel) else state.get(name)
