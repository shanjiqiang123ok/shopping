"""structlog wiring: pretty console in dev, JSONL file always, correlation id in every line."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from shopping_backend.core.config import Settings
from shopping_backend.core.ids import get_trace_id

_configured = False


def _pre_chain() -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%dT%H:%M:%S.%f", utc=True),
        _add_trace_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def _add_trace_id(_logger: object, _method: str, event_dict: dict) -> dict:
    trace_id = get_trace_id()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    return event_dict


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return

    settings = settings or Settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    structlog.configure(
        processors=[*_pre_chain(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    logger = logging.getLogger("shopping_backend")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
            ],
            foreign_pre_chain=_pre_chain(),
        )
    )
    logger.addHandler(console)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        logfile = Path(settings.log_dir) / f"shopping-{settings.APP_ENV}.jsonl"
        file_handler = RotatingFileHandler(logfile, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(ensure_ascii=False),
                ],
                foreign_pre_chain=_pre_chain(),
            )
        )
        logger.addHandler(file_handler)
    except OSError as exc:  # a missing log dir must never stop the service
        logger.warning("file logging disabled", error=str(exc), path=str(settings.log_dir))

    _configured = True


def bind_request(**kwargs: object) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


logger = structlog.get_logger("shopping_backend")
