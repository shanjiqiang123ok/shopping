"""Coupon availability — the six-step rule of docs/SHOPPING_RULES.md §2.3, in one place.

Ordering of the checks matters for the diagnostics: a paused coupon must not also be reported as
expired, so each rule short-circuits into a single reason code.

Not implemented here (and deliberately so): per-user claim history. `per_user_limit` is surfaced in
the DTO, but without a `user_coupon_claim` table the Agent cannot know whether a user already used
a coupon — claiming otherwise would be a fabricated fact. Tracked as an M7 extension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.engine import Engine
from sqlmodel import select

from shopping_backend.core.logging import logger
from shopping_backend.db.session import session_scope
from shopping_backend.domain.context import ensure_utc, utcnow
from shopping_backend.domain.enums import CouponScope, CouponStatus, DiscountType, UnavailableReason
from shopping_backend.domain.formatting import format_coupon_display
from shopping_backend.domain.models_orm import Coupon, Goods
from shopping_backend.domain.schemas import CouponOut, UnavailableCoupon

GENERAL_KEY = "*"  # R-C4: coupons with no goods context (「有什么满减活动」)
# R-C1 demo口径: 免邮折算成一个固定小权重，避免免券压过真实满减（否则耳机券 ¥20 会被免邮挤出展示）
SHIPPING_WEIGHT_CENTS = 1000
GENERAL_LIMIT = 8

# R-C5 extension point: add a "stacking": N entry when combination maths is required.
_STRATEGY_COUNTS = {"single_best": 1}


@dataclass(slots=True)
class CouponSelection:
    by_goods: dict[str, list[CouponOut]] = field(default_factory=dict)
    general: list[CouponOut] = field(default_factory=list)
    unavailable: list[UnavailableCoupon] = field(default_factory=list)

    def codes_for(self, goods_id: str) -> list[str]:
        return [coupon.code for coupon in self.by_goods.get(goods_id, [])]

    def flatten(self) -> list[CouponOut]:
        seen: dict[str, CouponOut] = {}
        for coupon in [*self.general, *(c for rows in self.by_goods.values() for c in rows)]:
            seen.setdefault(coupon.code, coupon)
        return list(seen.values())


def strength_cents(coupon: Coupon, price_cents: int) -> int:
    """R-C1: every discount type converted to cents so they can be ranked together."""
    if coupon.discount_type == DiscountType.AMOUNT.value:
        return coupon.discount_value
    if coupon.discount_type == DiscountType.PERCENT.value:
        return round(price_cents * (1000 - coupon.discount_value) / 1000)
    if coupon.discount_type == DiscountType.SHIPPING.value:
        return SHIPPING_WEIGHT_CENTS
    return 0


def _matches_scope(coupon: Coupon, goods: Goods) -> bool:
    values = [value.strip().lower() for value in (coupon.scope_value or "").split(",") if value.strip()]
    if coupon.scope == CouponScope.ALL.value:
        return True
    if coupon.scope == CouponScope.CATEGORY.value:
        return goods.category.lower() in values
    if coupon.scope == CouponScope.GOODS.value:
        return goods.id in [value.strip() for value in (coupon.scope_value or "").split(",") if value.strip()]
    return False


def _to_dto(coupon: Coupon, *, reference_price_cents: int) -> CouponOut:
    dto = CouponOut(
        id=coupon.id,
        code=coupon.code,
        name=coupon.name,
        scope=CouponScope(coupon.scope),
        scope_value=coupon.scope_value,
        threshold_cents=coupon.threshold_cents,
        discount_type=DiscountType(coupon.discount_type),
        discount_value=coupon.discount_value,
        valid_from=coupon.valid_from,
        valid_to=coupon.valid_to,
        total_quota=coupon.total_quota,
        remaining_quota=(None if coupon.total_quota < 0 else max(coupon.total_quota - coupon.used_count, 0)),
        per_user_limit=coupon.per_user_limit,
        status=CouponStatus(coupon.status),
    )
    return dto.model_copy(update={"display": format_coupon_display(dto)})


class CouponService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pick(
        self,
        goods_ids: list[str],
        *,
        now: datetime | None = None,
        strategy: str = "single_best",
    ) -> CouponSelection:
        """`strategy` is the documented extension point (R-C5 stacking is not implemented)."""
        moment = ensure_utc(now) or utcnow()
        selection = CouponSelection()

        with session_scope(self._engine) as session:
            coupons = list(session.exec(select(Coupon)).all())
            ids = list(dict.fromkeys(goods_ids))
            goods_rows: list[Goods] = []
            if ids:
                goods_rows = list(session.exec(select(Goods).where(Goods.id.in_(ids))).all())
            goods_by_id = {row.id: row for row in goods_rows}

        if not ids:
            self._select_general(coupons, selection, moment)
            return selection

        for goods_id in ids:
            goods = goods_by_id.get(goods_id)
            if goods is None:
                continue
            candidates: list[tuple[Coupon, CouponOut, int]] = []
            for coupon in coupons:
                reason = self._reject_reason(coupon, moment)
                if reason is not None:
                    self._record_unavailable(selection, coupon, reason)
                    continue
                if not _matches_scope(coupon, goods):
                    self._record_unavailable(selection, coupon, UnavailableReason.SCOPE_MISMATCH)
                    continue
                if goods.price_cents < coupon.threshold_cents:
                    self._record_unavailable(selection, coupon, UnavailableReason.BELOW_THRESHOLD)
                    continue
                candidates.append(
                    (
                        coupon,
                        _to_dto(coupon, reference_price_cents=goods.price_cents),
                        strength_cents(coupon, goods.price_cents),
                    )
                )

            candidates.sort(key=lambda triple: (-triple[2], triple[0].threshold_cents, triple[0].valid_to))
            take = _STRATEGY_COUNTS.get(strategy)
            if take is None:
                logger.info("coupon_strategy_unimplemented", requested=strategy, used="single_best")
                take = _STRATEGY_COUNTS["single_best"]
            selection.by_goods[goods_id] = [dto for _, dto, _ in candidates[:take]]
        return selection

    def _select_general(self, coupons: list[Coupon], selection: CouponSelection, moment: datetime) -> None:
        """R-C4: no goods context -> only scope=all, valid, in-quota coupons, ranked, capped."""
        scored: list[tuple[Coupon, CouponOut, int]] = []
        for coupon in coupons:
            if coupon.scope != CouponScope.ALL.value:
                continue
            reason = self._reject_reason(coupon, moment)
            if reason is not None:
                self._record_unavailable(selection, coupon, reason)
                continue
            reference = max(coupon.threshold_cents, 1)
            scored.append(
                (coupon, _to_dto(coupon, reference_price_cents=reference), strength_cents(coupon, reference))
            )
        scored.sort(key=lambda triple: (-triple[2], triple[0].threshold_cents, triple[0].valid_to))
        selection.general = [dto for _, dto, _ in scored[:GENERAL_LIMIT]]

    @staticmethod
    def _reject_reason(coupon: Coupon, moment: datetime) -> UnavailableReason | None:
        if coupon.status == CouponStatus.PAUSED.value:
            return UnavailableReason.PAUSED
        if coupon.status != CouponStatus.ACTIVE.value:
            return UnavailableReason.EXPIRED
        valid_from = ensure_utc(coupon.valid_from)
        valid_to = ensure_utc(coupon.valid_to)
        if valid_from is not None and moment < valid_from:
            return UnavailableReason.EXPIRED
        if valid_to is not None and moment > valid_to:
            return UnavailableReason.EXPIRED
        if 0 <= coupon.total_quota <= coupon.used_count:
            return UnavailableReason.QUOTA_USED_UP
        return None

    @staticmethod
    def _record_unavailable(selection: CouponSelection, coupon: Coupon, reason: UnavailableReason) -> None:
        if not any(item.code == coupon.code and item.reason == reason for item in selection.unavailable):
            selection.unavailable.append(UnavailableCoupon(code=coupon.code, reason=reason))
