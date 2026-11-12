"""Shared display formatting (single source of truth for the numbers users see).

Both `graph/nodes/presentation.py` (rendering) and `guardrails/grounding.py` (verification) build
their tokens from here, so the two can never disagree about what "¥1699.00" means.
"""

from __future__ import annotations

from shopping_backend.domain.enums import DiscountType
from shopping_backend.domain.schemas import CouponOut, StockOut

CURRENCY_SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€"}


def format_price_cents(cents: int, currency: str = "CNY") -> str:
    symbol = CURRENCY_SYMBOL.get(currency, currency + " ")
    yuan = cents / 100
    return f"{symbol}{yuan:.2f}"


def format_percent(discount_value: int) -> str:
    """permille -> Chinese discount wording: 900 -> 9折, 850 -> 8.5折."""
    zhe = discount_value / 100
    text = f"{zhe:g}"
    return f"{text}折"


def format_coupon_display(coupon: CouponOut) -> str:
    """R-C2 / O4: threshold and strength always appear together."""
    threshold = format_price_cents(coupon.threshold_cents) if coupon.threshold_cents > 0 else ""
    if coupon.discount_type == DiscountType.SHIPPING:
        return f"满{threshold}免邮" if threshold else "免邮"
    if coupon.discount_type == DiscountType.PERCENT:
        strength = format_percent(coupon.discount_value)
        return f"满{threshold}享{strength}" if threshold else strength
    strength = format_price_cents(coupon.discount_value)
    return f"满{threshold}减{strength}" if threshold else f"立减{strength}"


def format_stock_status(stock: StockOut | None) -> str:
    """O3: exactly four allowed phrasings."""
    if stock is None:
        return "无现货"
    available = stock.available
    if available <= 0:
        return "无现货"
    if available <= 3:
        return f"仅{available}件（紧张）"
    return f"现货{available}件"


def parse_price_token(token: str) -> int:
    """'1699.00' -> 169900 cents. Used by grounding to compare rendered prices with state."""
    cleaned = token.strip().replace(",", "")
    if "." in cleaned:
        yuan, _, frac = cleaned.partition(".")
        frac = (frac + "00")[:2]
        return int(yuan or 0) * 100 + int(frac or 0)
    return int(cleaned) * 100
