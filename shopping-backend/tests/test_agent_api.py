"""M5 verification: the service contract of docs/API.md §2, exercised over HTTP.

The fixtures here are the whole point of the milestone: a caller with only SQLite, no Docker and no
API key can complete an end-to-end shopping conversation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from shopping_backend.agent_api.main import create_app
from shopping_backend.agent_api.runtime import AgentRuntime, derive_preference_facts
from shopping_backend.core.config import Settings
from shopping_backend.core.errors import Unavailable
from shopping_backend.domain.models_orm import AgentRequestLog
from shopping_backend.services import build_services

RUN = "/api/shopping-agent/run"
AUTH = {"Authorization": "Bearer test-token"}


def _settings(**overrides) -> Settings:
    base = {
        "APP_ENV": "test",
        "AGENT_MODE": "rule_based",
        "BACKEND_DRIVER": "inprocess",
        "CHECKPOINT_BACKEND": "memory",
        "AGENT_INTERNAL_TOKEN": "test-token",
        "BUSINESS_API_TOKEN": "",
        "SEED_ON_STARTUP": False,
        "LOG_DIR": "./logs",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(mem_engine: Engine) -> Iterator[TestClient]:
    settings = _settings()
    runtime = AgentRuntime(settings=settings, engine=mem_engine)
    with TestClient(create_app(settings=settings, engine=mem_engine, runtime=runtime)) as test_client:
        yield test_client


def _run(client: TestClient, query: str, *, session_id: str = "s_api_1", user_id: str = "u_1001", **headers: str):
    return client.post(
        RUN, json={"user_id": user_id, "session_id": session_id, "query": query}, headers=headers or AUTH
    )


# ------------------------------------------------------------------ happy path


def test_run_selection(client: TestClient) -> None:
    response = _run(client, "推荐 2000 元以内的手机")
    assert response.status_code == 200
    body = response.json()

    assert body["meta"]["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]
    assert body["meta"]["mode"] == "rule_based"
    assert body["meta"]["backend"] == "inprocess"
    assert body["meta"]["degraded"] is False
    assert body["data"]["intent"] == "goods_query"
    assert {item["id"] for item in body["data"]["goods"]} == {"g_0001", "g_0002", "g_0003"}
    assert "¥1699.00" in body["answer"]
    assert body["request_id"] and body["trace_id"]
    assert body["meta"]["thread_id"] == "default:s_api_1"


def test_run_coupon_flow(client: TestClient) -> None:
    body = _run(client, "这款卖断了的耳机有优惠券吗", session_id="s_api_c").json()
    assert "coupon_worker" in body["meta"]["route_trace"]
    assert body["data"]["coupons"], "the sold-out branch must still carry a coupon"
    assert "M199_20" in body["answer"]


def test_run_generic_headers_and_openapi(client: TestClient) -> None:
    response = _run(client, "有什么满减活动", session_id="s_api_h")
    assert response.headers.get("X-Trace-Id")
    assert client.get("/openapi.json").status_code == 200
    assert "shopping-agent" in client.get("/").json()["run"]


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "rule_based"
    assert body["backend_driver"] == "inprocess"


def test_chat_debug_page_is_served(client: TestClient) -> None:
    response = client.get("/chat")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "/api/shopping-agent/run" in response.text
    assert "route_trace" in response.text, "调试字段必须可见，否则这页只是个玩具聊天框"
    assert "chat" in client.get("/").json()


def test_chat_page_stays_out_of_the_maxkb_contract(client: TestClient) -> None:
    """/docs 的 OpenAPI 会被当作 MaxKB 外部工具定义导入，页面无须出现在里面。"""
    assert "/chat" not in client.get("/openapi.json").json()["paths"]


def test_continuation_over_http(client: TestClient) -> None:
    first = _run(client, "推荐 2000 元以内的手机", session_id="s_api_cont").json()
    second = _run(client, "第二款有货吗", session_id="s_api_cont").json()

    ordered = sorted(first["data"]["goods"], key=lambda item: item["price_cents"])
    assert second["data"]["goods"][0]["id"] == ordered[1]["id"]
    assert second["meta"]["route_trace"] == ["supervisor", "goods_worker", "stock_worker", "summarize"]


# ------------------------------------------------------------------ auth and validation


def test_missing_token_is_401(client: TestClient) -> None:
    response = client.post(RUN, json={"user_id": "u_1001", "session_id": "s_x", "query": "手机"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_wrong_token_is_401(client: TestClient) -> None:
    response = _run(client, "手机", **{"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_unauthorized_request_never_reaches_the_graph(client: TestClient, mem_engine: Engine) -> None:
    before = len(_logs(mem_engine))
    client.post(RUN, json={"user_id": "u_1001", "session_id": "s_noauth", "query": "推荐手机"})
    assert len(_logs(mem_engine)) == before, "a rejected call must not consume a checkpoint or write a log row"


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "u_1001", "session_id": "s_1", "query": ""},
        {"user_id": "bad id", "session_id": "s_1", "query": "手机"},
        {"user_id": "u_1001", "session_id": "s_1", "query": "手机", "role": "admin"},
        {"user_id": "u_1001", "session_id": "s_1"},
    ],
)
def test_invalid_input_is_422(client: TestClient, payload: dict) -> None:
    response = client.post(RUN, json=payload, headers=AUTH)
    assert response.status_code == 422
    assert "errors" in response.json()


# ------------------------------------------------------------------ degradation


def test_unreachable_business_api_degrades_to_200(mem_engine: Engine) -> None:
    settings = _settings(
        BACKEND_DRIVER="http",
        BUSINESS_API_BASE_URL="http://127.0.0.1:1",
        BUSINESS_API_TIMEOUT_S=0.5,
        BUSINESS_API_MAX_RETRY=0,
    )
    runtime = AgentRuntime(settings=settings, engine=mem_engine)
    with TestClient(create_app(settings=settings, engine=mem_engine, runtime=runtime)) as guarded:
        response = guarded.post(
            RUN, json={"user_id": "u_1001", "session_id": "s_down", "query": "推荐手机"}, headers=AUTH
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["degraded"] is True
        assert body["answer"] == Unavailable.friendly
        assert "Traceback" not in response.text
        assert body["meta"]["backend"] == "http"


def test_total_budget_timeout_is_reported(mem_engine: Engine) -> None:
    settings = _settings(AGENT_TOTAL_TIMEOUT_S=0.0)
    runtime = AgentRuntime(settings=settings, engine=mem_engine)
    with TestClient(create_app(settings=settings, engine=mem_engine, runtime=runtime)) as tight:
        response = tight.post(
            RUN, json={"user_id": "u_1001", "session_id": "s_slow", "query": "推荐手机"}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["meta"]["degraded"] is True


# ------------------------------------------------------------------ persistence side effects


def test_request_log_is_written(client: TestClient, mem_engine: Engine) -> None:
    _run(client, "推荐 2000 元以内的手机", session_id="s_log")
    rows = _logs(mem_engine)
    assert rows, "operational trail (docs/ARCHITECTURE.md §6)"
    latest = rows[-1]
    assert latest.intent == "goods_query"
    assert "goods_worker" in latest.route_trace
    assert latest.goods_n == 3
    assert latest.degraded is False


def test_preferences_are_written_back_deterministically(client: TestClient, mem_engine: Engine) -> None:
    _run(client, "推荐 2000 元以内的手机", user_id="u_pref_1", session_id="s_pref")
    stored = {row.pref_key: row.pref_value for row in build_services(mem_engine).preference.load("u_pref_1")}
    assert stored["budget_limit"] == "预算不超过 2000.00 元"
    assert stored["preferred_category"] == "主要看手机"

    again = _run(client, "有什么满减活动", user_id="u_pref_1", session_id="s_pref2").json()
    assert "budget_limit" in (again["meta"]["preference_summary"] or "")


def test_derive_facts_ignores_unusable_slots() -> None:
    assert derive_preference_facts({"slots": {}}) == []
    assert derive_preference_facts({"slots": {"max_price_cents": 0}}) == []
    facts = derive_preference_facts({"slots": {"max_price_cents": 150000, "category": "earphone"}})
    assert {fact.key for fact in facts} == {"budget_limit", "preferred_category"}


def _logs(engine: Engine) -> list[AgentRequestLog]:
    with Session(engine) as session:
        return list(session.exec(select(AgentRequestLog)).all())
