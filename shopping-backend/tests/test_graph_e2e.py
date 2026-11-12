"""M4/M6 verification: the end-to-end graph, in rule_based mode, with no network and no model.

Every case in docs/SHOPPING_RULES.md §9 is asserted here against the real fixtures, real services,
real adapters and real guardrails — only the LLM is stubbed (and in these tests it is absent).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.engine import Engine

from fakes import FakeLLM, RaisingBackend
from shopping_backend.backends.inprocess import InProcessBackend
from shopping_backend.core.config import Settings
from shopping_backend.core.errors import Unavailable
from shopping_backend.graph.builder import compile_graph, graph_config
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import ShoppingAgentState
from shopping_backend.llm.service import NullLLM
from shopping_backend.services import build_services

THREAD = "default:s_e2e"


def make_deps(
    engine: Engine,
    *,
    settings: Settings | None = None,
    llm: Any | None = None,
    backend: Any | None = None,
) -> GraphDeps:
    settings = settings or Settings(APP_ENV="test", BACKEND_DRIVER="inprocess", AGENT_MODE="rule_based")
    services = build_services(engine)
    return GraphDeps(
        settings=settings,
        backend=backend or InProcessBackend(services),
        llm=llm or NullLLM(),
        services=services,
    )


async def run(
    deps: GraphDeps, query: str, *, thread_id: str = THREAD, saver: Any | None = None, **identity: Any
) -> dict[str, Any]:
    graph = compile_graph(deps, saver or InMemorySaver())
    state = ShoppingAgentState(
        user_id=identity.get("user_id", "u_1001"),
        session_id=identity.get("session_id", "s_e2e"),
        query=query,
        trace_id="t_e2e",
        preference_memory=identity.get("preference_memory", {}),
    )
    return await graph.ainvoke(state.as_input(), graph_config(thread_id, trace_id="t_e2e"))


def ids(result: Mapping[str, Any]) -> list[str]:
    return [str(row["id"]) for row in result.get("goods_list", [])]


@pytest.mark.asyncio
async def test_case1_selection_in_stock_skips_coupons(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "推荐 2000 元以内的手机")
    assert result["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]
    assert set(ids(result)) == {"g_0001", "g_0002", "g_0003"}
    assert "coupon_worker" not in result["route_trace"]
    answer = result["final_answer"]
    assert "¥1699.00" in answer
    assert "2000 元以内" in answer, "the user's own condition is echoed without a currency sign"
    assert result["degraded"] is False


@pytest.mark.asyncio
async def test_brand_narrows_a_matched_category(mem_engine: Engine) -> None:
    """A named brand must filter within its category, not be dropped in favour of the cheapest."""
    result = await run(make_deps(mem_engine), "我要买华为耳机")
    assert result["slots"]["category"] == "earphone"
    assert ids(result) == ["g_0007"], "QCY is a cheaper earphone but not the asked-for brand"
    assert "QCY" not in result["final_answer"]


@pytest.mark.asyncio
async def test_brand_with_no_on_sale_match_says_so(mem_engine: Engine) -> None:
    """The only Huawei phone is g_0005 off_sale: answer honestly instead of recommending others."""
    result = await run(make_deps(mem_engine), "我要买华为手机")
    assert result["goods_list"] == []
    assert "Redmi" not in result["final_answer"]
    assert "没找到" in result["final_answer"]


@pytest.mark.asyncio
async def test_case2_named_product_availability(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "Redmi Note 13 Pro 有货吗")
    assert result["resolve_only"] is True
    assert result["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]
    assert result["goods_list"][0]["id"] == "g_0001", "the id came from a search, not from the product name"
    assert "现货12件" in result["final_answer"]


@pytest.mark.asyncio
async def test_case2b_ordinal_after_previous_turn(mem_engine: Engine) -> None:
    deps = make_deps(mem_engine)
    saver = InMemorySaver()
    first = await run(deps, "推荐 2000 元以内的手机", saver=saver)
    second = await run(deps, "第二款有货吗", saver=saver)

    assert second["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"], (
        "route_trace must not accumulate across turns (spike finding)"
    )
    expected = sorted(first["goods_list"], key=lambda row: row["price_cents"])[1]["id"]
    assert ids(second) == [expected]
    assert second["resolve_only"] is True


@pytest.mark.asyncio
async def test_case3_sold_out_reaches_coupon_branch(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "这款卖断了的耳机有优惠券吗")
    assert "coupon_worker" in result["route_trace"]
    assert result["intent"] == "composite"
    assert "M199_20" in result["final_answer"]


@pytest.mark.asyncio
async def test_case_f3_all_sold_out_forces_coupons(mem_engine: Engine) -> None:
    """The fixtures' sold-out pair: g_0006 only (QCY, available 0) -> must reach coupons (F3)."""
    deps = make_deps(mem_engine)
    result = await run(deps, "QCY T13 有货吗")
    assert result["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"], (
        "resolve_only skips coupons"
    )

    result = await run(deps, "QCY T13 没货的话有券吗")
    assert "coupon_worker" in result["route_trace"]


@pytest.mark.asyncio
async def test_case4_general_promotions(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "有什么满减活动")
    assert result["route_trace"] == ["supervisor", "coupon_worker", "summarize"]
    assert "M5000_600" in result["final_answer"]
    assert result["goods_list"] == []


@pytest.mark.asyncio
async def test_case5_no_match_asks_for_clarification(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "推荐 5000 元以内的电视")
    assert result["goods_list"] == []
    assert "没找到" in result["final_answer"]
    assert "¥" not in result["final_answer"], "a clarification must not invent a price"


@pytest.mark.asyncio
async def test_case6_backend_failure_degrades(mem_engine: Engine) -> None:
    """O8: an outage answers with the data wording; a bug answers with the generic wording."""
    from shopping_backend.core.errors import Unavailable as UnavailableError

    result = await run(make_deps(mem_engine, backend=RaisingBackend(UnavailableError())), "推荐 2000 元以内的手机")
    assert result["degraded"] is True
    assert result["final_answer"] == Unavailable.friendly
    assert result["route_trace"] == ["supervisor", "goods_worker", "summarize"]
    assert "Traceback" not in str(result)

    buggy = await run(
        make_deps(mem_engine, backend=RaisingBackend(RuntimeError("bug in driver"))), "推荐 2000 元以内的手机"
    )
    assert buggy["degraded"] is True
    assert buggy["final_answer"] == "系统繁忙，请稍后再试。", (
        "a programming error must not masquerade as a data outage"
    )


@pytest.mark.asyncio
async def test_case8_order_request_is_refused(mem_engine: Engine) -> None:
    result = await run(make_deps(mem_engine), "帮我下单买第一个")
    assert result["intent"] == "other"
    assert result["route_trace"] == ["supervisor", "summarize"]
    assert "不能替你下单" in result["final_answer"]


@pytest.mark.asyncio
async def test_every_answer_is_grounded(mem_engine: Engine) -> None:
    """L7 must be a formality in rule_based mode: zero dropped lines on every path."""
    for query in (
        "推荐 2000 元以内的手机",
        "Redmi Note 13 Pro 有货吗",
        "有什么满减活动",
        "帮我下单买第一个",
        "推荐电视",
    ):
        result = await run(make_deps(mem_engine), query, session_id="s_" + str(abs(hash(query)) % 1000))
        assert result.get("grounding_drops", 0) == 0, (query, result["final_answer"])


# ------------------------------------------------------------------ llm mode


@pytest.mark.asyncio
async def test_llm_mode_reasons_and_number_filter(mem_engine: Engine) -> None:

    settings = Settings(APP_ENV="test", AGENT_MODE="llm", OPENAI_API_KEY="test-key")
    llm = FakeLLM(
        decisions=[{"intent": "goods_query", "category": "phone", "max_price_cents": 200000}],
        reasons={"g_0001": "预算内主摄规格最高", "g_0002": "只要 ¥1599 就能拿下"},
    )
    result = await run(make_deps(mem_engine, settings=settings, llm=llm), "推荐 2000 元以内的手机")
    assert result["intent"] == "goods_query"
    assert "理由：预算内主摄规格最高" in result["final_answer"]
    assert "就能拿下" not in result["final_answer"], "O9: a reason carrying a price is dropped"
    assert result["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]


@pytest.mark.asyncio
async def test_llm_mode_said_other_keeps_rule_verdict(mem_engine: Engine) -> None:

    settings = Settings(APP_ENV="test", AGENT_MODE="llm", OPENAI_API_KEY="test-key")
    llm = FakeLLM(decisions=[{"intent": "other"}])
    result = await run(make_deps(mem_engine, settings=settings, llm=llm), "推荐 2000 元以内的手机")
    assert result["intent"] == "goods_query", "a model that forgets the task must not disable the flow"


@pytest.mark.asyncio
async def test_llm_mode_still_refuses_transactions(mem_engine: Engine) -> None:

    settings = Settings(APP_ENV="test", AGENT_MODE="llm", OPENAI_API_KEY="test-key")
    llm = FakeLLM(decisions=[{"intent": "goods_query", "category": "phone"}])
    result = await run(make_deps(mem_engine, settings=settings, llm=llm), "帮我下单买手机")
    assert result["intent"] == "other"
    assert result["goods_list"] == []


@pytest.mark.asyncio
async def test_preferences_reach_the_llm_prompt(mem_engine: Engine) -> None:

    settings = Settings(APP_ENV="test", AGENT_MODE="llm", OPENAI_API_KEY="test-key")
    llm = FakeLLM(decisions=[{"intent": "goods_query"}])
    await run(
        make_deps(mem_engine, settings=settings, llm=llm),
        "推荐手机",
        preference_memory={"budget": "不超过2000元"},
    )
    assert any("不超过2000元" in call.get("system", "") for call in llm.calls)


@pytest.mark.asyncio
async def test_max_goods_limit_bounds_candidates(mem_engine: Engine) -> None:
    settings = Settings(APP_ENV="test", MAX_GOODS_LIMIT=2)
    result = await run(make_deps(mem_engine, settings=settings), "推荐手机")
    assert len(result["goods_list"]) <= 2
