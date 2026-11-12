"""Business API: the mock stand-in for the Java backend (docs/API.md §3).

Everything here is read-only except `/admin/seed`. Prices are accepted in yuan at the edge and
converted to cents internally, so a future Java peer that speaks yuan needs no change on our side.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from shopping_backend.business_api.deps import get_services, require_token
from shopping_backend.core.config import get_settings
from shopping_backend.core.logging import logger
from shopping_backend.domain.context import utcnow
from shopping_backend.domain.schemas import (
    CouponQueryOut,
    GoodsOut,
    GoodsSearchOut,
    SeedIn,
    StockOut,
    StockQueryOut,
)
from shopping_backend.guardrails.validate_input import parse_coupon_query, parse_goods_query, parse_stock_query
from shopping_backend.services import GENERAL_KEY, Services

router = APIRouter(prefix="/api/v1", tags=["business"], dependencies=[Depends(require_token)])


def _yuan_to_cents(value: float | None) -> int | None:
    if value is None:
        return None
    return round(float(value) * 100)


@router.get("/goods/search", response_model=GoodsSearchOut)
async def search_goods(
    request: Request,
    q: str = Query(default="", max_length=200),
    category: str | None = Query(default=None, max_length=32),
    min_price: float | None = Query(default=None, ge=0, description="yuan"),
    max_price: float | None = Query(default=None, ge=0, description="yuan"),
    limit: int = Query(default=8, ge=1, le=20),
    include_off_sale: bool = Query(default=False),
    services: Services = Depends(get_services),
) -> GoodsSearchOut:
    query = parse_goods_query(
        {
            "query": q,
            "category": category,
            "min_price_cents": _yuan_to_cents(min_price),
            "max_price_cents": _yuan_to_cents(max_price),
            "limit": limit,
            "include_off_sale": include_off_sale,
        }
    )
    items = await asyncio.to_thread(services.goods.search, query)
    return GoodsSearchOut(
        items=items,
        total=len(items),
        limit=query.limit,
        query={
            "q": query.query,
            "category": query.category,
            "min_price_cents": query.min_price_cents,
            "max_price_cents": query.max_price_cents,
        },
    )


@router.get("/goods/{goods_id}", response_model=GoodsOut)
async def goods_detail(goods_id: str, request: Request, services: Services = Depends(get_services)) -> GoodsOut:
    item = await asyncio.to_thread(services.goods.get, goods_id)
    if item is None or item.status != "on_sale":
        # one answer for "unknown" and "off sale": internal lifecycle state is not the caller's business
        raise HTTPException(status_code=404, detail="GOODS_NOT_FOUND")
    return item


@router.post("/stock/query", response_model=StockQueryOut)
async def stock_query(payload: dict, request: Request, services: Services = Depends(get_services)) -> StockQueryOut:
    parsed = parse_stock_query(payload)
    rows = await asyncio.to_thread(services.stock.query, parsed.goods_ids)
    by_id = {row.goods_id: row for row in rows}
    # API.md §3.3: unknown ids must still appear with available=0, never as a missing key
    stock = {goods_id: by_id.get(goods_id) or StockOut(goods_id=goods_id) for goods_id in parsed.goods_ids}
    return StockQueryOut(stock=stock, as_of=utcnow())


@router.post("/coupon/query", response_model=CouponQueryOut)
async def coupon_query(payload: dict, request: Request, services: Services = Depends(get_services)) -> CouponQueryOut:
    parsed = parse_coupon_query(payload)
    selection = await asyncio.to_thread(services.coupon.pick, parsed.goods_ids)
    keyed = dict(selection.by_goods)
    if selection.general:
        keyed[GENERAL_KEY] = selection.general
    return CouponQueryOut(coupons=keyed, unavailable=selection.unavailable)


@router.post("/admin/seed")
async def admin_seed(payload: SeedIn, request: Request, services: Services = Depends(get_services)) -> dict:
    settings = get_settings()
    if not settings.is_dev:
        raise HTTPException(status_code=403, detail="FORBIDDEN")

    from shopping_backend.db.base import create_all
    from shopping_backend.seeds.loader import count_rows, load_seeds

    engine = request.app.state.engine
    create_all(engine)
    reset = payload.reset
    report = await asyncio.to_thread(lambda: load_seeds(engine, reset=reset))
    logger.info("admin_seed_executed", reset=payload.reset, goods=report.goods)
    return {"loaded": report.as_dict(), "rows": count_rows(engine)}


@router.get("/goods-meta/categories", response_model=list[str])
async def categories(request: Request, services: Services = Depends(get_services)) -> list[str]:
    """Used by the demo script and by MaxKB tool docs to show what the fixtures cover."""
    return await asyncio.to_thread(services.goods.categories)
