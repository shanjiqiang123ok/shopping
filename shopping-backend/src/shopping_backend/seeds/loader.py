"""Seed loader: JSON fixtures -> database, with validation and the preference write filter applied.

Fixtures are validated through the same DTOs the runtime uses, so a broken sample fails at seed
time instead of surfacing as an empty answer three layers away.

CLI: `uv run python -m shopping_backend.seeds.loader --reset`
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import delete, select

from shopping_backend.core.config import get_settings
from shopping_backend.core.ids import new_id
from shopping_backend.core.logging import configure_logging, logger
from shopping_backend.db.base import create_all
from shopping_backend.db.engine import create_engine_from_settings
from shopping_backend.db.session import session_scope
from shopping_backend.domain.enums import PreferenceCategory
from shopping_backend.domain.models_orm import AgentRequestLog, Coupon, Goods, StockRow, UserPreference
from shopping_backend.domain.schemas import CouponOut, GoodsOut
from shopping_backend.services.preference import find_blocked

SEEDS_DIR = Path(__file__).resolve().parent


@dataclass(slots=True)
class SeedReport:
    goods: int = 0
    stock: int = 0
    coupons: int = 0
    preferences: int = 0
    preferences_rejected: list[tuple[str, str]] = field(default_factory=list)
    dialect: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "goods": self.goods,
            "stock": self.stock,
            "coupons": self.coupons,
            "preferences": self.preferences,
            "preferences_rejected": [{"key": key, "reason": reason} for key, reason in self.preferences_rejected],
        }


def _read(name: str) -> dict[str, Any]:
    path = SEEDS_DIR / name
    if not path.exists():
        msg = f"seed fixture missing: {path}"
        raise FileNotFoundError(msg)
    return json.loads(path.read_text(encoding="utf-8"))


def load_seeds(engine: Engine, *, reset: bool = True) -> SeedReport:
    create_all(engine)
    report = SeedReport(dialect=engine.url.get_backend_name())

    with session_scope(engine) as session:
        if reset:
            for model in (AgentRequestLog, UserPreference, Coupon, StockRow, Goods):
                session.exec(delete(model))

        for row in _read("goods.json")["goods"]:
            dto = GoodsOut.model_validate(row)
            session.add(
                Goods(
                    id=dto.id,
                    sku=dto.sku,
                    title=dto.title,
                    category=dto.category,
                    brand=dto.brand,
                    description=dto.description,
                    price_cents=dto.price_cents,
                    currency=dto.currency,
                    tags=json.dumps(dto.tags, ensure_ascii=False),
                    status=dto.status.value,
                )
            )
            report.goods += 1

        for row in _read("stock.json")["stock"]:
            session.add(
                StockRow(
                    id=new_id("s_"),
                    goods_id=str(row["goods_id"]),
                    warehouse=str(row.get("warehouse", "WH-MAIN")),
                    on_hand=int(row.get("on_hand", 0)),
                    reserved=int(row.get("reserved", 0)),
                )
            )
            report.stock += 1

        for row in _read("coupons.json")["coupons"]:
            CouponOut.model_validate(row)  # fixture sanity check only; storage keeps the CSV form
            session.add(
                Coupon(
                    id=str(row["id"]),
                    code=str(row["code"]),
                    name=str(row.get("name", "")),
                    scope=str(row["scope"]),
                    scope_value=str(row.get("scope_value", "")),
                    threshold_cents=int(row.get("threshold_cents", 0)),
                    discount_type=str(row["discount_type"]),
                    discount_value=int(row.get("discount_value", 0)),
                    valid_from=_as_datetime(row.get("valid_from")),
                    valid_to=_as_datetime(row.get("valid_to")),
                    total_quota=int(row.get("total_quota", -1)),
                    used_count=int(row.get("used_count", 0)),
                    per_user_limit=int(row.get("per_user_limit", 1)),
                    status=str(row.get("status", "active")),
                )
            )
            report.coupons += 1

        for row in _read("preferences.json")["preferences"]:
            value = str(row["pref_value"])
            blocked = find_blocked(value)
            if blocked is not None:
                # R-P3 at seed time too: the rejected sample is reported, never stored, and the
                # stored value is never echoed back.
                report.preferences_rejected.append((str(row["pref_key"]), blocked))
                logger.info("seed_preference_refused", key=row["pref_key"], reason=blocked)
                continue
            session.add(
                UserPreference(
                    user_id=str(row["user_id"]),
                    pref_key=str(row["pref_key"]),
                    pref_value=value[:200],
                    category=PreferenceCategory(str(row.get("category", "preference"))).value,
                    source_session_id=str(row.get("source_session_id", "")),
                )
            )
            report.preferences += 1

    logger.info("seeds_loaded", **report.as_dict())
    return report


def _as_datetime(value: Any):
    if value in (None, ""):
        return None
    from datetime import datetime

    from shopping_backend.domain.context import ensure_utc

    return ensure_utc(datetime.fromisoformat(str(value)))


def count_rows(engine: Engine) -> dict[str, int]:
    with session_scope(engine) as session:
        return {
            "goods": len(session.exec(select(Goods)).all()),
            "stock": len(session.exec(select(StockRow)).all()),
            "coupon": len(session.exec(select(Coupon)).all()),
            "user_preference": len(session.exec(select(UserPreference)).all()),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load demo fixtures into the configured database.")
    parser.add_argument("--reset", action="store_true", default=True, help="delete existing rows first (default)")
    parser.add_argument("--no-reset", dest="reset", action="store_false")
    parser.add_argument("--url", default=None, help="override DATABASE_URL for this run")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.url:
        settings = settings.model_copy(update={"DATABASE_URL": args.url})
    configure_logging(settings)

    engine = create_engine_from_settings(settings)
    report = load_seeds(engine, reset=args.reset)
    print(json.dumps({"loaded": report.as_dict(), "rows": count_rows(engine)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
