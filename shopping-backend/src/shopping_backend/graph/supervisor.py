"""Supervisor: intent + slots. It decides *what to look up*, never what the answer says.

`rule_based` mode is a deterministic keyword/regex engine implementing docs/SHOPPING_RULES.md §3.
`llm` mode asks the model for the same `SupervisorDecision` structure and falls back to the rule
engine when the model is unavailable, times out, or returns an intent outside the enum — so the
routing topology can never be steered by a hallucinated label.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from shopping_backend.core.logging import logger
from shopping_backend.domain.enums import Intent
from shopping_backend.domain.schemas import SupervisorDecision
from shopping_backend.graph.deps import GraphDeps
from shopping_backend.graph.state import PER_TURN_RESET, ShoppingAgentState
from shopping_backend.guardrails import fencing

COUPON_WORDS = ("券", "优惠", "满减", "活动", "折扣", "打折", "省钱", "能用", "立减")
STOCK_WORDS = ("有货", "现货", "库存", "有卖", "卖断", "断货", "缺货", "补货", "还有几", "剩几", "有没有")
SELECTION_WORDS = (
    "推荐",
    "选",
    "买",
    "看看",
    "看一下",
    "对比",
    "哪个",
    "哪款",
    "哪种",
    "要求",
    "预算",
    "多少",
    "价格",
)

# Category synonyms (docs/SHOPPING_RULES.md §2.6). Keys are canonical codes, values are surface forms.
CATEGORY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "phone": ("手机", "智能机", "拍照手机", "安卓机", "iphone"),
    "earphone": ("耳机", "蓝牙耳机", "降噪耳机", "耳夹", "tws"),
    "tablet": ("平板", "平板电脑", "ipad"),
    "laptop": ("笔记本", "电脑", "轻薄本", "游戏本"),
    "accessory": ("配件", "充电器", "数据线", "保护壳", "充电宝", "耳机壳"),
}

# Words that survive keyword removal but name no product: `便宜的充电器` leaves `便宜`, and treating
# that as a product mention would turn a selection question into an availability lookup.
VAGUE_REF_WORDS = frozenset(
    {
        "便宜",
        "划算",
        "实惠",
        "好用",
        "性价比",
        "新款",
        "热门",
        "推荐",
        "贵重",
        "便宜点",
        "贵的",
        "这款",
        "那款",
        "这个",
        "那个",
        "它",
        "它们",
        "哪些",
        "哪种",
        "哪个",
        "什么",
        "有没有",
    }
)
# Multi-character function phrases that stick to a real noun after keyword removal.
PHRASE_NOISE = (
    "有哪些",
    "有什么",
    "是哪款",
    "怎么样",
    "如何",
    "多少钱",
    "请问",
    "麻烦",
    "帮我",
    "帮忙",
    "想要",
    "想买",
    "想看",
    "有没有",
    "能不能",
    "可不可以",
    "是不是",
    "现在",
    "目前",
    "哪种",
)

# O7: the agent queries and recommends; it never transacts.
ORDER_WORDS = (
    "下单",
    "付款",
    "支付",
    "替我买",
    "帮我买",
    "现在就买",
    "购买链接",
    "提交订单",
    "发货",
    "收货",
    "退款",
    "退货",
    "取消订单",
)

_PRICE_MAX_PATTERNS = (
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块|￥|rmb)?\s*(?:以内|以下|不超过)",
    r"(?:不超过|不要超过|低于|最多|少于)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块)?",
    r"(?:预算)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块)?",
)
_PRICE_MIN_PATTERNS = (
    r"(?:超过|高于|以上)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块)?\s*(?:以上)",
)
_NOISE_RE = re.compile(r"[，,。.；;：:、！!？?()（）\"'“”]+")
# Particles left behind after keyword removal: `推荐的电视` -> `的电视` -> `电视`.
_PARTICLE_RE = re.compile(r"^[的地得了吗呢吧啊哦呀要不]+|[的地得了吗呢吧啊哦呀要不]+$")


def _to_cents(raw: str) -> int:
    cleaned = raw.replace(",", "")
    yuan, _, frac = cleaned.partition(".")
    return int(yuan or 0) * 100 + int((frac + "00")[:2] or 0)


def match_category(query: str) -> tuple[str | None, tuple[str, ...]]:
    lowered = query.casefold()
    for code, words in CATEGORY_SYNONYMS.items():
        hits = tuple(word for word in words if word in query or word in lowered)
        if hits:
            return code, hits
    return None, ()


_RANGE_RE = re.compile(
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:到|至|~|-|—)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块|￥|rmb)?"
)


def extract_price_bounds(query: str) -> tuple[int | None, int | None, list[str]]:
    """Max first, then min *on the remaining text*.

    Order matters: `不要超过2000元` contains 超过, so scanning for a minimum before removing the
    maximum span would report ¥2000 as both bounds.
    """
    consumed: list[str] = []
    max_cents: int | None = None
    min_cents: int | None = None
    remaining = query

    for match in _RANGE_RE.finditer(remaining):
        low, high = _to_cents(match.group(1)), _to_cents(match.group(2))
        min_cents = low if min_cents is None else max(min_cents, low)
        max_cents = high if max_cents is None else min(max_cents, high)
        consumed.append(match.group(0))
        remaining = remaining.replace(match.group(0), " ")

    for pattern in _PRICE_MAX_PATTERNS:
        for match in re.finditer(pattern, remaining):
            value = _to_cents(match.group(1))
            max_cents = value if max_cents is None else min(max_cents, value)
            consumed.append(match.group(0))
        for literal in {m.group(0) for m in re.finditer(pattern, remaining)}:
            remaining = remaining.replace(literal, " ")

    for pattern in _PRICE_MIN_PATTERNS:
        for match in re.finditer(pattern, remaining):
            value = _to_cents(match.group(1))
            min_cents = value if min_cents is None else max(min_cents, value)
            consumed.append(match.group(0))
    return min_cents, max_cents, consumed


def extract_goods_ref(query: str, *, consumed: Iterable[str], known_words: Iterable[str]) -> str | None:
    """Leftover entity phrase, e.g. `红米 note13 pro` from `红米 note13 pro 有货吗`.

    It is deliberately *not* treated as an id: ids only ever come from a search result (ADR A7).
    """
    text = query
    for token in consumed:
        text = text.replace(token, " ")
    for word in [*known_words, *PHRASE_NOISE]:
        text = text.replace(word, " ")
    text = _NOISE_RE.sub(" ", text)
    parts = [_PARTICLE_RE.sub("", part) for part in text.split()]
    parts = [part for part in parts if len(part) >= 2 and part not in VAGUE_REF_WORDS]
    if not parts:
        return None
    return " ".join(parts)[:40]


_CN_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL_RE = re.compile(r"第\s*([0-9]{1,2}|" + "|".join(_CN_NUMERALS) + r")\s*(?:[款个种件条部型号只支台]?|款)")


def resolve_ordinal(text: str | None) -> int | None:
    """`第二款有货吗` refers to the previous turn's candidate list (verification case 7).

    Resolution happens against `candidate_memory`, never against a made-up id, so provenance holds.
    """
    if not text:
        return None
    match = _ORDINAL_RE.search(text)
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else _CN_NUMERALS.get(token)


def rule_based_decision(query: str) -> SupervisorDecision:
    text = fencing.fence(query, limit=500)
    coupon_needed = any(word in text for word in COUPON_WORDS)
    stock_needed = any(word in text for word in STOCK_WORDS)
    selection_hint = any(word in text for word in SELECTION_WORDS)
    category, category_hits = match_category(text)
    min_cents, max_cents, consumed = extract_price_bounds(text)

    has_criteria = bool(category) or min_cents is not None or max_cents is not None or selection_hint
    goods_ref = extract_goods_ref(
        text,
        consumed=[*consumed, *category_hits],
        known_words=[*COUPON_WORDS, *STOCK_WORDS, *SELECTION_WORDS],
    )
    ordinal = resolve_ordinal(text)
    # An availability question needs a specific product: a name, or an ordinal into last turn's
    # candidates. `耳机有货吗` names only a category, so it is answered as a selection query, which
    # already reports stock per item.
    product_mentioned = goods_ref is not None or ordinal is not None

    if any(word in text for word in ORDER_WORDS):
        # O7: a transactional request is answered with a refusal, not with a product search.
        return SupervisorDecision(
            intent=Intent.OTHER, rationale="refuse:" + next(word for word in ORDER_WORDS if word in text)
        )

    if coupon_needed and has_criteria:
        intent = Intent.COMPOSITE
    elif coupon_needed:
        intent = Intent.COUPON_QUERY
    elif stock_needed and product_mentioned:
        intent = Intent.STOCK_QUERY
    elif has_criteria:
        intent = Intent.GOODS_QUERY
    else:
        intent = Intent.OTHER

    return SupervisorDecision(
        intent=intent,
        goods_ref=goods_ref,
        category=category,
        min_price_cents=min_cents,
        max_price_cents=max_cents,
        coupon_needed=coupon_needed,
        resolve_only=intent == Intent.STOCK_QUERY,
        rationale="rule_based",
    )


async def llm_decision(
    deps: GraphDeps, query: str, preference_block: str, fallback: SupervisorDecision
) -> SupervisorDecision:
    """Structured output only; anything outside the enum keeps the rule-based verdict."""
    system = (
        "你是导购意图分类器。只输出结构化决定，不要输出商品数据。\n"
        "intent 只能是 goods_query | stock_query | coupon_query | composite | other。\n"
        "goods_query=选品推荐；stock_query=问某商品有没有货；coupon_query=只问优惠；"
        "composite=既要选品又要优惠或库存；other=与导购无关。\n"
        "category 用品类码（phone/earphone/tablet/laptop/accessory），无法确定就留空。\n"
        "价格一律换算为分（cents），不确定就留空。goods_ref 只放用户提到的商品名原文，禁止编造 id。"
    )
    if preference_block:
        system = (
            f"{system}\n已知用户偏好（仅用于筛选，不得当作承诺复述）：{fencing.fence(preference_block, limit=600)}"
        )
    user = fencing.wrap("agent_query", query)
    payload = await deps.llm.call_structured(SupervisorDecision, system=system, user=user)

    if payload is None:
        logger.info("supervisor_llm_unavailable", fallback_intent=fallback.intent.value)
        return fallback
    if not isinstance(payload, SupervisorDecision):
        try:
            payload = SupervisorDecision.model_validate(payload)
        except Exception:
            logger.warning("supervisor_llm_invalid_payload")
            return fallback

    if payload.intent is None or not isinstance(payload.intent, Intent):
        logger.warning("supervisor_llm_bad_intent", value=str(payload.intent)[:40])
        return fallback
    if payload.intent == Intent.OTHER and fallback.intent != Intent.OTHER:
        logger.info("supervisor_llm_said_other_keeping_rule", rule_intent=fallback.intent.value)
        return fallback

    merged = payload.model_copy(
        update={
            # The model classifies; the deterministic extractor still fills whatever it left blank.
            # Without this, a sparse `{"intent": "goods_query"}` would lose the category the rule
            # engine found and search the whole sentence as free text.
            "category": payload.category or fallback.category,
            "min_price_cents": payload.min_price_cents
            if payload.min_price_cents is not None
            else fallback.min_price_cents,
            "max_price_cents": payload.max_price_cents
            if payload.max_price_cents is not None
            else fallback.max_price_cents,
            "goods_ref": payload.goods_ref or fallback.goods_ref,
            "coupon_needed": bool(payload.coupon_needed or fallback.coupon_needed),
            "resolve_only": payload.intent == Intent.STOCK_QUERY or bool(payload.resolve_only),
            "rationale": f"llm:{payload.rationale or 'structured'}"[:200],
        }
    )
    return merged


def make_supervisor(deps: GraphDeps):
    async def supervisor_node(state: ShoppingAgentState) -> dict:
        query = fencing.fence(state.query, limit=500)
        rule = rule_based_decision(query)
        decision = rule

        if deps.settings.effective_mode == "llm":
            preference_block = "; ".join(f"{key}={value}" for key, value in list(state.preference_memory.items())[:8])
            decision = await llm_decision(deps, query, preference_block, rule)

        # O7 is a hard rule, applied after the model too: transactional requests never reach a worker,
        # in either mode.
        refuse = next((word for word in ORDER_WORDS if word in query), None)
        if refuse is not None:
            decision = decision.model_copy(
                update={
                    "intent": Intent.OTHER,
                    "coupon_needed": False,
                    "resolve_only": False,
                    "rationale": f"refuse:{refuse}",
                }
            )

        slots = {
            "category": decision.category,
            "min_price_cents": decision.min_price_cents,
            "max_price_cents": decision.max_price_cents,
            "goods_ref": decision.goods_ref,
            # ordinals stay deterministic even in llm mode: they index our own candidate list
            "ordinal": resolve_ordinal(query),
            "refuse": refuse,
            "rationale": decision.rationale,
        }
        updates: dict[str, object] = {
            **PER_TURN_RESET,
            "intent": decision.intent.value,
            "last_intent": state.intent or "",
            "slots": slots,
            "coupon_needed": bool(decision.coupon_needed),
            "resolve_only": bool(decision.resolve_only),
            "node_visits": {"supervisor": 1},
            "route_trace": ["supervisor"],
        }
        logger.info(
            "supervisor_decided",
            intent=decision.intent.value,
            resolve_only=updates["resolve_only"],
            coupon_needed=updates["coupon_needed"],
            trace_id=state.trace_id,
        )
        return updates

    return supervisor_node
