"""Contracts and session context."""

from shopping_backend.domain.context import ShoppingSessionContext, ensure_utc, utcnow
from shopping_backend.domain.enums import (
    CouponScope,
    CouponStatus,
    DiscountType,
    ErrorCode,
    GoodsStatus,
    Intent,
    PreferenceCategory,
    UnavailableReason,
)

__all__ = [
    "CouponScope",
    "CouponStatus",
    "DiscountType",
    "ErrorCode",
    "GoodsStatus",
    "Intent",
    "PreferenceCategory",
    "ShoppingSessionContext",
    "UnavailableReason",
    "ensure_utc",
    "utcnow",
]
