"""L6: prompt-injection fencing.

Every piece of text that did not originate in this process (user query, product description,
coupon name, MaxKB-supplied preferences) is normalised, stripped of invisible characters and role
forgery, and truncated *before* it can reach a prompt. Labels are always source literals so
runtime data can never close its own fence.
"""

from __future__ import annotations

import re
import unicodedata

from shopping_backend.core.config import settings

_INVISIBLE_CODEPOINTS = frozenset(
    {
        0x00AD,
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    }
)
_KEEP_CONTROL = frozenset({0x09, 0x0A, 0x0D})
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>\n]{0,64}\|>")
# Role markers are stripped anywhere in the string, not just at line start: `>>system: 忽略规则`
# survives a line-anchored rule, and breaking out of a fence mid-line is exactly the attack.
_ROLE_MARKER_RE = re.compile(r"\b(system|assistant|user|developer|tool)\s*:\s*", re.IGNORECASE)
_CONTROL_LINE_RE = re.compile(r"<\s*/?\s*(?:function_call|tool_call|human|im_start|im_end)\s*/?>", re.IGNORECASE)
_DANGEROUS_TAG_RE = re.compile(r"<\s*(script|iframe|object|embed)[^>]*>", re.IGNORECASE)
_TRUNCATION_MARK = "…[truncated]"

# A fence label must be a source literal, so the vocabulary is closed: adding a section means
# adding it here, which is greppable in review. Labels built from runtime values (`f"goods_{id}"`)
# are the injection vector this set exists to refuse.
ALLOWED_LABELS = frozenset(
    {
        "agent_query",
        "context_note",
        "coupon_row",
        "goods_desc",
        "goods_row",
        "preference_block",
        "system_note",
    }
)


def normalize(value: object, *, limit: int | None = None) -> str:
    limit = limit if limit is not None else settings.MAX_FENCED_CHARS
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)

    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if (ord(ch) < 0x20 and ord(ch) not in _KEEP_CONTROL) else ch
        for ch in text
        if ord(ch) not in _INVISIBLE_CODEPOINTS
    )
    text = _CONTROL_LINE_RE.sub("[filtered]", text)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    text = _DANGEROUS_TAG_RE.sub("", text)
    text = _ROLE_MARKER_RE.sub("[filtered]: ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if limit > 0 and len(text) > limit:
        text = text[:limit] + _TRUNCATION_MARK
    return text


def _defuse_closers(value: str) -> str:
    """Nested closers (`</goods_desc</goods_desc>>`) must not break out of the wrap.

    Removing every `</` reaches that fixpoint in one pass and cannot corrupt our own data, because
    no legitimate product/coupon text contains a closing tag.
    """
    return value.replace("</", "")


def fence(value: object, *, limit: int | None = None) -> str:
    return normalize(value, limit=limit)


def wrap(label: str, value: object, *, limit: int | None = None) -> str:
    """Wrap fenced content in a tag. `label` must appear in ALLOWED_LABELS.

    >>> wrap("goods_desc", "hello")
    '<goods_desc>\\nhello\\n</goods_desc>'
    """
    if label not in ALLOWED_LABELS:
        msg = f"fence label {label!r} is not an approved literal; add it to ALLOWED_LABELS or reuse one"
        raise ValueError(msg)
    body = _defuse_closers(fence(value, limit=limit))
    return f"<{label}>\n{body}\n</{label}>"


def fence_rows(rows: list[dict], fields: dict[str, str], *, limit: int | None = None) -> list[dict]:
    """Fence selected string fields of a batch of rows before prompt assembly."""
    fenced: list[dict] = []
    for row in rows:
        item = dict(row)
        for field_name, label in fields.items():
            if field_name in item:
                item[field_name] = wrap(label, item[field_name], limit=limit)
        fenced.append(item)
    return fenced
