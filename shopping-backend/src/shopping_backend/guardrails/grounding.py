"""L7: provenance / anti-hallucination verification of the final answer.

The rendering layer already builds every number from state (ADR A6), so this is the *second*
line: it catches anything that entered the answer by another route (a model-authored reason that
leaked a number, a hand-written segment, a stale id restored from a checkpoint).

Detection is per line and the action is per line: a suspicious line is removed rather than the
whole answer discarded, so one bad sentence cannot turn a good recommendation into an apology.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from shopping_backend.core.config import settings
from shopping_backend.core.errors import DEGRADED_FALLBACK_TEXT
from shopping_backend.domain.formatting import format_price_cents, parse_price_token
from shopping_backend.domain.schemas import CouponOut, GoodsOut, StockOut

_GOODS_ID_RE = re.compile(r"\b(?:g|goods)_[0-9A-Za-z]{3,32}\b")
_RECORD_ID_RE = re.compile(r"\b(?:c|u|s)_[0-9A-Za-z]{3,32}\b")
_SKU_RE = re.compile(r"\b[A-Z]{2,6}-[A-Z0-9]{2,12}\b")
_COUPON_CODE_RE = re.compile(r"券码\s*([A-Z][A-Z0-9_]{2,24})")
_PRICE_RE = re.compile(r"[¥$€]\s*([0-9][0-9,]*(?:\.\d{1,2})?)")
_COUNT_RE = re.compile(r"(?:现货|仅)\s*([0-9]{1,6})\s*件")
_BANNED_CLAIM_RE = re.compile(
    r"(已下单|已购买|已付款|已支付|已发货|帮你下单|为你下单|已为你|库存已锁定|已锁定库存|下单链接|支付链接)"
)
_REASON_NUMBER_RE = re.compile(r"([¥$€]\s*\d|\d+\s*件|\d+(?:\.\d+)?\s*折|券码|[0-9]{3,})")


@dataclass(slots=True)
class Provenance:
    """Everything the answer is allowed to mention."""

    goods_ids: set[str] = field(default_factory=set)
    skus: set[str] = field(default_factory=set)
    coupon_codes: set[str] = field(default_factory=set)
    other_ids: set[str] = field(default_factory=set)
    prices_cents: set[int] = field(default_factory=set)
    available_counts: set[int] = field(default_factory=set)
    warehouses: set[str] = field(default_factory=set)

    @classmethod
    def from_state(
        cls,
        goods: list[GoodsOut],
        stock: dict[str, StockOut],
        coupons: list[CouponOut],
        *,
        extra_ids: list[str] | None = None,
    ) -> Provenance:
        prov = cls()
        for item in goods:
            prov.goods_ids.add(item.id)
            if item.sku:
                prov.skus.add(item.sku)
            prov.prices_cents.add(item.price_cents)
        for stock_row in stock.values():
            prov.available_counts.add(stock_row.available)
            for warehouse in stock_row.warehouses:
                prov.warehouses.add(warehouse.warehouse)
        for coupon in coupons:
            prov.coupon_codes.add(coupon.code)
            prov.other_ids.add(coupon.id)
            prov.prices_cents.add(coupon.threshold_cents)
            if coupon.discount_type == "amount":
                prov.prices_cents.add(coupon.discount_value)
        for goods_id in extra_ids or []:
            prov.goods_ids.add(goods_id)
        return prov


@dataclass(slots=True)
class GroundingReport:
    answer: str
    dropped_lines: list[tuple[int, str, str]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    fallback_used: bool = False

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_lines)

    @property
    def clean(self) -> bool:
        return not self.dropped_lines and not self.fallback_used


def verify_line(line: str, prov: Provenance, *, tolerance_cents: int | None = None) -> str | None:
    """Return a violation description for this line, or None when the line is fully grounded."""
    tolerance = tolerance_cents if tolerance_cents is not None else settings.GROUNDING_PRICE_TOLERANCE_CENTS

    if _BANNED_CLAIM_RE.search(line):
        return "forbidden_claim"

    for token in _GOODS_ID_RE.findall(line):
        if token not in prov.goods_ids:
            return f"unprovenance_goods_id:{token}"
    for token in _RECORD_ID_RE.findall(line):
        if token not in prov.other_ids and token not in prov.goods_ids:
            return f"unprovenance_id:{token}"
    for token in _SKU_RE.findall(line):
        if token not in prov.skus and token not in prov.warehouses:
            return f"unprovenance_sku:{token}"
    for token in _COUPON_CODE_RE.findall(line):
        if token not in prov.coupon_codes:
            return f"unprovenance_coupon_code:{token}"

    for raw in _PRICE_RE.findall(line):
        cents = parse_price_token(raw)
        if not _within_tolerance(cents, prov.prices_cents, tolerance):
            return f"price_not_in_state:{raw}"

    for raw in _COUNT_RE.findall(line):
        count = int(raw)
        if count not in prov.available_counts:
            return f"stock_count_not_in_state:{raw}"

    return None


def verify_answer(
    answer: str,
    prov: Provenance,
    *,
    tolerance_cents: int | None = None,
    fallback_text: str = DEGRADED_FALLBACK_TEXT,
) -> GroundingReport:
    lines = answer.splitlines()
    kept: list[str] = []
    report = GroundingReport(answer=answer)

    for position, line in enumerate(lines):
        violation = verify_line(line, prov, tolerance_cents=tolerance_cents) if line.strip() else None
        if violation is None:
            kept.append(line)
        else:
            report.dropped_lines.append((position, line.strip()[:80], violation))
            report.violations.append(violation)

    if kept and len(kept) != len(lines):
        report.answer = "\n".join(kept)
    if not any(line.strip() for line in kept):
        report.answer = fallback_text
        report.fallback_used = True
    return report


def verify_reason(reason: str, *, max_chars: int = 40) -> str | None:
    """O9: a model-authored reason may describe fit, never report a business number."""
    cleaned = (reason or "").strip()
    if not cleaned:
        return None
    if _REASON_NUMBER_RE.search(cleaned):
        return None
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1] + "…"
    return cleaned


def _within_tolerance(cents: int, allowed: set[int], tolerance: int) -> bool:
    return any(abs(cents - candidate) <= tolerance for candidate in allowed)


def render_price_set(goods: list[GoodsOut]) -> set[str]:
    """Helper for tests: the exact price strings a grounded answer may contain."""
    return {format_price_cents(item.price_cents, item.currency).lstrip("¥$€") for item in goods}
