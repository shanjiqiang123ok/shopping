"""M3 exit gate: the three drivers must be *interchangeable*.

One assertion table runs against inprocess (direct services), http (the mock business API over
ASGI) and http_java (a fake Java server speaking yuan, `skuIds`, `ON_SALE`...). Any divergence is
a contract bug, not a preference.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from shopping_backend.backends.base import BackendResult, CouponBundle, StorefrontBackend, build_backend
from shopping_backend.backends.http import HttpBackend
from shopping_backend.backends.http_java import HttpJavaBackend
from shopping_backend.backends.inprocess import InProcessBackend
from shopping_backend.business_api.main import create_app
from shopping_backend.core.config import Settings
from shopping_backend.core.errors import ConfigError, Unavailable
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.enums import GoodsStatus
from shopping_backend.domain.schemas import GoodsOut, GoodsQuery, StockOut
from shopping_backend.services import Services, build_services

EXPECTED_PHONE_IDS = {"g_0001", "g_0002", "g_0003"}
SCOPE_TO_JAVA = {"all": "ALL", "category": "CATEGORY", "goods": "SKU"}
DISCOUNT_TO_JAVA = {"amount": "FULL_REDUCE", "percent": "DISCOUNT", "shipping": "FREE_SHIPPING"}


class FakeJavaServer:
    """Speaks the wire format we *assume* the Java backend uses, so the alias tables get tested."""

    def __init__(self, services: Services) -> None:
        self._services = services
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/product/list":
            keyword = request.url.params.get("keyword", "")
            category = request.url.params.get("categoryId")
            max_price = request.url.params.get("maxPrice")
            limit = int(request.url.params.get("pageSize", "8"))
            query = GoodsQuery(
                query=keyword,
                category=category,
                max_price_cents=None if max_price is None else round(float(max_price) * 100),
                limit=limit,
            )
            rows = self._services.goods.search(query)
            return httpx.Response(200, json={"data": {"resultList": [self._product(row) for row in rows]}})

        if path == "/api/sku/stock/batch":
            ids = json.loads(request.content.decode("utf-8"))["skuIds"]
            stock = {row.goods_id: row for row in self._services.stock.query(ids)}
            encoded: dict[str, list[dict[str, Any]]] = {}
            for goods_id in ids:
                row = stock.get(goods_id)
                if row is None:
                    continue  # Java omits unknown skus; our guardrail must not
                encoded[goods_id] = [
                    {"storeCode": warehouse.warehouse, "stockQty": warehouse.on_hand, "lockQty": warehouse.reserved}
                    for warehouse in row.warehouses
                ]
            return httpx.Response(200, json={"data": {"skuStockMap": encoded}})

        if path == "/api/promotion/available":
            ids = json.loads(request.content.decode("utf-8"))["skuIds"]
            selection = self._services.coupon.pick(ids)
            keyed = {
                goods_id: [self._coupon(coupon) for coupon in coupons]
                for goods_id, coupons in selection.by_goods.items()
            }
            if selection.general:
                keyed["ALL"] = [self._coupon(coupon) for coupon in selection.general]
            return httpx.Response(
                200,
                json={
                    "data": {"couponMap": keyed},
                    "unavailable": [
                        {"couponCode": item.code, "unavailableReason": item.reason.value.upper()}
                        for item in selection.unavailable
                    ],
                },
            )

        if path == "/actuator/health":
            return httpx.Response(200, json={"status": "UP"})

        return httpx.Response(404, json={"error": "no such endpoint"})

    @staticmethod
    def _product(row: GoodsOut) -> dict[str, Any]:
        return {
            "productId": row.id,
            "productCode": row.sku,
            "productName": row.title,
            "categoryId": row.category,
            "brandName": row.brand,
            "brief": row.description,
            "priceYuan": row.price_cents / 100,
            "currencyCode": row.currency,
            "tagNames": ",".join(row.tags),
            "shelfStatus": {"on_sale": "ON_SALE", "off_sale": "OFF_SHELF", "draft": "DRAFT"}[row.status.value],
        }

    @staticmethod
    def _coupon(coupon) -> dict[str, Any]:
        used = 0 if coupon.remaining_quota is None else max(coupon.total_quota - coupon.remaining_quota, 0)
        payload: dict[str, Any] = {
            "couponId": coupon.id,
            "couponCode": coupon.code,
            "couponName": coupon.name,
            "promotionType": SCOPE_TO_JAVA[coupon.scope.value],
            "scopeIds": ",".join(coupon.scope_value),
            "startAmount": coupon.threshold_cents / 100,
            "discountType": DISCOUNT_TO_JAVA[coupon.discount_type.value],
            "endTime": coupon.valid_to.isoformat() if coupon.valid_to else None,
            "totalStock": coupon.total_quota,
            "sendCount": used,
        }
        if coupon.discount_type.value == "percent":
            payload["discountRate"] = coupon.discount_value / 1000
        elif coupon.discount_type.value == "amount":
            payload["discountAmount"] = coupon.discount_value / 100
        return payload


class BrokenServices:
    """Every domain raises: the adapter must degrade, not propagate (F7)."""

    class _Boom:
        async def _never(self):  # pragma: no cover - never awaited
            return None

        def search(self, *a, **kw):
            msg = "goods service exploded"
            raise RuntimeError(msg)

        def query(self, *a, **kw):
            msg = "stock service exploded"
            raise RuntimeError(msg)

        def pick(self, *a, **kw):
            msg = "coupon service exploded"
            raise RuntimeError(msg)

        def categories(self):
            return []

    def __init__(self) -> None:
        self.goods = self._Boom()
        self.stock = self._Boom()
        self.coupon = self._Boom()


@pytest.fixture
def ctx() -> ShoppingSessionContext:
    return ShoppingSessionContext(user_id="u_1001", session_id="s_matrix", trace_id="t_matrix")


@pytest.fixture
def java_server(mem_engine: Engine) -> FakeJavaServer:
    return FakeJavaServer(build_services(mem_engine))


def _make_driver(name: str, engine: Engine, java: FakeJavaServer) -> StorefrontBackend:
    services = build_services(engine)
    settings = Settings(APP_ENV="test", BUSINESS_API_TOKEN="", BUSINESS_API_MAX_RETRY=0, BUSINESS_API_TIMEOUT_S=5)

    if name == "inprocess":
        return InProcessBackend(services)
    if name == "http":
        app = create_app(
            engine=engine, settings=Settings(APP_ENV="test", SEED_ON_STARTUP=False, BUSINESS_API_TOKEN="")
        )
        transport = httpx.ASGITransport(app=app)
        return HttpBackend(settings, client=httpx.AsyncClient(transport=transport, base_url="http://business"))
    if name == "http_java":
        return HttpJavaBackend(
            settings, client=httpx.AsyncClient(transport=httpx.MockTransport(java.handle), base_url="http://java")
        )
    raise AssertionError(name)


@pytest.fixture(params=["inprocess", "http", "http_java"])
def driver(request, mem_engine: Engine, java_server: FakeJavaServer) -> StorefrontBackend:
    return _make_driver(request.param, mem_engine, java_server)


@pytest.mark.asyncio
async def test_matrix_search(driver: StorefrontBackend, ctx: ShoppingSessionContext) -> None:
    result = await driver.search_goods(ctx, GoodsQuery(category="phone", max_price_cents=200000, limit=8))
    assert isinstance(result, BackendResult)
    assert result.usable and result.error_msg is None
    assert {item.id for item in result.value or []} == EXPECTED_PHONE_IDS
    assert all(item.status == GoodsStatus.ON_SALE for item in result.value or [])
    assert all(item.price_cents <= 200000 for item in result.value or []), "the same rule must hold over the wire"


@pytest.mark.asyncio
async def test_matrix_search_by_text(driver: StorefrontBackend, ctx: ShoppingSessionContext) -> None:
    result = await driver.search_goods(ctx, GoodsQuery(query="耳机", limit=10))
    assert {item.id for item in result.value or []} == {"g_0006", "g_0007"}


@pytest.mark.asyncio
async def test_matrix_stock_aggregation(driver: StorefrontBackend, ctx: ShoppingSessionContext) -> None:
    result = await driver.get_stock(ctx, ["g_0001", "g_0006", "g_0008"])
    stock: dict[str, StockOut] = result.value or {}
    assert stock["g_0001"].available == 12
    assert stock["g_0006"].available == 0
    assert stock["g_0008"].available == 0, "no stock record must degrade to zero, never disappear (R-S2)"
    assert len(stock["g_0001"].warehouses) == 2


@pytest.mark.asyncio
async def test_matrix_coupon_single_best(driver: StorefrontBackend, ctx: ShoppingSessionContext) -> None:
    result = await driver.get_coupon(ctx, ["g_0001"])
    bundle = result.value
    assert bundle is not None
    assert bundle.codes_for("g_0001") == ["M1500_100"]
    assert bundle.as_state()["g_0001"][0]["display"] == "满¥1500.00减¥100.00"


@pytest.mark.asyncio
async def test_matrix_general_coupons(driver: StorefrontBackend, ctx: ShoppingSessionContext) -> None:
    result = await driver.get_coupon(ctx, [])
    bundle = result.value
    assert bundle is not None
    assert [coupon.code for coupon in bundle.general] == ["M5000_600", "FREESHIP"]


@pytest.mark.asyncio
async def test_matrix_failure_degrades_instead_of_raising(ctx: ShoppingSessionContext) -> None:
    """F7 across all three drivers: no exception escapes, the caller gets an empty degraded result."""

    def refuse(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg)

    drivers: list[StorefrontBackend] = [
        InProcessBackend(BrokenServices()),
        HttpBackend(
            Settings(APP_ENV="test", BUSINESS_API_MAX_RETRY=0, BUSINESS_API_TIMEOUT_S=5),
            client=httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://x"),
        ),
        HttpJavaBackend(
            Settings(APP_ENV="test", BUSINESS_API_MAX_RETRY=0, BUSINESS_API_TIMEOUT_S=5),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"error": "down"})),
                base_url="http://x",
            ),
        ),
    ]

    for backend in drivers:
        goods = await backend.search_goods(ctx, GoodsQuery(query="手机"))
        stock = await backend.get_stock(ctx, ["g_0001"])
        coupons = await backend.get_coupon(ctx, ["g_0001"])
        assert not goods.usable and goods.degraded and goods.error_msg, backend.driver
        assert not stock.usable and stock.degraded, backend.driver
        assert not coupons.usable and coupons.degraded, backend.driver
        assert "堆栈" not in (goods.error_msg or "") and "Traceback" not in (goods.error_msg or "")


@pytest.mark.asyncio
async def test_http_java_maps_yuan_to_cents(
    mem_engine: Engine, java_server: FakeJavaServer, ctx: ShoppingSessionContext
) -> None:
    backend = _make_driver("http_java", mem_engine, java_server)
    result = await backend.search_goods(ctx, GoodsQuery(category="phone", limit=8))
    by_id = {item.id: item for item in result.value or []}
    assert by_id["g_0001"].price_cents == 169900, "1699.0 元 in, 169900 分 out"
    assert by_id["g_0001"].tags == ["5G", "拍照", "长续航"], "comma-separated tagNames must become a list"
    assert "productId" not in by_id["g_0001"].model_dump()


@pytest.mark.asyncio
async def test_http_java_percent_discount_round_trip(
    mem_engine: Engine, java_server: FakeJavaServer, ctx: ShoppingSessionContext
) -> None:
    """`discountRate: 0.9` must arrive as permille 900, or every 折扣 coupon would be dropped."""
    backend = _make_driver("http_java", mem_engine, java_server)
    result = await backend.get_coupon(ctx, ["g_0011"])  # ¥129 charger: 9折 = ¥12.90 beats 免邮 = ¥10
    coupon = (result.value or CouponBundle()).by_goods["g_0011"][0]
    assert coupon.code == "ACC90"
    assert coupon.discount_type == "percent"
    assert coupon.discount_value == 900
    assert coupon.display == "9折"


@pytest.mark.asyncio
async def test_http_java_maps_unavailable_reasons(
    mem_engine: Engine, java_server: FakeJavaServer, ctx: ShoppingSessionContext
) -> None:
    backend = _make_driver("http_java", mem_engine, java_server)
    result = await backend.get_coupon(ctx, [])
    reasons = {item.code: item.reason.value for item in (result.value or CouponBundle()).unavailable}
    assert reasons["M500_50"] == "paused"
    assert reasons["M1000_200"] == "expired"
    assert reasons["M300_30"] == "quota_used_up"


@pytest.mark.asyncio
async def test_http_sends_internal_token(mem_engine: Engine, ctx: ShoppingSessionContext) -> None:
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Internal-Token", "")
        return httpx.Response(200, json={"items": []})

    settings = Settings(APP_ENV="test", BUSINESS_API_TOKEN="shared-secret", BUSINESS_API_MAX_RETRY=0)
    backend = HttpBackend(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(capture), base_url="http://x")
    )
    await backend.search_goods(ctx, GoodsQuery(query="手机"))
    assert seen["token"] == "shared-secret"


@pytest.mark.asyncio
async def test_http_retries_transport_errors_then_degrades(ctx: ShoppingSessionContext) -> None:
    attempts: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        msg = "connection reset"
        raise httpx.ConnectError(msg)

    settings = Settings(APP_ENV="test", BUSINESS_API_MAX_RETRY=2, BUSINESS_API_TIMEOUT_S=5)
    backend = HttpBackend(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(flaky), base_url="http://x")
    )
    result = await backend.search_goods(ctx, GoodsQuery(query="手机"))
    assert len(attempts) == 3, "BUSINESS_API_MAX_RETRY=2 means two retries plus the first attempt"
    assert result.degraded and "暂时无法获取商品信息" in (result.error_msg or "")


class _TimeoutTransport(httpx.AsyncBaseTransport):
    """ASGITransport does not enforce httpx timeouts, so this reproduces what a real socket does:
    record the effective per-request budget, then raise ReadTimeout."""

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.timeouts: list[Any] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(request.extensions.get("timeout"))
        msg = "simulated read timeout after BUSINESS_API_TIMEOUT_S elapsed"
        raise httpx.ReadTimeout(msg, request=request)


@pytest.mark.asyncio
async def test_timeout_setting_is_plumbed_and_degrades(ctx: ShoppingSessionContext) -> None:
    """Verification case 5: a too-small BUSINESS_API_TIMEOUT_S yields the degraded wording, not a hang."""
    settings = Settings(APP_ENV="test", BUSINESS_API_TIMEOUT_S=0.001, BUSINESS_API_MAX_RETRY=0)
    transport = _TimeoutTransport(None)
    # an injected client keeps its own 5s default; the backend must still apply the configured budget
    client = httpx.AsyncClient(transport=transport, base_url="http://business")
    backend = HttpBackend(settings, client=client)

    result = await backend.search_goods(ctx, GoodsQuery(query="手机", limit=3))
    assert transport.timeouts, "the request never carried a timeout"
    assert transport.timeouts[0]["read"] == pytest.approx(0.001), "BUSINESS_API_TIMEOUT_S must reach the wire"
    assert result.degraded is True
    assert not result.usable
    assert result.error_msg == Unavailable.friendly


@pytest.mark.asyncio
async def test_build_backend_factory(settings: Settings, mem_engine: Engine) -> None:
    services = build_services(mem_engine)
    assert build_backend(settings, services=services).driver == "inprocess"
    assert build_backend(settings.model_copy(update={"BACKEND_DRIVER": "http"})).driver == "http"
    assert build_backend(settings.model_copy(update={"BACKEND_DRIVER": "http_java"})).driver == "http_java"
    with pytest.raises(ConfigError):
        build_backend(settings.model_copy(update={"BACKEND_DRIVER": "carrier_pigeon"}), services=services)
