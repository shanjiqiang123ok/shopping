"""Pydantic contracts.

Two families, deliberately separated:
  * ORM models (`models_orm.py`) describe storage;
  * these DTOs describe what may cross a boundary, and are the whitelist the post-call guardrail
    validates against. Java-facing field renames happen in `backends/http_java.py`, never here.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shopping_backend.domain.enums import (
    CouponScope,
    CouponStatus,
    DiscountType,
    GoodsStatus,
    Intent,
    PreferenceCategory,
    UnavailableReason,
)

ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SHORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,36}$")
_SCRIPT_RE = re.compile(r"<\s*script", re.IGNORECASE)


def _check_identifier(value: str, *, max_len: int = 64) -> str:
    if not ID_PATTERN.match(value):
        msg = f"must match [A-Za-z0-9_-] and be at most {max_len} characters"
        raise ValueError(msg)
    return value


def parse_tags(value: Any) -> list[str]:
    """Accept a JSON string (storage form), a list, or nothing."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        value = loaded
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


# --------------------------------------------------------------------------- output contracts


class GoodsOut(BaseModel):
    """One sellable product. `price_cents` integer-only is the anti-hallucination anchor for L7."""

    model_config = ConfigDict(extra="ignore", from_attributes=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=36)
    sku: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(default="", max_length=32)
    brand: str = Field(default="", max_length=32)
    description: str = Field(default="")
    price_cents: int = Field(ge=0)
    currency: str = Field(default="CNY", max_length=3)
    tags: list[str] = Field(default_factory=list)
    status: GoodsStatus = GoodsStatus.ON_SALE

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not SHORT_ID_PATTERN.match(value):
            msg = "goods id must match [A-Za-z0-9_-]{1,36}"
            raise ValueError(msg)
        return value

    @field_validator("price_cents", mode="before")
    @classmethod
    def _integer_cents_only(cls, value: Any) -> Any:
        # R-G/§3: a float means someone mixed yuan and cents upstream; refuse instead of guessing.
        if isinstance(value, float):
            msg = "price must be integer cents, got float"
            raise ValueError(msg)
        if isinstance(value, str) and value.strip():
            if not re.fullmatch(r"-?\d+", value.strip()):
                msg = "price_cents must be an integer string"
                raise ValueError(msg)
            return int(value.strip())
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_from_json_string(cls, value: Any) -> list[str]:
        return parse_tags(value)


class WarehouseStock(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    warehouse: str = Field(default="WH-MAIN", max_length=32)
    on_hand: int = Field(default=0, ge=0)
    reserved: int = Field(default=0, ge=0)
    available: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _clamp(self) -> WarehouseStock:
        self.available = max(self.on_hand - self.reserved, 0)  # R-S1
        return self


class StockOut(BaseModel):
    """Aggregated availability. Never trust a backend-supplied `available` (R-S1)."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    goods_id: str = Field(min_length=1, max_length=36)
    on_hand: int = Field(default=0, ge=0)
    reserved: int = Field(default=0, ge=0)
    available: int = Field(default=0, ge=0)
    warehouses: list[WarehouseStock] = Field(default_factory=list)

    @field_validator("goods_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not SHORT_ID_PATTERN.match(value):
            msg = "goods_id must match [A-Za-z0-9_-]{1,36}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _recompute(self) -> StockOut:
        if self.warehouses:
            self.on_hand = sum(w.on_hand for w in self.warehouses)
            self.reserved = sum(w.reserved for w in self.warehouses)
            self.available = sum(max(w.on_hand - w.reserved, 0) for w in self.warehouses)
        else:
            self.available = max(self.on_hand - self.reserved, 0)
        return self


class CouponOut(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: str = Field(min_length=1, max_length=36)
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(default="", max_length=64)
    scope: CouponScope = CouponScope.ALL
    scope_value: list[str] = Field(default_factory=list)
    threshold_cents: int = Field(default=0, ge=0)
    discount_type: DiscountType = DiscountType.AMOUNT
    discount_value: int = Field(default=0, ge=0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    total_quota: int = Field(default=-1)
    remaining_quota: int | None = None
    per_user_limit: int | None = None
    status: CouponStatus = CouponStatus.ACTIVE
    display: str = Field(default="", max_length=48)

    @field_validator("scope_value", mode="before")
    @classmethod
    def _scope_csv(cls, value: Any) -> list[str]:
        return parse_csv(value)

    @field_validator("discount_value")
    @classmethod
    def _sane_discount(cls, value: int, info) -> int:
        dtype = info.data.get("discount_type")
        if dtype == DiscountType.PERCENT and not 1 <= value <= 999:
            msg = "percent discount_value must be a permille between 1 and 999"
            raise ValueError(msg)
        if dtype == DiscountType.AMOUNT and value <= 0:
            msg = "amount discount_value must be positive"
            raise ValueError(msg)
        return value


class UnavailableCoupon(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(max_length=32)
    reason: UnavailableReason


# --------------------------------------------------------------------------- input contracts


class GoodsQuery(BaseModel):
    """`extra="forbid"` is L3: the model cannot smuggle identity or price overrides in here."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(default="", max_length=200)
    category: str | None = Field(default=None, max_length=32)
    min_price_cents: int | None = Field(default=None, ge=0)
    max_price_cents: int | None = Field(default=None, ge=0)
    limit: int = Field(default=8, ge=1, le=20)
    include_off_sale: bool = Field(default=False)

    @model_validator(mode="after")
    def _normalise(self) -> GoodsQuery:
        if None not in (self.min_price_cents, self.max_price_cents) and self.min_price_cents > self.max_price_cents:
            self.min_price_cents, self.max_price_cents = self.max_price_cents, self.min_price_cents
        return self


class StockQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goods_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("goods_ids")
    @classmethod
    def _valid_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not SHORT_ID_PATTERN.match(item):
                msg = "each goods id must match [A-Za-z0-9_-]{1,36}"
                raise ValueError(msg)
        return list(dict.fromkeys(value))


class CouponQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goods_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("goods_ids")
    @classmethod
    def _valid_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not SHORT_ID_PATTERN.match(item):
                msg = "each goods id must match [A-Za-z0-9_-]{1,36}"
                raise ValueError(msg)
        return list(dict.fromkeys(value))


class PreferenceFact(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=200)
    category: PreferenceCategory = PreferenceCategory.PREFERENCE

    @field_validator("key")
    @classmethod
    def _snake_case_key(cls, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        if not cleaned:
            msg = "preference key is empty after normalisation"
            raise ValueError(msg)
        return cleaned[:64]


# --------------------------------------------------------------------------- business API


class GoodsSearchOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[GoodsOut] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)
    query: dict[str, Any] = Field(default_factory=dict)


class StockQueryOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stock: dict[str, StockOut] = Field(default_factory=dict)
    as_of: datetime


class CouponQueryOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coupons: dict[str, list[CouponOut]] = Field(default_factory=dict)
    unavailable: list[UnavailableCoupon] = Field(default_factory=list)


class SeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset: bool = False


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    detail: str
    error_code: str
    request_id: str = ""
    errors: list[dict[str, Any]] | None = None


# --------------------------------------------------------------------------- agent API


class AgentRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(default="default", max_length=64)
    query: str = Field(min_length=1, max_length=2000)
    preference_memory: dict[str, str] = Field(default_factory=dict)

    @field_validator("user_id", "session_id", "tenant_id")
    @classmethod
    def _identifiers(cls, value: str) -> str:
        return _check_identifier(value)

    @field_validator("query")
    @classmethod
    def _no_script_or_null(cls, value: str) -> str:
        if "\x00" in value:
            msg = "query contains a null byte"
            raise ValueError(msg)
        if _SCRIPT_RE.search(value):
            msg = "query contains a script tag"
            raise ValueError(msg)
        if not value.strip():
            msg = "query must not be blank"
            raise ValueError(msg)
        return value

    @field_validator("preference_memory", mode="before")
    @classmethod
    def _cap_preferences(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, item in list(value.items())[:32]:
            key_s = str(key).strip()[:64]
            val_s = str(item).strip()[:200]
            if key_s and val_s:
                cleaned[key_s] = val_s
        return cleaned


class GoodsView(BaseModel):
    """Flattened per-goods view for the response: goods + availability + its best coupon codes."""

    model_config = ConfigDict(extra="ignore")

    id: str
    sku: str = ""
    title: str
    price_cents: int
    currency: str = "CNY"
    category: str = ""
    brand: str = ""
    available: int = 0
    coupon_codes: list[str] = Field(default_factory=list)
    reason: str = ""


class AgentRunData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str = Intent.OTHER.value
    goods: list[GoodsView] = Field(default_factory=list)
    stock: dict[str, StockOut] = Field(default_factory=dict)
    coupons: dict[str, list[CouponOut]] = Field(default_factory=dict)
    out_of_stock_ids: list[str] = Field(default_factory=list)


class AgentRunMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = "rule_based"
    backend: str = "inprocess"
    route_trace: list[str] = Field(default_factory=list)
    seen_goods_ids: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    degraded: bool = False
    error_msg: str | None = None
    latency_ms: int = 0
    thread_id: str = ""
    preference_summary: str | None = None


class AgentRunOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str = ""
    trace_id: str = ""
    answer: str
    data: AgentRunData = Field(default_factory=AgentRunData)
    meta: AgentRunMeta = Field(default_factory=AgentRunMeta)


class SupervisorDecision(BaseModel):
    """The only thing the LLM is allowed to decide (F8). Anything else is code or backend data."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent = Intent.OTHER
    goods_ref: str | None = Field(default=None, max_length=40)
    category: str | None = Field(default=None, max_length=32)
    min_price_cents: int | None = Field(default=None, ge=0)
    max_price_cents: int | None = Field(default=None, ge=0)
    coupon_needed: bool = False
    resolve_only: bool = False
    rationale: str = Field(default="", max_length=200)


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facts: list[PreferenceFact] = Field(default_factory=list, max_length=8)
