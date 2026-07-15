#!/usr/bin/env python3
"""Measure round-trip latency to Bybit's REST API from this host.

Run this BEFORE choosing a host and AFTER provisioning to confirm you are
in the right region. Target: < 5 ms median to the matching engine region.

    python scripts/latency_check.py
"""
from __future__ import annotations

import statistics
import time
import urllib.request

ENDPOINTS = {
    "Bybit main (AWS Singapore)": "https://api.bybit.com/v5/market/time",
    "Bybit testnet": "https://api-testnet.bybit.com/v5/market/time",
}
N = 50


def ping(url: str, n: int = N) -> list[float]:
    times: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        try:
            urllib.request.urlopen(url, timeout=5).read()
            times.append((time.perf_counter() - start) * 1000.0)
        except Exception as exc:
            print(f"  request error: {exc}")
    return times


def report(name: str, times: list[float]) -> None:
    if not times:
        print(f"{name}: no successful requests")
        return
    times.sort()
    p50 = statistics.median(times)
    p95 = times[int(len(times) * 0.95)]
    p99 = times[int(len(times) * 0.99)] if len(times) > 1 else times[-1]
    print(
        f"{name:35s} min={min(times):6.2f}ms  "
        f"p50={p50:6.2f}ms  p95={p95:6.2f}ms  p99={p99:6.2f}ms  (n={len(times)})"
    )


def main() -> None:
    print(f"Latency probe ({N} requests each)\n" + "-" * 70)
    for name, url in ENDPOINTS.items():
        report(name, ping(url))


if __name__ == "__main__":
    main()
