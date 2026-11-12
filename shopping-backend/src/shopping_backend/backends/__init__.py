"""Backend adapters: one ABC, three interchangeable drivers."""

from shopping_backend.backends.base import (
    BackendResult,
    CouponBundle,
    CouponBundleRaw,
    StorefrontBackend,
    build_backend,
)
from shopping_backend.backends.http import HttpBackend
from shopping_backend.backends.http_java import HttpJavaBackend
from shopping_backend.backends.inprocess import InProcessBackend

__all__ = [
    "BackendResult",
    "CouponBundle",
    "CouponBundleRaw",
    "HttpBackend",
    "HttpJavaBackend",
    "InProcessBackend",
    "StorefrontBackend",
    "build_backend",
]
