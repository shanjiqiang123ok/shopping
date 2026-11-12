"""Answer rendering (docs/SHOPPING_RULES.md §5).

The template owns every number; the model may only supply the `reason` clause. That single rule is
what makes the anti-hallucination check in `guardrails/grounding.py` mostly a formality rather than
a last-resort filter (ADR A6).

Number notation is a deliberate convention:
  * `¥1699.00`  — a fact from state, therefore *checked* by grounding;
  * `2000 元以内` — the user's own words echoed back, deliberately *not* ¥-prefixed.
"""

from __future__ import annotations

from datetime import datetime

from shopping_backend.core.errors import DEGRADED_FALLBACK_TEXT
from shopping_backend.domain.enums import Intent
from shopping_backend.domain.formatting import format_price_cents, format_stock_status
from shopping_backend.domain.schemas import CouponOut, GoodsOut, StockOut
from shopping_backend.graph.state import ShoppingAgentState, state_coupons, state_goods, state_stock
from shopping_backend.guardrails.grounding import verify_reason
from shopping_backend.services import GENERAL_KEY

MAX_DISPLAY_ITEMS = 6  # O1
CATEGORY_DISPLAY = {
    "phone": "手机",
    "earphone": "耳机",
    "tablet": "平板",
    "laptop": "笔记本",
    "accessory": "配件",
}
CLOSING_HINTS = {
    Intent.GOODS_QUERY.value: "想对比其中两款，或换个价位再看，都可以继续问我。",
    Intent.COMPOSITE.value: "想只看有货的、或者换个预算再筛一次，告诉我就行。",
    Intent.STOCK_QUERY.value: "需要我顺便看看它现在能用什么券吗？",
    Intent.COUPON_QUERY.value: "告诉我具体商品，我可以确认这张券能不能用在你看中的款上。",
    Intent.OTHER.value: "我可以帮你查商品、库存和优惠券，你想看哪一类？",
}


def _spaced(condition: str) -> str:
    """`款 2000 元以内…` needs a separator before a digit, `款耳机` does not."""
    if not condition:
        return ""
    return f" {condition}" if condition[0].isdigit() else condition


def condition_text(slots: dict) -> str:
    """Restate the user's own filter, using 元 (never ¥) — see the module docstring."""
    min_cents = slots.get("min_price_cents")
    max_cents = slots.get("max_price_cents")
    category = CATEGORY_DISPLAY.get(str(slots.get("category") or ""), "")
    price_part = ""
    if min_cents and max_cents:
        price_part = f"{_yuan(min_cents)}-{_yuan(max_cents)} 元"
    elif max_cents:
        price_part = f"{_yuan(max_cents)} 元以内"
    elif min_cents:
        price_part = f"{_yuan(min_cents)} 元以上"
    if price_part and category:
        return f"{price_part}的{category}"
    return price_part or category


def _yuan(cents: object) -> str:
    value = float(cents or 0) / 100
    return f"{value:g}"


def _goods_line(index: int, goods: GoodsOut, stock: StockOut | None, reason: str | None) -> str:
    head = (
        f"{index}) {goods.title} {format_price_cents(goods.price_cents, goods.currency)}"
        f"｜{format_stock_status(stock)}｜{goods.brand or '其他品牌'}"
    )
    return f"{head}\n   理由：{reason}" if reason else head


def _coupon_line(coupon: CouponOut) -> str:
    expiry = f"，{coupon.valid_to:%Y-%m-%d} 前有效" if isinstance(coupon.valid_to, datetime) else ""
    return f"可用券：{coupon.display}（券码 {coupon.code}{expiry}）"


def render(state: ShoppingAgentState, *, reasons: dict[str, str] | None = None) -> str:
    goods = state_goods(state)
    stock = state_stock(state)
    coupons_by_goods = state_coupons(state)
    reasons = reasons or {}
    slots = dict(state.slots or {})
    condition = condition_text(slots)

    if state.error_msg and not goods and not coupons_by_goods:
        # F7/O8: the graph always reaches summarize, so the apology is rendered here, not raised
        return state.error_msg or DEGRADED_FALLBACK_TEXT

    if state.intent == Intent.OTHER.value:
        if (state.slots or {}).get("refuse"):
            return "这里只能帮你查商品、库存和优惠券，不能替你下单或付款。要看这款的现货情况和能用的券吗？"
        return CLOSING_HINTS[Intent.OTHER.value]

    if not goods and state.intent == Intent.STOCK_QUERY.value:
        return state.error_msg or "没找到这款商品，可以说一下完整型号或换个关键词吗？"

    if not goods and state.intent == Intent.COUPON_QUERY.value:
        general = coupons_by_goods.get(GENERAL_KEY) or []
        if not general:
            return "当前没有可参与的满减或折扣活动。" + CLOSING_HINTS[Intent.COUPON_QUERY.value]
        lines = ["当前可参与的优惠："] + [_coupon_line(coupon) for coupon in general[:8]]
        lines.append(CLOSING_HINTS[Intent.COUPON_QUERY.value])
        return "\n".join(lines)

    if not goods:
        return f"没找到符合「{(state.query or '').strip()[:40]}」的在售商品，可以换个品类或放宽预算再问我。"

    any_available = any((stock.get(item.id) is not None and stock[item.id].available > 0) for item in goods)

    if state.resolve_only:
        item = goods[0]
        row = stock.get(item.id)
        lines = [f"{item.title} {format_price_cents(item.price_cents, item.currency)}：{format_stock_status(row)}"]
        if row and row.warehouses:
            detail = "、".join(f"{w.warehouse} 可售{w.available}件" for w in row.warehouses[:4])
            lines.append(f"分仓情况：{detail}")
        codes = coupons_by_goods.get(item.id) or (coupons_by_goods.get(GENERAL_KEY) or [])
        if codes:
            lines.append(_coupon_line(codes[0]))
        lines.append(CLOSING_HINTS[Intent.STOCK_QUERY.value])
        return "\n".join(lines)

    shown = goods[:MAX_DISPLAY_ITEMS]
    label = _spaced(condition)
    if condition:
        lead = f"为你找到 {len(goods)} 款{label}在售商品："
        sold_out_lead = f"这 {len(goods)} 款{label}商品当前均无现货，可先领可用券，补货后再下单："
    else:
        lead = f"为你找到 {len(goods)} 款在售商品："
        sold_out_lead = f"这 {len(goods)} 款商品当前均无现货，可先领可用券，补货后再下单："
    header = lead if any_available else sold_out_lead

    body = [
        _goods_line(
            index,
            item,
            stock.get(item.id),
            verify_reason(reasons.get(item.id, "")),  # O9: numbers in a model reason kill the clause
        )
        for index, item in enumerate(shown, start=1)
    ]
    if len(goods) > MAX_DISPLAY_ITEMS:
        body.append(f"（另有 {len(goods) - MAX_DISPLAY_ITEMS} 款可选）")

    # Group identical promotions onto one line instead of repeating "可用券：..." per product:
    # the numbers still come straight from state, so grounding is unchanged, and the answer reads
    # like the §5.1 template (one 优惠信息 block).
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for item in shown:
        for coupon in (coupons_by_goods.get(item.id) or [])[:1]:
            label = _coupon_line(coupon)
            if label not in grouped:
                grouped[label] = []
                order.append(label)
            grouped[label].append(item.title)
    if not order and (coupons_by_goods.get(GENERAL_KEY) or []):
        order = [_coupon_line(coupon) for coupon in coupons_by_goods[GENERAL_KEY][:3]]
        grouped.update({label: [] for label in order})

    coupon_lines = [
        f"{label}｜适用：{'、'.join(titles)}" if titles else label for label in order for titles in [grouped[label]]
    ]

    parts = [header, *body]
    if coupon_lines:
        parts.append("\n".join(coupon_lines))
    parts.append(CLOSING_HINTS.get(state.intent, CLOSING_HINTS[Intent.GOODS_QUERY.value]))
    return "\n".join(parts)
