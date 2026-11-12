"""Goods read model — the single source of the search/visibility rules (R-G1..R-G4).

The business layer is synchronous on purpose (ADR A4): both services, the seeds loader and the
tests can call it directly, and the async graph wraps it in `asyncio.to_thread`.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from shopping_backend.db.session import session_scope
from shopping_backend.domain.enums import GoodsStatus
from shopping_backend.domain.models_orm import Goods
from shopping_backend.domain.schemas import GoodsOut, GoodsQuery


def to_dto(row: Goods) -> GoodsOut:
    return GoodsOut(
        id=row.id,
        sku=row.sku,
        title=row.title,
        category=row.category,
        brand=row.brand,
        description=row.description,
        price_cents=row.price_cents,
        currency=row.currency,
        tags=row.tags,
        status=GoodsStatus(row.status),
    )


def _normalize(value: str) -> str:
    return (value or "").casefold().replace(" ", "")


# Text matching runs in python, not SQL: LIKE case-folding and collation differ between SQLite and
# MySQL, and the candidate set is already small after the indexed category/price filters.
def _relevance(item: GoodsOut, needle: str) -> int:
    """0 = no match; higher = better.

    Every branch compares the *needle* against one field of the item; comparing an item's own tags
    against its own title (as an earlier draft did) scores every tagged product and drowns the
    real matches.
    """
    if not needle:
        return 1
    target = _normalize(needle)
    title = _normalize(item.title)
    brand = _normalize(item.brand)
    category = _normalize(item.category)
    if target == title:
        return 6
    if target in title:
        return 5
    if brand and (target in brand or brand in target):
        return 4
    if any(tag and (target in tag or tag in target) for tag in (_normalize(value) for value in item.tags)):
        return 3
    if category and target in category:
        return 2
    # last resort: any single token of the needle appears in the title
    if any(len(token) >= 2 and _normalize(token) in title for token in needle.split()):
        return 1
    return 0


class GoodsService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def search(self, query: GoodsQuery) -> list[GoodsOut]:
        with session_scope(self._engine) as session:
            statement = select(Goods)
            if not query.include_off_sale:
                statement = statement.where(Goods.status == GoodsStatus.ON_SALE.value)  # R-G1
            if query.category:
                statement = statement.where(Goods.category == query.category.strip().lower())
            if query.max_price_cents is not None:
                statement = statement.where(Goods.price_cents <= query.max_price_cents)  # R-G3 inclusive
            if query.min_price_cents is not None:
                statement = statement.where(Goods.price_cents >= query.min_price_cents)
            rows = [to_dto(row) for row in session.exec(statement).all()]

        needle = query.query.strip()
        scored = [(item, _relevance(item, needle)) for item in rows]
        matched = [(item, score) for item, score in scored if score > 0] if needle else [(item, 1) for item in rows]
        matched.sort(key=lambda pair: (-pair[1], pair[0].price_cents, pair[0].id))
        return [item for item, _ in matched[: query.limit]]

    def get(self, goods_id: str) -> GoodsOut | None:
        with session_scope(self._engine) as session:
            row = session.exec(select(Goods).where(Goods.id == goods_id)).first()
            return to_dto(row) if row else None

    def list_by_ids(self, goods_ids: list[str]) -> list[GoodsOut]:
        if not goods_ids:
            return []
        with session_scope(self._engine) as session:
            rows = session.exec(select(Goods).where(Goods.id.in_(goods_ids))).all()
        by_id = {row.id: row for row in rows}
        return [to_dto(by_id[goods_id]) for goods_id in goods_ids if goods_id in by_id]

    def categories(self) -> list[str]:
        with session_scope(self._engine) as session:
            rows = session.exec(select(Goods.category).distinct()).all()
        return sorted({str(row).strip().lower() for row in rows if row})

    def count(self, *, on_sale_only: bool = True) -> int:
        with session_scope(self._engine) as session:
            statement = select(Goods)
            if on_sale_only:
                statement = statement.where(Goods.status == GoodsStatus.ON_SALE.value)
            return len(session.exec(statement).all())
