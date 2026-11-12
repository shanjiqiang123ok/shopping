"""`http` driver: the graph talks to the business API over HTTP.

This is the rehearsal for the real Java integration — same transport code, different base URL.
Timeout/retry/auth live here once, so no node ever has to think about them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from shopping_backend.backends.base import CouponBundleRaw, StorefrontBackend
from shopping_backend.core.config import Settings
from shopping_backend.core.errors import NotOffered, Unavailable
from shopping_backend.core.logging import logger
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.schemas import GoodsQuery

RETRYABLE = (httpx.TransportError, httpx.TimeoutException, TimeoutError)


class HttpBackend(StorefrontBackend):
    """Read-only endpoints, so every call here is safe to retry (idempotent by construction)."""

    driver = "http"
    PATHS: dict[str, str] = {
        "search_goods": "/api/v1/goods/search",
        "get_stock": "/api/v1/stock/query",
        "get_coupon": "/api/v1/coupon/query",
        "health": "/health",
    }
    ROWS_PATH: dict[str, str] = {
        "search_goods": "items",
        "get_stock": "stock",
        "get_coupon": "coupons",
    }

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._auth_headers = self._headers(settings)
        self._client = client or httpx.AsyncClient(
            base_url=settings.BUSINESS_API_BASE_URL,
            timeout=httpx.Timeout(settings.BUSINESS_API_TIMEOUT_S),
            headers=self._auth_headers,
        )

    @staticmethod
    def _headers(settings: Settings) -> dict[str, str]:
        token = settings.BUSINESS_API_TOKEN.strip()
        return {"X-Internal-Token": token} if token else {}

    # ------------------------------------------------------------------ protocol

    async def _fetch_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": query.limit}
        if query.query:
            params["q"] = query.query
        if query.category:
            params["category"] = query.category
        if query.min_price_cents is not None:
            params["min_price"] = query.min_price_cents / 100
        if query.max_price_cents is not None:
            params["max_price"] = query.max_price_cents / 100
        if query.include_off_sale:
            params["include_off_sale"] = "true"
        payload = await self._call("search_goods", params=params)
        return list(_dig(payload, self.ROWS_PATH["search_goods"]) or [])

    async def _fetch_stock(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> Mapping[str, Any]:
        payload = await self._call("get_stock", json_body={"goods_ids": goods_ids})
        rows = _dig(payload, self.ROWS_PATH["get_stock"])
        return dict(rows or {})

    async def _fetch_coupon(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> CouponBundleRaw:
        payload = await self._call("get_coupon", json_body={"goods_ids": goods_ids})
        keyed = {key: list(value or []) for key, value in (_dig(payload, self.ROWS_PATH["get_coupon"]) or {}).items()}
        return CouponBundleRaw(keyed=keyed, unavailable=list(payload.get("unavailable") or []))

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self._call("health")
        except Unavailable as exc:
            return {
                "driver": self.driver,
                "base_url": self._settings.BUSINESS_API_BASE_URL,
                "reachable": False,
                "error": str(exc)[:120],
            }
        return {
            "driver": self.driver,
            "base_url": self._settings.BUSINESS_API_BASE_URL,
            "reachable": True,
            "rows": payload.get("rows"),
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ transport

    async def _call(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> dict[str, Any]:
        """Explicit retry loop.

        Not tenacity: every endpoint here is a read, so the only question is "was it a transport
        failure?", and an inline loop states that without depending on iterator-API semantics.
        """
        path = self.PATHS[operation]
        method = "POST" if json_body is not None else "GET"
        attempts = max(1, self._settings.BUSINESS_API_MAX_RETRY + 1)
        last: Exception | None = None

        for attempt in range(attempts):
            try:
                # headers go per-request: an injected client (tests, a shared pool) must not be
                # able to silently skip the service credential
                response = await self._client.request(
                    method,
                    path,
                    params=dict(params or {}),
                    json=json_body,
                    headers=self._auth_headers,
                    timeout=self._settings.BUSINESS_API_TIMEOUT_S,
                )
                return self._decode(operation, response)
            except RETRYABLE as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
                logger.warning(
                    "backend_http_retry",
                    driver=self.driver,
                    operation=operation,
                    attempt=attempt + 1,
                    of=attempts,
                    error=str(exc)[:120],
                )
                await asyncio.sleep(min(1.5, 0.2 * 2**attempt))

        raise Unavailable(f"{operation}: {type(last).__name__} after {attempts} attempt(s)")

    def _decode(self, operation: str, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status == 404:
            raise NotOffered(f"{operation}: not offered by the business API (404)")
        if status in (401, 403):
            logger.error("backend_http_auth_rejected", driver=self.driver, operation=operation, status=status)
            raise Unavailable(f"{operation}: rejected by the business API ({status})")
        if status == 422:
            # a schema-level refusal is our bug, not a data outage; logged loudly, reported as empty
            logger.error("backend_http_422", driver=self.driver, operation=operation, body=response.text[:200])
            raise NotOffered(f"{operation}: request rejected (422)")
        if status >= 500:
            raise Unavailable(f"{operation}: business API error ({status})")
        if status >= 400:
            raise NotOffered(f"{operation}: request failed ({status})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise Unavailable(f"{operation}: response is not JSON") from exc
        return payload if isinstance(payload, dict) else {"items": payload}


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    """Support dotted paths (`data.resultList`) so the Java driver can reuse `_call`."""
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current
