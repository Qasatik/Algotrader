"""Application entry point.

Boots three concurrent subsystems on one asyncio loop:
  1. Prometheus metrics server (background thread),
  2. Telegram admin bot (polling),
  3. Trading engine (websocket-driven event loop).

Run locally:
    python main.py

Run in Docker:
    docker compose up
"""
from __future__ import annotations

import asyncio
import signal

from bot.telegram_admin import TelegramAdminBot
from config.settings import get_settings
from core.engine import TradingEngine
from utils.logger import get_logger
from utils.metrics import MetricsServer, update_from_status

log = get_logger("main")


async def _metrics_loop(engine: TradingEngine, stop_event: asyncio.Event) -> None:
    """Refresh Prometheus metrics every 5s until shutdown."""
    while not stop_event.is_set():
        try:
            update_from_status(await engine.status())
        except Exception as exc:
            log.warning("metrics_refresh_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    settings = get_settings()
    settings.assert_ready_for_live()
    log.info("boot", mode="paper" if settings.is_paper_mode else "LIVE",
             symbol=settings.trading_symbol, device=settings.ml_device)

    metrics = MetricsServer()
    metrics.start()

    engine = TradingEngine()
    tg_bot = TelegramAdminBot(engine)

    stop_event = asyncio.Event()

    # Graceful shutdown on SIGINT/SIGTERM (works in Docker/k8s)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    await tg_bot.start()
    log.info("ready_send /start to your Telegram bot to control the engine")

    metrics_task = asyncio.create_task(_metrics_loop(engine, stop_event))
    try:
        await stop_event.wait()
    finally:
        log.info("shutting_down")
        await engine.stop()
        await tg_bot.stop()
        metrics_task.cancel()
        metrics.stop()
        log.info("bye")


if __name__ == "__main__":
    asyncio.run(main())
