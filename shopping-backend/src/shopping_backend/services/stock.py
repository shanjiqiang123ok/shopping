"""Stock read model — R-S1..R-S5.

Availability is computed here and nowhere else, so the HTTP layer, the graph and MaxKB can never
disagree about what "有货" means.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from shopping_backend.db.session import session_scope
from shopping_backend.domain.models_orm import StockRow
from shopping_backend.domain.schemas import StockOut, WarehouseStock


def aggregate(rows: list[StockRow]) -> StockOut:
    """R-S1: available = sum over warehouses of max(on_hand - reserved, 0)."""
    warehouses = [
        WarehouseStock(
            warehouse=row.warehouse,
            on_hand=row.on_hand,
            reserved=row.reserved,
        )
        for row in rows
    ]
    return StockOut(
        goods_id=rows[0].goods_id,
        warehouses=warehouses,
    )


class StockService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def query(self, goods_ids: list[str]) -> list[StockOut]:
        """Returns rows that exist. Goods with no stock row are simply absent — R-S2 turns that
        into an explicit zero inside the guardrail layer, not here, so the two paths stay testable."""
        ids = list(dict.fromkeys(goods_ids))
        if not ids:
            return []
        with session_scope(self._engine) as session:
            rows = session.exec(select(StockRow).where(StockRow.goods_id.in_(ids))).all()

        grouped: dict[str, list[StockRow]] = {}
        for row in rows:
            grouped.setdefault(row.goods_id, []).append(row)

        ordered: list[StockOut] = []
        for goods_id in ids:
            if goods_id in grouped:
                ordered.append(aggregate(grouped[goods_id]))
        return ordered

    def available_map(self, goods_ids: list[str]) -> dict[str, int]:
        return {stock.goods_id: stock.available for stock in self.query(goods_ids)}

    def detail(self, goods_id: str) -> StockOut | None:
        found = self.query([goods_id])
        return found[0] if found else None
