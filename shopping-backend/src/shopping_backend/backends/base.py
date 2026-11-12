"""`StorefrontBackend` — the one integration surface between the graph and business data.

Three rules, both borrowed from commerce-agents, shape this whole module:

1. **Identity is never a tool argument.** Every method takes `ShoppingSessionContext`; the model
   cannot pass a `user_id`, so it cannot read somebody else's cart.
2. **The adapter never raises.** A timeout, a 5xx or a dirty row becomes an empty result plus an
   `error_msg`, because a failed lookup must not end the conversation (F7).
3. **Validation happens here, once.** Subclasses only produce *raw* rows; the ABC applies the
   post-call guardrail (L4/L5), which is why all three drivers behave identically and can be
   tested against one shared contract matrix.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from shopping_backend.core.config import Settings
from shopping_backend.core.errors import ConfigError, NotOffered, ShoppingError, Unavailable, friendly_message
from shopping_backend.core.logging import logger
from shopping_backend.domain.context import ShoppingSessionContext
from shopping_backend.domain.formatting import format_coupon_display
from shopping_backend.domain.schemas import CouponOut, GoodsOut, GoodsQuery, StockOut, UnavailableCoupon
from shopping_backend.guardrails.validate_output import RowsResult, validate_coupons, validate_goods, validate_stock
from shopping_backend.services import GENERAL_KEY

T = TypeVar("T")


@dataclass(slots=True)
class BackendResult(Generic[T]):
    """commerce-agents `ToolOutcome`: what the model reads, plus whether it is trustworthy."""

    value: T | None = None
    error_msg: str | None = None
    degraded: bool = False
    dropped_rows: int = 0
    source: str = ""

    @classmethod
    def ok(
        cls, value: T, *, source: str = "", dropped_rows: int = 0, error_msg: str | None = None
    ) -> BackendResult[T]:
        return cls(
            value=value,
            source=source,
            dropped_rows=dropped_rows,
            error_msg=error_msg,
            degraded=bool(dropped_rows) or error_msg is not None,
        )

    @classmethod
    def failure(cls, error_msg: str, *, source: str = "") -> BackendResult[T]:
        return cls(value=None, error_msg=error_msg, degraded=True, source=source)

    @property
    def usable(self) -> bool:
        return self.value is not None


@dataclass(slots=True)
class CouponBundleRaw:
    """What a driver's wire format decodes into, before validation."""

    keyed: dict[str, list[Any]] = field(default_factory=dict)
    unavailable: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class CouponBundle:
    by_goods: dict[str, list[CouponOut]] = field(default_factory=dict)
    general: list[CouponOut] = field(default_factory=list)
    unavailable: list[UnavailableCoupon] = field(default_factory=list)

    def keyed(self) -> dict[str, list[CouponOut]]:
        merged: dict[str, list[CouponOut]] = dict(self.by_goods)
        if self.general:
            merged[GENERAL_KEY] = self.general
        return merged

    def codes_for(self, goods_id: str) -> list[str]:
        return [coupon.code for coupon in self.by_goods.get(goods_id, [])]

    def flatten(self) -> list[CouponOut]:
        seen: dict[str, CouponOut] = {}
        for coupon in [*self.general, *(c for rows in self.by_goods.values() for c in rows)]:
            seen.setdefault(coupon.code, coupon)
        return list(seen.values())

    def as_state(self) -> dict[str, list[dict[str, Any]]]:
        return {key: [coupon.model_dump(mode="json") for coupon in rows] for key, rows in self.keyed().items()}


class StorefrontBackend(ABC):
    driver: str = "abstract"

    # ------------------------------------------------------------------ public API (graph calls these)

    async def search_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> BackendResult[list[GoodsOut]]:
        rows, error = await self._guarded(ctx, "search_goods", lambda: self._fetch_goods(ctx, query))
        if error is not None:
            return BackendResult.failure(error, source=self.driver)
        result: RowsResult[GoodsOut] = validate_goods(rows)
        self._log_drops("search_goods", result)
        return BackendResult.ok(
            result.kept,
            source=self.driver,
            dropped_rows=result.dropped_count,
            error_msg=result.error_msg,
        )

    async def get_stock(
        self, ctx: ShoppingSessionContext, goods_ids: Iterable[str]
    ) -> BackendResult[dict[str, StockOut]]:
        requested = list(dict.fromkeys(goods_ids))
        rows, error = await self._guarded(ctx, "get_stock", lambda: self._fetch_stock(ctx, requested))
        if error is not None:
            return BackendResult.failure(error, source=self.driver)
        payload = rows if isinstance(rows, Mapping) else list(rows or [])
        result = validate_stock(payload, requested)
        self._log_drops("get_stock", result)
        by_id = {stock.goods_id: stock for stock in result.kept}
        return BackendResult.ok(
            by_id, source=self.driver, dropped_rows=result.dropped_count, error_msg=result.error_msg
        )

    async def get_coupon(self, ctx: ShoppingSessionContext, goods_ids: Iterable[str]) -> BackendResult[CouponBundle]:
        requested = list(dict.fromkeys(goods_ids))
        rows, error = await self._guarded(ctx, "get_coupon", lambda: self._fetch_coupon(ctx, requested))
        if error is not None:
            return BackendResult.failure(error, source=self.driver)

        bundle_raw: CouponBundleRaw = rows if isinstance(rows, CouponBundleRaw) else CouponBundleRaw()
        bundle = CouponBundle(
            unavailable=[_coerce_unavailable(item) for item in bundle_raw.unavailable],
        )
        dropped = 0
        error_msg: str | None = None

        # Validate per bucket instead of flattening and re-associating by key: a driver may hand us
        # rows whose wire keys are not ours yet (the Java payload speaks `couponCode`), so the only
        # reliable identity is "which bucket did this row come from".
        for key, rows_for_key in bundle_raw.keyed.items():
            validated = validate_coupons(list(rows_for_key or []))
            dropped += validated.dropped_count
            error_msg = error_msg or validated.error_msg
            if key == GENERAL_KEY:
                bundle.general = _with_display(validated.kept)
            else:
                bundle.by_goods[key] = _with_display(validated.kept)

        self._log_drops("get_coupon", RowsResult(kept=bundle.flatten(), dropped=[], error_msg=error_msg))
        return BackendResult.ok(
            bundle,
            source=self.driver,
            dropped_rows=dropped,
            error_msg=error_msg,
        )

    async def health(self) -> dict[str, Any]:
        return {"driver": self.driver}

    async def aclose(self) -> None:  # noqa: B027 - intentionally optional: in-process has nothing to close
        """Release network/pool resources. Overridden by the HTTP drivers; a default no-op means
        every driver is safe to close uniformly in the application lifespan."""
        return None

    # ------------------------------------------------------------------ driver protocol (raw rows only)

    @abstractmethod
    async def _fetch_goods(self, ctx: ShoppingSessionContext, query: GoodsQuery) -> Iterable[Any]: ...

    @abstractmethod
    async def _fetch_stock(
        self, ctx: ShoppingSessionContext, goods_ids: list[str]
    ) -> Mapping[str, Any] | Iterable[Any]: ...

    @abstractmethod
    async def _fetch_coupon(self, ctx: ShoppingSessionContext, goods_ids: list[str]) -> CouponBundleRaw: ...

    # ------------------------------------------------------------------ shared plumbing

    async def _guarded(
        self, ctx: ShoppingSessionContext, operation: str, call: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, str | None]:
        """F7: convert every failure into `(None, message)`; the graph always continues."""
        try:
            return await call(), None
        except NotOffered as exc:
            logger.info("backend_not_offered", driver=self.driver, operation=operation, trace_id=ctx.trace_id)
            return None, exc.friendly
        except (Unavailable, TimeoutError) as exc:
            logger.warning("backend_unavailable", driver=self.driver, operation=operation, error=str(exc)[:200])
            return None, Unavailable.friendly
        except ShoppingError as exc:
            logger.warning("backend_shopping_error", driver=self.driver, operation=operation, code=exc.code)
            return None, exc.friendly
        except Exception as exc:  # a tool failure must not end the turn
            logger.error(
                "backend_unexpected", driver=self.driver, operation=operation, error=f"{type(exc).__name__}: {exc}"
            )
            return None, friendly_message(exc)

    def _log_drops(self, operation: str, result: RowsResult) -> None:
        if result.dropped_count:
            logger.warning(
                "guardrail_L4_summary",
                driver=self.driver,
                operation=operation,
                kept=len(result.kept),
                dropped=result.dropped_count,
                reasons=[drop.reason for drop in result.dropped][:10],
            )


def _with_display(coupons: list[CouponOut]) -> list[CouponOut]:
    """The coupon label is derived from the validated numbers, never taken from the wire.

    A backend that sends a blank (or wrong) `display` would otherwise leak into the user-facing
    answer, and O4 requires the threshold to appear next to the discount.
    """
    return [coupon.model_copy(update={"display": format_coupon_display(coupon)}) for coupon in coupons]


def _coerce_unavailable(item: Any) -> UnavailableCoupon:
    payload = (
        dict(item)
        if isinstance(item, Mapping)
        else {"code": getattr(item, "code", ""), "reason": getattr(item, "reason", "")}
    )
    try:
        return UnavailableCoupon.model_validate(payload)
    except Exception:  # diagnostics only; a malformed reason must not poison the answer
        return UnavailableCoupon(code=str(payload.get("code", ""))[:32], reason="expired")


def build_backend(settings: Settings, *, services: Any = None, http_client: Any = None) -> StorefrontBackend:
    """Driver choice is a single env var; the graph never learns which one is live."""
    driver = settings.BACKEND_DRIVER
    if driver == "inprocess":
        from shopping_backend.backends.inprocess import InProcessBackend

        if services is None:
            msg = "inprocess driver needs the services container"
            raise NotOffered(msg)
        return InProcessBackend(services)

    if driver == "http":
        from shopping_backend.backends.http import HttpBackend

        return HttpBackend(settings, client=http_client)

    if driver == "http_java":
        from shopping_backend.backends.http_java import HttpJavaBackend

        return HttpJavaBackend(settings, client=http_client)

    msg = f"unknown BACKEND_DRIVER: {driver!r} (expected inprocess | http | http_java)"
    raise ConfigError(msg)
