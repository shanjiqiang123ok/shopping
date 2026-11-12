"""Guardrails: the only place cross-boundary data is validated (see docs/GUARDRAILS.md)."""

from shopping_backend.guardrails import fencing, grounding, validate_input, validate_output
from shopping_backend.guardrails.grounding import GroundingReport, Provenance, verify_answer, verify_reason
from shopping_backend.guardrails.validate_input import (
    describe_violation,
    parse_agent_run,
    parse_coupon_query,
    parse_goods_query,
    parse_stock_query,
    try_goods_query,
)
from shopping_backend.guardrails.validate_output import (
    RowsResult,
    cross_check,
    validate_coupons,
    validate_goods,
    validate_rows,
    validate_stock,
)

__all__ = [
    "GroundingReport",
    "Provenance",
    "RowsResult",
    "cross_check",
    "describe_violation",
    "fencing",
    "grounding",
    "parse_agent_run",
    "parse_coupon_query",
    "parse_goods_query",
    "parse_stock_query",
    "try_goods_query",
    "validate_coupons",
    "validate_goods",
    "validate_output",
    "validate_rows",
    "validate_stock",
    "verify_answer",
    "verify_reason",
]
