"""Lightweight push-notification helper for the carry bot.

Sends short messages to Telegram admins via the Bot API (stdlib only — no
extra dependencies).  If ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_ADMIN_IDS`` are
not configured, every call silently no-ops so the bot runs fine without
Telegram.

Usage::

    from utils.notifier import notify

    notify("🟢 Opened carry: 0.001 BTC @ funding +0.0071%")
"""
from __future__ import annotations

import urllib.parse
import urllib.request

from config.settings import get_settings
from utils.logger import get_logger

_log = get_logger("notifier")


def is_configured() -> bool:
    """True when Telegram credentials are present."""
    s = get_settings()
    return bool(s.telegram_bot_token and s.admin_ids)


def notify(message: str) -> bool:
    """Push *message* to all configured Telegram admins.

    Returns ``True`` if at least one delivery succeeded, ``False`` otherwise
    (including when Telegram is not configured).  **Never raises** — a
    notification failure must never crash the trading loop.
    """
    s = get_settings()
    if not s.telegram_bot_token or not s.admin_ids:
        return False

    ok = False
    for chat_id in s.admin_ids:
        try:
            url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
            data = urllib.parse.urlencode(
                {
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": "true",
                }
            ).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            urllib.request.urlopen(req, timeout=10)
            ok = True
        except Exception as exc:
            _log.warning("notify_failed", chat_id=chat_id, error=str(exc))
    return ok
