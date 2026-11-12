"""M4 verification: F1-F4 as pure functions — no graph, no IO, no model.

These assertions prove the business order is enforced by code rather than by prompt
(docs/GUARDRAILS.md, docs/SHOPPING_RULES.md §4).
"""

from __future__ import annotations

import pytest

from shopping_backend.graph.routing import (
    route_after_coupon,
    route_after_goods,
    route_after_stock,
    route_from_supervisor,
)
from shopping_backend.graph.state import MAX_NODE_VISITS, ShoppingAgentState


def state(**overrides) -> ShoppingAgentState:
    base: dict = {
        "user_id": "u_1001",
        "session_id": "s_1",
        "query": "推荐手机",
        "intent": "goods_query",
        "goods_list": [{"id": "g_0001", "title": "A", "price_cents": 169900}],
        "stock_map": {"g_0001": {"goods_id": "g_0001", "available": 12}},
        "node_visits": {"supervisor": 1, "goods_worker": 1, "stock_worker": 1},
    }
    base.update(overrides)
    return ShoppingAgentState.model_validate(base)


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("goods_query", "goods_worker"),
        ("composite", "goods_worker"),
        ("stock_query", "goods_worker"),  # ADR A7: ids are resolved before availability is read
        ("coupon_query", "coupon_worker"),
        ("other", "summarize"),
    ],
)
def test_supervisor_routes_by_intent(intent: str, expected: str) -> None:
    assert route_from_supervisor(state(intent=intent)) == expected


def test_unknown_intent_falls_back_to_summarize() -> None:
    assert route_from_supervisor(state(intent="hallucinated_intent")) == "summarize"


def test_goods_always_goes_to_stock_when_there_are_candidates() -> None:
    assert route_after_goods(state()) == "stock_worker"


def test_no_candidates_skips_stock_lookup() -> None:
    assert route_after_goods(state(goods_list=[])) == "summarize"


def test_in_stock_answer_does_not_consult_coupons() -> None:
    """F2: this is the branch that makes verification case 1 assert `coupon_worker not in trace`."""
    assert route_after_stock(state()) == "summarize"


def test_every_candidate_sold_out_consults_coupons() -> None:
    """F3."""
    sold_out = state(stock_map={"g_0001": {"goods_id": "g_0001", "available": 0}})
    assert route_after_stock(sold_out) == "coupon_worker"


def test_mixed_availability_still_shows_coupons_only_when_asked() -> None:
    mixed = state(
        goods_list=[
            {"id": "g_0001", "title": "A", "price_cents": 1},
            {"id": "g_0002", "title": "B", "price_cents": 2},
        ],
        stock_map={"g_0001": {"goods_id": "g_0001", "available": 0}, "g_0002": {"goods_id": "g_0002", "available": 3}},
    )
    assert route_after_stock(mixed) == "summarize"
    assert route_after_stock(mixed.model_copy(update={"coupon_needed": True})) == "coupon_worker"


def test_composite_with_coupon_intent_always_checks_coupons() -> None:
    assert route_after_stock(state(intent="composite", coupon_needed=True)) == "coupon_worker"


def test_resolve_only_availability_question_skips_coupons() -> None:
    """`有货吗` must not volunteer promotions, even when the answer is 无现货."""
    assert (
        route_after_stock(
            state(
                intent="stock_query", resolve_only=True, stock_map={"g_0001": {"goods_id": "g_0001", "available": 0}}
            )
        )
        == "summarize"
    )


def test_empty_stock_map_does_not_trigger_coupon_branch() -> None:
    assert route_after_stock(state(stock_map={})) == "summarize"


def test_visit_budget_stops_extra_hops() -> None:
    """F6: a state that already burned the budget cannot add a coupon hop."""
    exhausted = state(
        coupon_needed=True,
        stock_map={"g_0001": {"goods_id": "g_0001", "available": 0}},
        node_visits={"supervisor": 1, "goods_worker": 1, "stock_worker": 1, "coupon_worker": MAX_NODE_VISITS},
    )
    assert route_after_stock(exhausted) == "summarize"


def test_coupon_worker_always_ends_at_summarize() -> None:
    assert route_after_coupon(state()) == "summarize"


def test_routing_accepts_a_restored_dict_state() -> None:
    """Routing is called with the raw checkpoint dict as well as the model."""
    as_dict = {
        "intent": "coupon_query",
        "goods_list": [],
        "stock_map": {},
        "coupon_needed": False,
        "resolve_only": False,
        "node_visits": {},
    }
    assert route_from_supervisor(as_dict) == "coupon_worker"
