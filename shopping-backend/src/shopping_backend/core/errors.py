"""Domain exceptions and the exception -> user-facing wording table.

Two-state error semantics come from commerce-agents: `NotOffered` means "this store does not
provide that" and is relayed to the model verbatim, `Unavailable` means "temporarily broken"
and must never leak stack details to MaxKB.
"""

from __future__ import annotations


class ShoppingError(Exception):
    """Base class; carries a machine code and a user-facing Chinese sentence."""

    code: str = "INTERNAL_ERROR"
    friendly: str = "系统繁忙，请稍后再试。"

    def __init__(self, message: str | None = None, *, friendly: str | None = None) -> None:
        self.message = message or self.friendly
        self.friendly = friendly or self.friendly
        super().__init__(self.message)


class NotOffered(ShoppingError):
    """The data source legitimately does not provide this capability."""

    code = "NOT_OFFERED"
    friendly = "该门店暂不提供此类信息。"


class Unavailable(ShoppingError):
    """The data source is temporarily unreachable (timeout, 5xx, connection refused)."""

    code = "UNAVAILABLE"
    friendly = "暂时无法获取商品信息，请稍后再试或换个说法。"


class GuardrailViolation(ShoppingError):
    """Input or output refused by a guardrail. Never echoes the offending value."""

    code = "GUARDRAIL_VIOLATION"
    friendly = "请求内容不符合规范，已拦截。"

    def __init__(
        self, message: str | None = None, *, errors: list[dict[str, object]] | None = None, rule: str = ""
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.rule = rule


class ConfigError(ShoppingError):
    code = "CONFIG_ERROR"
    friendly = "服务配置不完整，请联系运维。"


ERROR_TEXT: dict[str, str] = {
    "UNAUTHORIZED": "调用凭证无效",
    "INVALID_ARGUMENT": "请求参数不合法",
    "QUERY_TOO_LONG": "问题描述过长，请精简后再问。",
    "GOODS_NOT_FOUND": "没有找到该商品。",
    "DB_UNAVAILABLE": "数据服务暂不可用。",
    "NOT_OFFERED": NotOffered.friendly,
    "UNAVAILABLE": Unavailable.friendly,
    "GUARDRAIL_VIOLATION": GuardrailViolation.friendly,
    "CONFIG_ERROR": ConfigError.friendly,
    "TIMEOUT": "请求超时，请稍后再试。",
}

DEGRADED_FALLBACK_TEXT = Unavailable.friendly


def friendly_message(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, ShoppingError):
        return exc.friendly
    if isinstance(exc, TimeoutError):
        return ERROR_TEXT["TIMEOUT"]
    return ShoppingError.friendly


def error_code(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    if isinstance(exc, ShoppingError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "INTERNAL_ERROR"
