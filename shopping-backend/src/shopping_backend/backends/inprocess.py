"""`inprocess` driver: the graph calls the services directly (no socket, no second process).

Rows are handed to the shared guardrail as plain dicts, exactly like the HTTP drivers do, which is
what makes `tests/test_backends.py` able to assert one contract across all three.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from shopping_backend.backends.base import CouponBundleRaw, StorefrontBackend
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.schemas import GoodsQuery
from shopping_backend.services import GENERAL_KEY, Services


class InProcessBackend(StorefrontBackend):
    driver = "inprocess"

    def __init__(self, services: Services) -> None:
        self._services = services

    async def _fetch_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> list[dict[str, Any]]:
        items = await asyncio.to_thread(self._services.goods.search, query)
        return [item.model_dump(mode="json") for item in items]

    async def _fetch_stock(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> dict[str, Any]:
        rows = await asyncio.to_thread(self._services.stock.query, goods_ids)
        return {row.goods_id: row.model_dump(mode="json") for row in rows}

    async def _fetch_coupon(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> CouponBundleRaw:
        selection = await asyncio.to_thread(self._services.coupon.pick, goods_ids)
        keyed: dict[str, list[dict[str, Any]]] = {
            goods_id: [coupon.model_dump(mode="json") for coupon in coupons]
            for goods_id, coupons in selection.by_goods.items()
        }
        if selection.general:
            keyed[GENERAL_KEY] = [coupon.model_dump(mode="json") for coupon in selection.general]
        return CouponBundleRaw(
            keyed=keyed,
            unavailable=[item.model_dump(mode="json") for item in selection.unavailable],
        )

    async def health(self) -> dict[str, Any]:
        return {"driver": self.driver, "transport": "in-process", "categories": self._services.goods.categories()}

    def supports_ids(self, goods_ids: Iterable[str]) -> list[str]:
        return [item.id for item in self._services.goods.list_by_ids(list(goods_ids))]
