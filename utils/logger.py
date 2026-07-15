"""Structured logging setup using structlog.

Provides a single `get_logger(name)` entry point that emits JSON-friendly
log lines (great for aggregation) while staying readable in a terminal.
"""
from __future__ import annotations

import logging
import sys

import structlog

from config.settings import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    """Configure structlog + stdlib logging once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = get_settings().log_level

    # Shared processors for both structlog and stdlib log records
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Render JSON in production, pretty console locally.
            structlog.processors.JSONRenderer()
            if sys.stdout.isatty() is False
            else structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging (used by pybit/aiohttp) into structlog
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )
    for noisy in ("urllib3", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str = "bot") -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger."""
    configure_logging()
    return structlog.get_logger(name)
