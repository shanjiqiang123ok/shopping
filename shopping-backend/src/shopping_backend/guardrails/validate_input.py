"""L2 (API input) and L3 (tool input) validation.

L2 raises so FastAPI can answer 422. L3 must never raise: an invalid model-supplied argument is
reported back to the model as text so the turn continues instead of collapsing
(commerce-agents `InvalidArguments` handling).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from shopping_backend.core.config import settings
from shopping_backend.core.errors import GuardrailViolation
from shopping_backend.domain.schemas import AgentRunIn, CouponQueryIn, GoodsQuery, StockQueryIn


def _violation(exc: ValidationError, rule: str) -> GuardrailViolation:
    errors = [
        {
            "loc": list(err.get("loc", ())),
            "type": err.get("type", "value_error"),
            "msg": str(err.get("msg", ""))[:200],
        }
        for err in exc.errors()
    ]
    return GuardrailViolation("请求参数不合法", errors=errors, rule=rule)


def parse_agent_run(payload: dict[str, Any]) -> AgentRunIn:
    """L2: raises GuardrailViolation (mapped to 422 by the API layer)."""
    try:
        return AgentRunIn.model_validate(payload)
    except ValidationError as exc:
        raise _violation(exc, "L2/AgentRunIn") from exc


def parse_goods_query(payload: dict[str, Any], *, max_limit: int | None = None) -> GoodsQuery:
    """L3 with clamping instead of rejection for numeric bounds (limit, price range)."""
    limit_cap = max_limit if max_limit is not None else settings.MAX_GOODS_LIMIT
    data = dict(payload)

    raw_limit = data.get("limit")
    if isinstance(raw_limit, int):
        data["limit"] = max(1, min(raw_limit, limit_cap))

    try:
        return GoodsQuery.model_validate(data)
    except ValidationError as exc:
        raise _violation(exc, "L3/GoodsQuery") from exc


def parse_stock_query(payload: dict[str, Any]) -> StockQueryIn:
    try:
        return StockQueryIn.model_validate(payload)
    except ValidationError as exc:
        raise _violation(exc, "L3/StockQueryIn") from exc


def parse_coupon_query(payload: dict[str, Any]) -> CouponQueryIn:
    try:
        return CouponQueryIn.model_validate(payload)
    except ValidationError as exc:
        raise _violation(exc, "L3/CouponQueryIn") from exc


def describe_violation(exc: GuardrailViolation) -> str:
    """Model-facing wording: tell it what was wrong, ask it to retry (never raises)."""
    fields = ", ".join(".".join(str(part) for part in err["loc"]) or "body" for err in exc.errors[:5])
    return f"参数不合法（{fields or exc.rule}），请调整参数后重试。"


def try_goods_query(payload: dict[str, Any], *, max_limit: int | None = None) -> tuple[GoodsQuery | None, str | None]:
    try:
        return parse_goods_query(payload, max_limit=max_limit), None
    except GuardrailViolation as exc:
        return None, describe_violation(exc)
