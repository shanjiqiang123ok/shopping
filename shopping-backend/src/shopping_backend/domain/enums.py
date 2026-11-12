"""Business enums. Stored as VARCHAR(16) + application-side validation, never as a native
database ENUM type (PostgreSQL native enums make migrations painful).
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    GOODS_QUERY = "goods_query"
    STOCK_QUERY = "stock_query"
    COUPON_QUERY = "coupon_query"
    COMPOSITE = "composite"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: object) -> Intent | None:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                return None
        return None


class GoodsStatus(str, Enum):
    ON_SALE = "on_sale"
    OFF_SALE = "off_sale"
    DRAFT = "draft"


class CouponScope(str, Enum):
    ALL = "all"
    CATEGORY = "category"
    GOODS = "goods"


class DiscountType(str, Enum):
    PERCENT = "percent"
    AMOUNT = "amount"
    SHIPPING = "shipping"


class CouponStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"


class PreferenceCategory(str, Enum):
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    CONTEXT = "context"


class UnavailableReason(str, Enum):
    """Diagnostic-only codes returned by the coupon service; never shown to users (R-C)."""

    EXPIRED = "expired"
    PAUSED = "paused"
    QUOTA_USED_UP = "quota_used_up"
    BELOW_THRESHOLD = "below_threshold"
    SCOPE_MISMATCH = "scope_mismatch"


class ErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    QUERY_TOO_LONG = "QUERY_TOO_LONG"
    GOODS_NOT_FOUND = "GOODS_NOT_FOUND"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    NOT_OFFERED = "NOT_OFFERED"
    UNAVAILABLE = "UNAVAILABLE"
    GUARDRAIL_VIOLATION = "GUARDRAIL_VIOLATION"
    CONFIG_ERROR = "CONFIG_ERROR"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


INTENT_VALUES = [i.value for i in Intent]
