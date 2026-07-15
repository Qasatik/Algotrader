"""Structured logging setup using structlog.

Provides a single `get_logger(name)` entry point that emits JSON-friendly
log lines (great for aggregation) while staying readable in a terminal.
Logs are tee'd to both stdout and a rotating file (``logs/carry.log``).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from config.settings import get_settings

_CONFIGURED = False


class _RotatingTee:
    """Write to stdout *and* a size-rotated log file simultaneously."""

    def __init__(self, stdout, path: str, max_bytes: int, backup_count: int):
        self._stdout = stdout
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._f = open(self._path, "a", encoding="utf-8")

    def write(self, msg: str) -> int:
        self._stdout.write(msg)
        self._f.write(msg)
        self._f.flush()
        if self._f.tell() > self._max_bytes:
            self._rotate()
        return len(msg)

    def flush(self) -> None:
        self._stdout.flush()
        self._f.flush()

    def isatty(self) -> bool:
        return self._stdout.isatty()

    def fileno(self) -> int:
        return self._stdout.fileno()

    def _rotate(self) -> None:
        """Rename carry.log → carry.log.1 → carry.log.2 → ... (drop oldest)."""
        self._f.close()
        for i in range(self._backup_count - 1, 0, -1):
            old = self._path.parent / f"{self._path.name}.{i}"
            new = self._path.parent / f"{self._path.name}.{i + 1}"
            if old.exists():
                old.rename(new)
        self._path.rename(self._path.parent / f"{self._path.name}.1")
        self._f = open(self._path, "a", encoding="utf-8")


def configure_logging() -> None:
    """Configure structlog + stdlib logging once per process.

    All log output is tee'd to both stdout and a rotating file
    (``logs/carry.log``, 5 MB × 5 files).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = get_settings().log_level

    # Tee stdout → stdout + rotating file so logs survive terminal close.
    try:
        _tee = _RotatingTee(
            sys.stdout, "logs/carry.log", max_bytes=5 * 1024 * 1024, backup_count=5
        )
    except Exception:
        _tee = sys.stdout  # fall back to stdout-only if dir creation fails

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
        logger_factory=structlog.PrintLoggerFactory(file=_tee),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging (used by pybit/aiohttp) into the same tee
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=_tee,
    )
    for noisy in ("urllib3", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str = "bot") -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger."""
    configure_logging()
    return structlog.get_logger(name)
