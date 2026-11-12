"""L4 (row-by-row output validation) and L5 (field whitelist into state).

Backends call this before anything reaches the graph, so a dirty row can never become a "fact"
the summary node would render. Dropping is per row, not per batch: one bad product must not wipe
the whole recommendation list (that would turn a data bug into an empty answer).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from shopping_backend.core.logging import logger
from shopping_backend.domain.context import ensure_utc, utcnow
from shopping_backend.domain.enums import CouponStatus, GoodsStatus
from shopping_backend.domain.schemas import CouponOut, GoodsOut, StockOut

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class DroppedRow:
    position: int
    ref: str
    reason: str


@dataclass(slots=True)
class RowsResult(Generic[T]):
    kept: list[T] = field(default_factory=list)
    dropped: list[DroppedRow] = field(default_factory=list)
    error_msg: str | None = None

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    @property
    def degraded(self) -> bool:
        return bool(self.dropped)

    @property
    def dicts(self) -> list[dict[str, Any]]:
        """L5: re-serialising through the DTO is what enforces the field whitelist."""
        return [row.model_dump(mode="json") for row in self.kept]


def validate_rows(rows: Iterable[Any], model: type[T], *, label: str, limit: int | None = None) -> RowsResult[T]:
    kept: list[T] = []
    dropped: list[DroppedRow] = []

    for position, row in enumerate(rows):
        if limit is not None and len(kept) >= limit:
            break
        try:
            kept.append(model.model_validate(row))
        except ValidationError as exc:
            ref = _ref_of(row, position)
            dropped.append(DroppedRow(position=position, ref=ref, reason=_first_reason(exc)))
            logger.warning(
                "guardrail_L4_dropped_row",
                label=label,
                position=position,
                ref=ref,
                reason=_first_reason(exc),
            )

    error_msg = None
    if dropped and not kept:
        error_msg = "返回数据校验未通过"
    return RowsResult(kept=kept, dropped=dropped, error_msg=error_msg)


def validate_goods(rows: Iterable[Any], *, limit: int | None = None) -> RowsResult[GoodsOut]:
    """L4 + R-G1 second line of defense: an off-sale row returned by mistake is dropped here too."""
    result: RowsResult[GoodsOut] = validate_rows(rows, GoodsOut, label="goods", limit=limit)
    sellable: list[GoodsOut] = []
    for position, goods in enumerate(result.kept):
        if goods.status != GoodsStatus.ON_SALE:
            result.dropped.append(DroppedRow(position=position, ref=goods.id, reason="not_on_sale"))
            logger.warning("guardrail_L4_dropped_off_sale", ref=goods.id, status=goods.status.value)
            continue
        sellable.append(goods)
    result.kept = sellable
    if result.dropped and not result.kept:
        result.error_msg = "返回数据校验未通过"
    return result


def validate_stock(payload: Mapping[str, Any] | Iterable[Any], requested: Iterable[str]) -> RowsResult[StockOut]:
    """R-S2: a missing key becomes an explicit zero, never an absent entry the model must guess about."""
    requested_ids = list(dict.fromkeys(requested))

    rows: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        for goods_id, value in payload.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("goods_id", goods_id)
                rows[goods_id] = row
            else:
                rows[goods_id] = value
    else:
        for value in payload:
            goods_id = str(value.get("goods_id") if isinstance(value, Mapping) else getattr(value, "goods_id", ""))
            if goods_id:
                rows[goods_id] = value

    result = validate_rows(rows.values(), StockOut, label="stock")
    by_id = {stock.goods_id: stock for stock in result.kept}

    for goods_id in requested_ids:
        if goods_id not in by_id:
            by_id[goods_id] = StockOut(goods_id=goods_id)  # zero-fill, per R-S2
            logger.info("guardrail_L4_stock_zero_filled", ref=goods_id)

    ordered = [by_id[goods_id] for goods_id in requested_ids if goods_id in by_id]
    ordered.extend(stock for goods_id, stock in by_id.items() if goods_id not in requested_ids)
    result.kept = ordered
    result.error_msg = None
    return result


def validate_coupons(
    payload: Mapping[str, Any] | Iterable[Any], *, now: datetime | None = None
) -> RowsResult[CouponOut]:
    """Accepts `{goods_id: [coupon...]}` or a flat list; returns the flattened validated rows.

    Expiry and paused state are re-checked here even though the business service already filtered
    them (docs/GUARDRAILS.md §三): a driver that talks to a third-party promotion system must not
    be able to hand the graph an expired coupon.
    """
    if isinstance(payload, Mapping):
        flat: list[Any] = []
        for rows in payload.values():
            flat.extend(rows or [])
    else:
        flat = list(payload)

    result = validate_rows(flat, CouponOut, label="coupon")
    moment = ensure_utc(now) or utcnow()
    kept: list[CouponOut] = []
    for position, coupon in enumerate(result.kept):
        if coupon.status != CouponStatus.ACTIVE:
            result.dropped.append(
                DroppedRow(position=position, ref=coupon.code, reason=f"status_{coupon.status.value}")
            )
            logger.warning("guardrail_L4_dropped_coupon", ref=coupon.code, reason="not_active")
            continue
        valid_from, valid_to = ensure_utc(coupon.valid_from), ensure_utc(coupon.valid_to)
        if valid_to is not None and moment > valid_to:
            result.dropped.append(DroppedRow(position=position, ref=coupon.code, reason="expired"))
            logger.warning("guardrail_L4_dropped_coupon", ref=coupon.code, reason="expired")
            continue
        if valid_from is not None and moment < valid_from:
            result.dropped.append(DroppedRow(position=position, ref=coupon.code, reason="not_started"))
            continue
        kept.append(coupon)
    result.kept = kept
    if result.dropped and not result.kept:
        result.error_msg = "返回数据校验未通过"
    return result


def cross_check(goods_ids: Iterable[str], stock: Iterable[StockOut], coupons: Iterable[CouponOut]) -> list[str]:
    """Cross-domain consistency: stock/coupons must only talk about goods we actually saw."""
    allowed = set(goods_ids)
    warnings: list[str] = []
    for stock_row in stock:
        if stock_row.goods_id not in allowed:
            warnings.append(f"stock for unknown goods {stock_row.goods_id}")
    for coupon in coupons:
        for scope_value in coupon.scope_value:
            if coupon.scope_value and scope_value not in allowed and not scope_value.isalnum():
                warnings.append(f"coupon {coupon.code} references malformed scope value {scope_value}")
    return warnings


def _ref_of(row: Any, position: int) -> str:
    if isinstance(row, Mapping):
        return str(row.get("id") or row.get("goods_id") or row.get("code") or f"#{position}")
    return str(getattr(row, "id", None) or getattr(row, "goods_id", None) or f"#{position}")


def _first_reason(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "row"
    return f"{loc}: {first.get('type', 'invalid')}"
