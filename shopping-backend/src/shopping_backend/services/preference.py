"""User preference storage, including the write filter.

Modelled on commerce-agents' memory contract: the backend owns the facts, the model may propose
them, and anything that looks like an identifier is refused *without echoing the value back*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine
from sqlmodel import select

from shopping_backend.core.config import settings
from shopping_backend.core.logging import logger
from shopping_backend.db.session import session_scope
from shopping_backend.domain.context import utcnow
from shopping_backend.domain.enums import PreferenceCategory
from shopping_backend.domain.models_orm import UserPreference
from shopping_backend.domain.schemas import PreferenceFact

BLOCKED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("long_digit_run", re.compile(r"\d[\d\s\-]{7,}\d")),  # card / account-like numbers
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    ("email", re.compile(r"\S+@\S+\.\S+")),
    ("cn_mobile", re.compile(r"\b1[3-9]\d{9}\b")),
    ("id_card", re.compile(r"\b\d{17}[\dXx]\b")),
)

MAX_FACTS_PER_TURN = 3

# Category display order for tier-one injection (constraints always win).
_CATEGORY_ORDER = {
    PreferenceCategory.CONSTRAINT.value: 0,
    PreferenceCategory.CONTEXT.value: 1,
    PreferenceCategory.PREFERENCE.value: 2,
}


@dataclass(slots=True)
class StoredResult:
    kept: list[PreferenceFact] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.rejected)


def find_blocked(value: str) -> str | None:
    for name, pattern in BLOCKED_PATTERNS:
        if pattern.search(value):
            return name
    return None


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text.casefold())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} or {cleaned}


def _similar(left: str, right: str) -> bool:
    """commerce-agents `_same_fact`: containment or Jaccard >= 0.6 on character bigrams."""
    a, b = left.casefold().strip(), right.casefold().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    set_a, set_b = _bigrams(a), _bigrams(b)
    union = set_a | set_b
    if not union:
        return False
    return len(set_a & set_b) / len(union) >= 0.6


class PreferenceService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, user_id: str) -> list[UserPreference]:
        with session_scope(self._engine) as session:
            return list(session.exec(select(UserPreference).where(UserPreference.user_id == user_id)).all())

    def tier_one(self, user_id: str, *, cap: int | None = None) -> dict[str, str]:
        """Constraints first, then most recently updated, capped (R-P2)."""
        limit = cap if cap is not None else settings.PREFERENCE_TIER1_CAP
        rows = self.load(user_id)
        rows.sort(
            key=lambda row: (
                _CATEGORY_ORDER.get(row.category, 9),
                -(row.updated_at.timestamp() if row.updated_at else 0),
            )
        )
        return {row.pref_key: row.pref_value for row in rows[:limit]}

    def merged(self, user_id: str, remote: dict[str, str] | None) -> dict[str, str]:
        """MaxKB may pass a summary back; the server-side table always wins (R-P1)."""
        local = self.tier_one(user_id)
        if not remote:
            return local
        merged = {**{str(k)[:64]: str(v)[:200] for k, v in remote.items()}, **local}
        return merged

    def as_prompt_block(self, user_id: str, remote: dict[str, str] | None = None) -> str:
        facts = self.merged(user_id, remote)
        if not facts:
            return ""
        lines = [f"- {key}: {value}" for key, value in list(facts.items())[: settings.PREFERENCE_TIER1_CAP]]
        return "已知用户偏好（仅用于筛选，不得复述为承诺）：\n" + "\n".join(lines)

    def store(self, user_id: str, facts: list[PreferenceFact], *, source_session_id: str = "") -> StoredResult:
        result = StoredResult()
        if not facts:
            return result

        candidates: list[PreferenceFact] = []
        for fact in facts[:MAX_FACTS_PER_TURN]:  # single-turn write cap
            if blocked := find_blocked(fact.value):
                # never echo the value: the rejection reason is a category name only
                result.rejected.append((fact.key, blocked))
                logger.info("preference_write_refused", user_id=user_id, reason=blocked)
                continue
            candidates.append(fact)

        if not candidates:
            return result

        try:
            with session_scope(self._engine) as session:
                existing = list(session.exec(select(UserPreference).where(UserPreference.user_id == user_id)).all())
                for fact in candidates:
                    same = next((row for row in existing if _similar(row.pref_value, fact.value)), None)
                    if same is not None and same.pref_key != fact.key:
                        result.duplicate_keys.append(fact.key)
                        continue
                    row = next((item for item in existing if item.pref_key == fact.key), None)
                    if row is None:
                        row = UserPreference(
                            user_id=user_id, pref_key=fact.key, pref_value="", source_session_id=source_session_id
                        )
                        session.add(row)
                        existing.append(row)
                    row.pref_value = fact.value[:200]
                    row.category = fact.category.value
                    row.source_session_id = source_session_id or row.source_session_id
                    row.updated_at = utcnow()
                    result.kept.append(fact)
        except Exception as exc:  # R-P4: memory failure must never fail the turn
            logger.warning("preference_write_failed", user_id=user_id, error=str(exc)[:200])
            result.rejected.append(("*", "storage_error"))
        return result

    def clear(self, user_id: str) -> int:
        with session_scope(self._engine) as session:
            rows = list(session.exec(select(UserPreference).where(UserPreference.user_id == user_id)).all())
            for row in rows:
                session.delete(row)
            return len(rows)
