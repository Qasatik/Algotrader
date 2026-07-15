"""Prometheus metrics server.

Exposes a tiny HTTP endpoint (default :9090/metrics) so a DevOps stack
(Prometheus + Grafana) can scrape bot health: engine state, order counters,
equity, and signal latency.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from config.settings import get_settings

_REGISTRY = CollectorRegistry()

# ---- Metric definitions -------------------------------------------
ENGINE_STATE = Gauge(
    "bybit_engine_state", "Engine state (0=stopped,1=running,2=paused,3=killed)",
    registry=_REGISTRY,
)
EQUITY_USDT = Gauge("bybit_equity_usdt", "Account equity in USDT", registry=_REGISTRY)
ORDERS_TOTAL = Gauge(
    "bybit_orders_total", "Orders placed", ["kind"], registry=_REGISTRY
)
SIGNALS_TOTAL = Gauge("bybit_signals_total", "Signals generated", registry=_REGISTRY)
MID_PRICE = Gauge("bybit_mid_price", "Current mid price", registry=_REGISTRY)

_STATE_MAP = {"stopped": 0, "running": 1, "paused": 2, "killed": 3}


def update_from_status(status: dict) -> None:
    """Push the latest engine status into Prometheus gauges."""
    ENGINE_STATE.set(_STATE_MAP.get(status.get("state", "stopped"), 0))
    EQUITY_USDT.set(status.get("equity_usdt", 0) or 0)
    stats = status.get("stats", {})
    SIGNALS_TOTAL.set(stats.get("signals", 0))
    ORDERS_TOTAL.labels(kind="filled").set(stats.get("orders_filled", 0))
    ORDERS_TOTAL.labels(kind="failed").set(stats.get("orders_failed", 0))
    ORDERS_TOTAL.labels(kind="placed").set(stats.get("orders_placed", 0))
    MID_PRICE.set(status.get("mid_price") or 0)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = generate_latest(_REGISTRY)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default logging
        pass


class MetricsServer:
    """Background HTTP server serving /metrics."""

    def __init__(self) -> None:
        port = get_settings().metrics_port
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
