"""O3 — Lightweight alert dispatcher for Telegram notifications.

Decouples event producers (engine, risk manager, data feed) from the
Telegram bot: components just call ``notify("...")`` and any registered
sender (the Telegram bot) delivers it to all admins.

If no sender is registered (e.g. running headless), messages are logged.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from utils.logger import get_logger

log = get_logger("alerts")

# A sender is an async callable that takes a message string.
AlertSender = Callable[[str], Awaitable[None]]

_sender: AlertSender | None = None


def set_alert_sender(sender: AlertSender | None) -> None:
    """Register (or clear) the global alert sender."""
    global _sender
    _sender = sender
    log.info("alert_sender_registered" if sender else "alert_sender_cleared")


async def notify(message: str) -> None:
    """Send an alert to all admins via the registered sender.

    Safe to call from anywhere; never raises into the caller.
    """
    try:
        if _sender is not None:
            await _sender(message)
        else:
            log.info("alert_no_sender", message=message)
    except Exception as exc:
        log.warning("alert_send_failed", error=str(exc), message=message)


def notify_now(message: str) -> None:
    """Fire-and-forget ``notify`` from a sync context (schedules on the loop)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(notify(message))
    except RuntimeError:
        # No running loop -> just log.
        log.info("alert_no_loop", message=message)
