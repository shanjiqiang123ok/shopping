"""M2 verification: the contract of docs/API.md §3, exercised through HTTP."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from shopping_backend.business_api.main import create_app
from shopping_backend.core.config import Settings


@pytest.fixture
def client(mem_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(APP_ENV="test", BUSINESS_API_TOKEN="", SEED_ON_STARTUP=False)
    with TestClient(create_app(engine=mem_engine, settings=settings)) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["rows"]["goods"] == 12
    assert client.get("/health").headers.get("X-Trace-Id")


def test_search_by_category_and_price(client: TestClient) -> None:
    body = client.get("/api/v1/goods/search", params={"category": "phone", "max_price": 2000, "limit": 8}).json()
    assert {item["id"] for item in body["items"]} == {"g_0001", "g_0002", "g_0003"}
    assert body["query"]["max_price_cents"] == 200000, "yuan at the edge becomes integer cents inside"


def test_search_with_chinese_query(client: TestClient) -> None:
    body = client.get("/api/v1/goods/search", params={"q": "耳机", "limit": 10}).json()
    assert {item["id"] for item in body["items"]} == {"g_0006", "g_0007"}


def test_detail_hides_off_sale_as_404(client: TestClient) -> None:
    assert client.get("/api/v1/goods/g_0001").status_code == 200
    missing = client.get("/api/v1/goods/g_0005")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "GOODS_NOT_FOUND"
    assert client.get("/api/v1/goods/nope").status_code == 404


def test_stock_query_zero_fills_unknown_ids(client: TestClient) -> None:
    body = client.post("/api/v1/stock/query", json={"goods_ids": ["g_0001", "g_0008", "g_missing"]}).json()
    assert body["stock"]["g_0001"]["available"] == 12
    assert body["stock"]["g_0008"] == {
        "goods_id": "g_0008",
        "on_hand": 0,
        "reserved": 0,
        "available": 0,
        "warehouses": [],
    }, "no stock record must not become a missing key the model has to guess about"
    assert body["stock"]["g_missing"]["available"] == 0


def test_coupon_query_ranks_single_best(client: TestClient) -> None:
    body = client.post("/api/v1/coupon/query", json={"goods_ids": ["g_0001"]}).json()
    codes = [coupon["code"] for coupon in body["coupons"]["g_0001"]]
    assert codes == ["M1500_100"]
    assert body["coupons"]["g_0001"][0]["display"] == "满¥1500.00减¥100.00"
    reasons = {item["code"]: item["reason"] for item in body["unavailable"]}
    assert reasons["M2000_150"] == "below_threshold"


def test_coupon_query_without_goods_uses_general_key(client: TestClient) -> None:
    body = client.post("/api/v1/coupon/query", json={"goods_ids": []}).json()
    assert [coupon["code"] for coupon in body["coupons"]["*"]] == ["M5000_600", "FREESHIP"]


@pytest.mark.parametrize(
    ("payload", "status_code"),
    [
        ({"goods_ids": []}, 422),
        ({"goods_ids": ["has space"]}, 422),
        ({"goods_ids": ["g_0001"], "user_id": "u_1001"}, 422),  # extra="forbid"
    ],
)
def test_stock_query_rejects_bad_input(client: TestClient, payload: dict, status_code: int) -> None:
    response = client.post("/api/v1/stock/query", json=payload)
    assert response.status_code == status_code
    assert response.json()["error_code"] in {"INVALID_ARGUMENT", "GUARDRAIL_VIOLATION"}


def test_search_rejects_oversized_limit(client: TestClient) -> None:
    assert client.get("/api/v1/goods/search", params={"limit": 99}).status_code == 422


def test_admin_seed_reports_counts(client: TestClient) -> None:
    body = client.post("/api/v1/admin/seed", json={"reset": True}).json()
    assert body["rows"] == {"goods": 12, "stock": 11, "coupon": 9, "user_preference": 5}


def test_categories_helper(client: TestClient) -> None:
    assert set(client.get("/api/v1/goods-meta/categories").json()) == {
        "phone",
        "earphone",
        "tablet",
        "laptop",
        "accessory",
    }


def test_token_required_when_configured(mem_engine: Engine) -> None:
    settings = Settings(APP_ENV="test", BUSINESS_API_TOKEN="secret-value", SEED_ON_STARTUP=False)
    with TestClient(create_app(engine=mem_engine, settings=settings)) as guarded:
        assert guarded.get("/api/v1/goods/search").status_code == 401
        assert guarded.get("/api/v1/goods/search", headers={"X-Internal-Token": "secret-value"}).status_code == 200
