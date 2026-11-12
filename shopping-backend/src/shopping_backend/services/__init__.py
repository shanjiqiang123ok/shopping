"""Business rule layer, shared by the mock business API and the in-process backend."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from shopping_backend.services.coupon import GENERAL_KEY, CouponSelection, CouponService
from shopping_backend.services.goods import GoodsService
from shopping_backend.services.preference import PreferenceService
from shopping_backend.services.stock import StockService


@dataclass(slots=True)
class Services:
    goods: GoodsService
    stock: StockService
    coupon: CouponService
    preference: PreferenceService


def build_services(engine: Engine) -> Services:
    return Services(
        goods=GoodsService(engine),
        stock=StockService(engine),
        coupon=CouponService(engine),
        preference=PreferenceService(engine),
    )


__all__ = [
    "GENERAL_KEY",
    "CouponSelection",
    "CouponService",
    "GoodsService",
    "PreferenceService",
    "Services",
    "StockService",
    "build_services",
]
