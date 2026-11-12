"""ORM tables.

Portability rules enforced here (asserted by tests/test_db_engine.py, see
docs/EXTENSION_DB_DOCKER.md §2): String(36) UUID primary keys instead of autoincrement, integer
cents for money, Text instead of a dialect JSON type, naive UTC datetimes, short indexed columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from shopping_backend.domain.context import utcnow


class Goods(SQLModel, table=True):
    __tablename__ = "goods"

    id: str = Field(primary_key=True, max_length=36)
    sku: str = Field(max_length=64, unique=True, index=True)
    title: str = Field(max_length=120)
    category: str = Field(max_length=32, index=True)
    brand: str = Field(default="", max_length=32)
    description: str = Field(default="", sa_column=Column(Text))
    price_cents: int = Field(ge=0)
    currency: str = Field(default="CNY", max_length=3)
    tags: str = Field(default="[]", sa_column=Column(Text))  # JSON array, parsed in the service layer
    status: str = Field(default="on_sale", max_length=16, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StockRow(SQLModel, table=True):
    """`available` is deliberately not a column: R-S1 makes it a computed value."""

    __tablename__ = "stock"
    __table_args__ = (UniqueConstraint("goods_id", "warehouse", name="uq_stock_goods_id_warehouse"),)

    id: str = Field(primary_key=True, max_length=36)
    goods_id: str = Field(max_length=36, foreign_key="goods.id", index=True)
    warehouse: str = Field(default="WH-MAIN", max_length=32)
    on_hand: int = Field(default=0, ge=0)
    reserved: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utcnow)


class Coupon(SQLModel, table=True):
    __tablename__ = "coupon"

    id: str = Field(primary_key=True, max_length=36)
    code: str = Field(max_length=32, unique=True, index=True)
    name: str = Field(max_length=64)
    scope: str = Field(max_length=16)
    scope_value: str = Field(default="", sa_column=Column(Text))  # CSV of categories or goods ids
    threshold_cents: int = Field(default=0, ge=0)
    discount_type: str = Field(max_length=16)
    discount_value: int = Field(default=0, ge=0)
    valid_from: datetime = Field(default_factory=utcnow)
    valid_to: datetime = Field(default_factory=utcnow)
    total_quota: int = Field(default=-1)  # -1 = unlimited
    used_count: int = Field(default=0, ge=0)
    per_user_limit: int = Field(default=1, ge=0)
    status: str = Field(default="active", max_length=16, index=True)


class UserPreference(SQLModel, table=True):
    __tablename__ = "user_preference"

    user_id: str = Field(max_length=64, primary_key=True)
    pref_key: str = Field(max_length=64, primary_key=True)  # named to dodge the SQL keyword `key`
    pref_value: str = Field(max_length=200)
    category: str = Field(default="preference", max_length=16)
    source_session_id: str = Field(default="", max_length=64)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class AgentRequestLog(SQLModel, table=True):
    """Operational trail: routing, guardrail drops and latency, so regressions are debuggable
    without a metrics stack."""

    __tablename__ = "agent_request_log"

    id: str = Field(primary_key=True, max_length=36)
    trace_id: str = Field(max_length=32, index=True)
    session_id: str = Field(max_length=64, index=True)
    user_id: str = Field(max_length=64)
    mode: str = Field(max_length=16)
    backend_driver: str = Field(max_length=16)
    intent: str = Field(max_length=24)
    route_trace: str = Field(default="[]", sa_column=Column(Text))
    goods_n: int = Field(default=0, ge=0)
    stock_n: int = Field(default=0, ge=0)
    coupon_n: int = Field(default=0, ge=0)
    dropped_rows_n: int = Field(default=0, ge=0)
    grounding_drops_n: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    degraded: bool = False
    error_code: str = Field(default="", max_length=32)
    created_at: datetime = Field(default_factory=utcnow, index=True)


TABLE_MODELS = (Goods, StockRow, Coupon, UserPreference, AgentRequestLog)
