"""Graph nodes. Each is a factory closing over `GraphDeps` — no module-level singletons."""

from shopping_backend.graph.nodes.coupon_worker import make_coupon_worker
from shopping_backend.graph.nodes.goods_worker import make_goods_worker
from shopping_backend.graph.nodes.presentation import render
from shopping_backend.graph.nodes.stock_worker import make_stock_worker
from shopping_backend.graph.nodes.summarize import make_summarize

__all__ = ["make_coupon_worker", "make_goods_worker", "make_stock_worker", "make_summarize", "render"]
