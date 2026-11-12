"""Agent runtime: owns the engine, the checkpointer, the compiled graph, and one request's lifecycle.

`create_app()` wires this up in the lifespan; tests construct it directly with an injected engine so
the whole service can be exercised in-process.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session

from shopping_backend.core.config import Settings
from shopping_backend.core.errors import DEGRADED_FALLBACK_TEXT, Unavailable, error_code, friendly_message
from shopping_backend.core.ids import get_trace_id, new_id
from shopping_backend.core.logging import logger
from shopping_backend.domain.context import ShoppingSessionContext, utcnow
from shopping_backend.domain.enums import Intent, PreferenceCategory
from shopping_backend.domain.formatting import format_price_cents
from shopping_backend.domain.models_orm import AgentRequestLog
from shopping_backend.domain.schemas import (
    AgentRunData,
    AgentRunIn,
    AgentRunMeta,
    AgentRunOut,
    CouponOut,
    GoodsOut,
    GoodsView,
    PreferenceFact,
    StockOut,
)
from shopping_backend.graph.builder import compile_graph, graph_config
from shopping_backend.graph.checkpoint import CheckpointProvider, open_checkpoint_provider
from shopping_backend.graph.deps import GraphDeps, build_deps
from shopping_backend.graph.nodes.presentation import CATEGORY_DISPLAY
from shopping_backend.graph.state import ShoppingAgentState
from shopping_backend.services import Services


@dataclass(slots=True)
class AgentRuntime:
    settings: Settings
    engine: Engine | None = None
    provider: CheckpointProvider | None = None
    deps: GraphDeps | None = None
    graph: Any = None
    services: Services | None = None
    started: bool = False

    # ------------------------------------------------------------------ lifecycle

    async def startup(self, *, engine: Engine | None = None) -> None:
        from shopping_backend.db.engine import create_engine_from_settings

        # An injected engine must survive: falling back to settings would open a *different*
        # database than the one create_all/seed just prepared.
        self.engine = engine or self.engine or create_engine_from_settings(self.settings)
        self.provider = await open_checkpoint_provider(self.settings)
        self.deps = build_deps(self.settings, engine=self.engine)
        self.services = self.deps.services
        self.graph = compile_graph(self.deps, self.provider.saver)
        self.started = True
        logger.info("agent_runtime_started", **self.describe())

    async def shutdown(self) -> None:
        if self.deps is not None:
            await self.deps.backend.aclose()
        if self.provider is not None:
            await self.provider.aclose()
        if self.engine is not None:
            self.engine.dispose()
        self.started = False
        self.graph = None

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.settings.effective_mode, "checkpointer": self.settings.CHECKPOINT_BACKEND}
        if self.deps is not None:
            info.update(self.deps.describe())
        if self.provider is not None:
            info["checkpoint"] = self.provider.describe()
        return info

    @property
    def backend_name(self) -> str:
        backend = getattr(self.deps, "backend", None)
        return str(getattr(backend, "driver", self.settings.BACKEND_DRIVER))

    # ------------------------------------------------------------------ request

    async def run(self, payload: AgentRunIn) -> AgentRunOut:
        started = time.perf_counter()
        ctx = ShoppingSessionContext(
            user_id=payload.user_id,
            session_id=payload.session_id,
            tenant_id=payload.tenant_id,
            trace_id=get_trace_id() or new_id("t_")[:12],
        )
        preferences = self._merged_preferences(payload)
        state = ShoppingAgentState(
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            query=payload.query,
            trace_id=ctx.trace_id,
            preference_memory=preferences,
        )

        try:
            values = await asyncio.wait_for(
                self.graph.ainvoke(state.as_input(), graph_config(ctx.thread_id, trace_id=ctx.trace_id)),
                timeout=self.settings.AGENT_TOTAL_TIMEOUT_S,
            )
        except TimeoutError:
            logger.error("agent_total_timeout", thread_id=ctx.thread_id, budget_s=self.settings.AGENT_TOTAL_TIMEOUT_S)
            return self._finish(
                ctx,
                {
                    "final_answer": Unavailable.friendly,
                    "intent": Intent.OTHER.value,
                    "degraded": True,
                    "route_trace": ["supervisor", "summarize"],
                },
                started,
                preferences,
                code="TIMEOUT",
            )
        except Exception as exc:  # the graph must not hand MaxKB a 500 with a stack (阶段3 异常适配)
            logger.error("agent_graph_failed", thread_id=ctx.thread_id, error=f"{type(exc).__name__}: {exc}")
            return self._finish(
                ctx,
                {
                    "final_answer": friendly_message(exc),
                    "intent": Intent.OTHER.value,
                    "degraded": True,
                    "route_trace": ["supervisor", "summarize"],
                },
                started,
                preferences,
                code=error_code(exc) or "INTERNAL_ERROR",
            )

        return self._finish(ctx, values, started, preferences)

    def _merged_preferences(self, payload: AgentRunIn) -> dict[str, str]:
        if self.services is not None:
            try:
                return self.services.preference.merged(payload.user_id, payload.preference_memory)
            except Exception as exc:  # storage trouble must not block the answer
                logger.warning("preference_read_failed", error=str(exc)[:200])
        return dict(payload.preference_memory)

    # ------------------------------------------------------------------ assembly + side effects

    def _finish(
        self,
        ctx: ShoppingSessionContext,
        values: dict[str, Any],
        started: float,
        preferences: dict[str, str],
        *,
        code: str | None = None,
    ) -> AgentRunOut:
        goods = [GoodsOut.model_validate(row) for row in values.get("goods_list", [])]
        stock = {key: StockOut.model_validate(row) for key, row in (values.get("stock_map") or {}).items()}
        coupons = {
            key: [CouponOut.model_validate(row) for row in rows]
            for key, rows in (values.get("coupon_map") or {}).items()
        }
        reasons = dict(values.get("reasons") or {})

        views = [
            GoodsView(
                id=item.id,
                sku=item.sku,
                title=item.title,
                price_cents=item.price_cents,
                currency=item.currency,
                category=item.category,
                brand=item.brand,
                available=stock[item.id].available if item.id in stock else 0,
                coupon_codes=[coupon.code for coupon in coupons.get(item.id, [])],
                reason=reasons.get(item.id, ""),
            )
            for item in goods
        ]

        counts = {
            "goods": len(goods),
            "stock": len(stock),
            "coupons": sum(len(rows) for rows in coupons.values()),
            "dropped_rows": int(values.get("dropped_rows") or 0),
            "grounding_drops": int(values.get("grounding_drops") or 0),
        }
        meta = AgentRunMeta(
            mode=self.settings.effective_mode,
            backend=self.backend_name,
            route_trace=list(values.get("route_trace") or []),
            seen_goods_ids=list(values.get("seen_goods_ids") or []),
            counts=counts,
            degraded=bool(values.get("degraded")),
            error_msg=values.get("error_msg"),
            latency_ms=round((time.perf_counter() - started) * 1000),
            thread_id=ctx.thread_id,
            preference_summary=_preference_summary(preferences),
        )
        out = AgentRunOut(
            request_id=new_id("req_"),
            trace_id=ctx.trace_id,
            answer=str(values.get("final_answer") or DEGRADED_FALLBACK_TEXT),
            data=AgentRunData(
                intent=str(values.get("intent") or Intent.OTHER.value),
                goods=views,
                stock=stock,
                coupons=coupons,
                out_of_stock_ids=list(values.get("out_of_stock_ids") or []),
            ),
            meta=meta,
        )

        self._write_log(ctx, out, code=code)
        self._store_preferences(ctx, values)
        return out

    def _write_log(self, ctx: ShoppingSessionContext, out: AgentRunOut, *, code: str | None = None) -> None:
        """Operational trail (docs/ARCHITECTURE.md §6): best-effort, never fails the turn."""
        if self.engine is None:
            return
        try:
            row = AgentRequestLog(
                id=new_id("log_"),
                trace_id=ctx.trace_id[:32],
                session_id=ctx.session_id[:64],
                user_id=ctx.user_id[:64],
                mode=out.meta.mode,
                backend_driver=out.meta.backend,
                intent=out.data.intent[:24],
                route_trace=json.dumps(out.meta.route_trace, ensure_ascii=False)[:2000],
                goods_n=out.meta.counts.get("goods", 0),
                stock_n=out.meta.counts.get("stock", 0),
                coupon_n=out.meta.counts.get("coupons", 0),
                dropped_rows_n=out.meta.counts.get("dropped_rows", 0),
                grounding_drops_n=out.meta.counts.get("grounding_drops", 0),
                latency_ms=out.meta.latency_ms,
                degraded=out.meta.degraded,
                error_code=(code or ("DEGRADED" if out.meta.degraded else ""))[:32],
                created_at=utcnow(),
            )
            with Session(self.engine) as session:
                session.add(row)
                session.commit()
        except Exception as exc:
            logger.warning("request_log_failed", error=str(exc)[:200])

    def _store_preferences(self, ctx: ShoppingSessionContext, values: dict[str, Any]) -> None:
        if self.services is None:
            return
        facts = derive_preference_facts(values)
        if not facts:
            return
        try:
            result = self.services.preference.store(ctx.user_id, facts, source_session_id=ctx.session_id)
            if result.rejected:
                logger.info(
                    "preference_rejected_at_write",
                    user_id=ctx.user_id,
                    reasons=[reason for _, reason in result.rejected],
                )
        except Exception as exc:  # R-P4
            logger.warning("preference_write_failed", error=str(exc)[:200])

    async def health(self) -> dict[str, Any]:
        from shopping_backend.db.session import health_check

        db_ok = self.engine is not None and health_check(self.engine)
        return {
            "status": "ok" if (db_ok and self.started) else "degraded",
            "db": "ok" if db_ok else "unavailable",
            "checkpointer": "ok" if self.provider is not None else "unavailable",
            "mode": self.settings.effective_mode,
            "backend_driver": self.backend_name,
            "llm_enabled": self.settings.llm_enabled,
            "started": self.started,
        }


def derive_preference_facts(values: dict[str, Any]) -> list[PreferenceFact]:
    """Deterministic extraction for rule_based mode.

    Only what the user literally asked for is remembered, so nothing stored here can be a
    hallucination (R-P1). Model-driven extraction is an M7 addition behind the same write filter.
    """
    slots = dict(values.get("slots") or {})
    facts: list[PreferenceFact] = []
    max_cents = slots.get("max_price_cents")
    if isinstance(max_cents, int) and max_cents > 0:
        yuan = format_price_cents(max_cents).removeprefix("¥")
        facts.append(
            PreferenceFact(key="budget_limit", value=f"预算不超过 {yuan} 元", category=PreferenceCategory.CONSTRAINT)
        )
    category = slots.get("category")
    if isinstance(category, str) and category:
        facts.append(
            PreferenceFact(
                key="preferred_category",
                value=f"主要看{CATEGORY_DISPLAY.get(category, category)}",
                category=PreferenceCategory.PREFERENCE,
            )
        )
    return facts[:3]


def _preference_summary(preferences: dict[str, str]) -> str | None:
    if not preferences:
        return None
    return "；".join(f"{key}={value}" for key, value in list(preferences.items())[:3])[:200]
