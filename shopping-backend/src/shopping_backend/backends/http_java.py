"""`http_java` driver: the real Java backend, expressed as two mapping tables.

Everything that differs from the mock is *protocol*, never business meaning: endpoint paths,
field names, and money units. The moment a mapping stops being a rename, it belongs in the
business layer, not here.

TODO(对接确认): the paths/aliases below are a plausible convention, not a spec. Re-read them
against your Java interface doc; `tests/test_backends.py::test_http_java_*` pins the behaviour of
whatever you settle on, so the mapping is verified without a running Java service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shopping_backend.backends.base import CouponBundleRaw
from shopping_backend.backends.http import HttpBackend, _dig
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.schemas import GoodsQuery
from shopping_backend.services import GENERAL_KEY


def _yuan_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    return round(float(value) * 100)


def _cents_to_yuan(value: Any) -> float | None:
    if value is None:
        return None
    return round(int(value) / 100, 2)


def _rate_to_permille(value: Any) -> int | None:
    """Java `discountRate: 0.9` -> our permille `900` (docs/SHOPPING_RULES.md §2.3)."""
    if value is None:
        return None
    return round(float(value) * 1000)


class HttpJavaBackend(HttpBackend):
    driver = "http_java"

    PATHS = {
        "search_goods": "/api/product/list",
        "get_stock": "/api/sku/stock/batch",
        "get_coupon": "/api/promotion/available",
        "health": "/actuator/health",
    }
    ROWS_PATH = {
        "search_goods": "data.resultList",
        "get_stock": "data.skuStockMap",
        "get_coupon": "data.couponMap",
    }
    # our field -> their query/body parameter
    REQUEST_ALIASES: dict[str, dict[str, str]] = {
        "search_goods": {
            "q": "keyword",
            "category": "categoryId",
            "min_price": "minPrice",
            "max_price": "maxPrice",
            "limit": "pageSize",
        },
        "get_stock": {"goods_ids": "skuIds"},
        "get_coupon": {"goods_ids": "skuIds"},
    }
    # their row field -> our DTO field (converters applied by name)
    ROW_ALIASES: dict[str, dict[str, str]] = {
        "search_goods": {
            "productId": "id",
            "productCode": "sku",
            "productName": "title",
            "categoryId": "category",
            "brandName": "brand",
            "brief": "description",
            "priceYuan": "price_cents",
            "currencyCode": "currency",
            "tagNames": "tags",
            "shelfStatus": "status",
        },
        "get_stock": {
            "stockQty": "on_hand",
            "lockQty": "reserved",
            "storeCode": "warehouse",
        },
        "get_coupon": {
            "couponId": "id",
            "couponCode": "code",
            "couponName": "name",
            "promotionType": "scope",
            "scopeIds": "scope_value",
            "startAmount": "threshold_cents",
            "discountType": "discount_type",
            "discountRate": "discount_value",
            "discountAmount": "discount_value",
            "endTime": "valid_to",
            "sendCount": "used_count",
            "totalStock": "total_quota",
        },
    }
    CONVENTIONS = {
        "priceYuan": _yuan_to_cents,
        "startAmount": _yuan_to_cents,
        "discountAmount": _yuan_to_cents,
        "discountRate": _rate_to_permille,
    }
    SHELF_STATUS_MAP = {"ON_SALE": "on_sale", "OFF_SHELF": "off_sale", "DRAFT": "draft"}
    SCOPE_MAP = {"ALL": "all", "CATEGORY": "category", "SKU": "goods", "GOODS": "goods"}
    DISCOUNT_TYPE_MAP = {
        "FULL_REDUCE": "amount",
        "AMOUNT": "amount",
        "DISCOUNT": "percent",
        "PERCENT": "percent",
        "FREE_SHIPPING": "shipping",
        "SHIPPING": "shipping",
    }

    async def _fetch_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> list[dict[str, Any]]:
        params: dict[str, Any] = {self._param("search_goods", "limit"): query.limit}
        if query.query:
            params[self._param("search_goods", "q")] = query.query
        if query.category:
            params[self._param("search_goods", "category")] = query.category
        if query.min_price_cents is not None:
            params[self._param("search_goods", "min_price")] = _cents_to_yuan(query.min_price_cents)
        if query.max_price_cents is not None:
            params[self._param("search_goods", "max_price")] = _cents_to_yuan(query.max_price_cents)

        payload = await self._call("search_goods", params=params)
        rows = _dig(payload, self.ROWS_PATH["search_goods"]) or []
        return [self._rename_row("search_goods", row) for row in rows]

    async def _fetch_stock(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> Mapping[str, Any]:
        payload = await self._call("get_stock", json_body={self._param("get_stock", "goods_ids"): goods_ids})
        raw = _dig(payload, self.ROWS_PATH["get_stock"]) or {}
        stock: dict[str, Any] = {}
        for goods_id, entry in raw.items():
            entries = entry if isinstance(entry, list) else [entry]
            warehouses = [self._rename_row("get_stock", item) for item in entries if isinstance(item, Mapping)]
            for warehouse in warehouses:
                warehouse.pop("goods_id", None)
            stock[goods_id] = {"goods_id": goods_id, "warehouses": warehouses}
        return stock

    async def _fetch_coupon(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> CouponBundleRaw:
        payload = await self._call("get_coupon", json_body={self._param("get_coupon", "goods_ids"): goods_ids})
        raw = _dig(payload, self.ROWS_PATH["get_coupon"]) or {}
        keyed = {
            (GENERAL_KEY if key in ("", "*", "ALL") else key): [
                self._rename_row("get_coupon", row) for row in (value or [])
            ]
            for key, value in raw.items()
        }
        return CouponBundleRaw(
            keyed=keyed, unavailable=[self._rename_unavailable(row) for row in payload.get("unavailable") or []]
        )

    # ------------------------------------------------------------------ mapping helpers

    @classmethod
    def _param(cls, operation: str, ours: str) -> str:
        return cls.REQUEST_ALIASES.get(operation, {}).get(ours, ours)

    @classmethod
    def _rename_row(cls, operation: str, row: Mapping[str, Any]) -> dict[str, Any]:
        aliases = cls.ROW_ALIASES.get(operation, {})
        out: dict[str, Any] = {}
        for key, value in row.items():
            target = aliases.get(key, key)
            if key in cls.CONVENTIONS:
                value = cls.CONVENTIONS[key](value)
            if target == "status" and operation == "search_goods":
                value = cls.SHELF_STATUS_MAP.get(str(value), str(value).lower())
            if target == "scope":
                value = cls.SCOPE_MAP.get(str(value), str(value).lower())
            if target == "discount_type":
                value = cls.DISCOUNT_TYPE_MAP.get(str(value), str(value).lower())
            if target == "tags" and isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            out[target] = value
        if operation == "get_coupon":
            out.setdefault("discount_type", "amount")  # Java omits it for plain 满减
            out.setdefault("scope", "all")
        return out

    @staticmethod
    def _rename_unavailable(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "code": row.get("couponCode") or row.get("code") or "",
            "reason": str(row.get("unavailableReason") or row.get("reason") or "expired").lower(),
        }
