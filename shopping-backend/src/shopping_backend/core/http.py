"""HTTP plumbing shared by both services: trace context, error shape, CORS.

The rule this file exists to enforce: an error body never contains a stack trace, and every
response carries a `request_id` an operator can grep for in `logs/shopping-*.jsonl`.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from shopping_backend.core.config import Settings
from shopping_backend.core.errors import (
    ERROR_TEXT,
    ConfigError,
    GuardrailViolation,
    NotOffered,
    ShoppingError,
    Unavailable,
    error_code,
    friendly_message,
)
from shopping_backend.core.ids import new_trace_id, reset_trace_id, set_trace_id
from shopping_backend.core.logging import bind_request, clear_request_context, logger

STATUS_BY_CODE = {
    "UNAVAILABLE": 503,
    "NOT_OFFERED": 200,
    "DB_UNAVAILABLE": 503,
    "CONFIG_ERROR": 503,
    "TIMEOUT": 504,
}


def install_middleware(app: FastAPI, settings: Settings) -> None:
    origins = settings.ALLOWED_ORIGINS

    @app.middleware("http")
    async def trace_context(request: Request, call_next):  # noqa: ANN001 - starlette signature
        incoming = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
        token = set_trace_id(incoming or new_trace_id())
        bind_request(trace_id=get_trace_id_safe(), path=request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            clear_request_context()
            reset_trace_id(token)
        response.headers["X-Trace-Id"] = get_trace_id_safe()
        response.headers["X-Elapsed-Ms"] = str(elapsed_ms)
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def get_trace_id_safe() -> str:
    from shopping_backend.core.ids import get_trace_id

    return get_trace_id() or "-"


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GuardrailViolation)
    async def _guardrail(request: Request, exc: GuardrailViolation) -> JSONResponse:
        logger.warning("guardrail_rejected", rule=exc.rule, path=request.url.path, rejected=len(exc.errors))
        return JSONResponse(
            status_code=422,
            content={
                "detail": ERROR_TEXT["INVALID_ARGUMENT"],
                "error_code": "GUARDRAIL_VIOLATION",
                "request_id": get_trace_id_safe(),
                "errors": exc.errors[:20],
            },
        )

    @app.exception_handler(ValidationError)
    async def _pydantic(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": ERROR_TEXT["INVALID_ARGUMENT"],
                "error_code": "INVALID_ARGUMENT",
                "request_id": get_trace_id_safe(),
                "errors": [
                    {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", ""))[:200]} for err in exc.errors()
                ][:20],
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": ERROR_TEXT["INVALID_ARGUMENT"],
                "error_code": "INVALID_ARGUMENT",
                "request_id": get_trace_id_safe(),
                "errors": [
                    {
                        "loc": list(err.get("loc", ())),
                        "msg": str(err.get("msg", ""))[:200],
                        "type": err.get("type", ""),
                    }
                    for err in exc.errors()
                ][:20],
            },
        )

    @app.exception_handler(ShoppingError)
    async def _shopping(request: Request, exc: ShoppingError) -> JSONResponse:
        code = exc.code or error_code(exc)
        status = STATUS_BY_CODE.get(code, 400 if not isinstance(exc, NotOffered) else 200)
        logger.warning("shopping_error", code=code, path=request.url.path, message=exc.message[:200])
        return JSONResponse(
            status_code=status,
            content={"detail": exc.friendly, "error_code": code, "request_id": get_trace_id_safe()},
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = exc.detail if isinstance(exc.detail, str) and exc.detail.isupper() else "INTERNAL_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": ERROR_TEXT.get(code, str(exc.detail)),
                "error_code": code,
                "request_id": get_trace_id_safe(),
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # the stack goes to the log, never to the caller (MaxKB shows the body verbatim)
        logger.error("unhandled_exception", path=request.url.path, error=f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "detail": friendly_message(exc),
                "error_code": "INTERNAL_ERROR",
                "request_id": get_trace_id_safe(),
            },
        )

    _ = (Unavailable, ConfigError)  # documented as subclasses of ShoppingError, handled above


def install_middleware_and_handlers(app: FastAPI, settings: Settings) -> None:
    install_middleware(app, settings)
    install_exception_handlers(app)
