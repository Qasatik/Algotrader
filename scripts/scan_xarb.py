#!/usr/bin/env python3
"""X0 scanner: log sign-opposite funding slots, Bybit vs OKX.

Implements Stage X0 of SPEC_CROSS_EXCHANGE_OKX.md: public-data only, no API
keys, no orders. Every run polls both venues' funding tables, pairs symbols
by base asset, computes the combined per-slot EV of a delta-neutral perp/perp
pair (short the venue with positive funding, long the venue with negative
funding — both legs RECEIVE), and appends qualifying slots to a CSV log.

Gate to X1 (paper runner): >= 2 weeks of logged slots, >= 5 events with
EV >= min_ev_bps (default 10).

Usage:
    python3 scripts/scan_xarb.py [--min-ev-bps 10] [--top 10]
        [--csv data/xarb_slots.csv] [--universe 120] [--timeout 10]

Exit codes: 0 = scan completed (even if no slots found), 2 = venue fetch failed.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
OKX_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
OKX_TICKERS = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate?instId={inst}"

MIN_TURNOVER_USD = 5_000_000  # same liquidity floor as the carry bot


_UA = "Mozilla/5.0 (X11; Linux x86_64) bybit-algo-bot/1.0"


def _get_json(url: str, timeout: int) -> dict:
    import json

    req = Request(url, headers={"User-Agent": _UA})  # OKX 403s the default urllib UA
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _base_from_bybit(symbol: str) -> str | None:
    """BTCUSDT -> BTC; None for non-USDT quotes."""
    if not symbol.endswith("USDT"):
        return None
    base = symbol[: -len("USDT")]
    return base or None


def _base_from_okx(inst_id: str) -> str | None:
    """BTC-USDT-SWAP -> BTC; None for non-USDT swaps."""
    parts = inst_id.split("-")
    if len(parts) != 3 or parts[1] != "USDT":
        return None
    return parts[0]


def slot_ev(funding_bybit: float, funding_okx: float) -> tuple[str, float]:
    """Best per-slot EV of a perp/perp pair, in bps of notional.

    Returns (direction, ev_bps). Direction is which leg is SHORT:
      - "short_bybit": Bybit funding > 0 (short receives) AND OKX < 0
        (long receives) -> EV = f_bybit + |f_okx|
      - "short_okx": mirror case.
    Same-sign funding means one leg pays -> no trade, EV 0.
    """
    ev_bps = 0.0
    direction = ""
    if funding_bybit > 0 and funding_okx < 0:
        ev_bps = (funding_bybit - funding_okx) * 10_000.0
        direction = "short_bybit"
    elif funding_bybit < 0 and funding_okx > 0:
        ev_bps = (-funding_bybit + funding_okx) * 10_000.0
        direction = "short_okx"
    return direction, ev_bps


def fetch_bybit(timeout: int) -> dict[str, float]:
    """{base: funding_rate} for liquid USDT perps on Bybit."""
    data = _get_json(BYBIT_TICKERS, timeout)["result"]["list"]
    out: dict[str, float] = {}
    for t in data:
        base = _base_from_bybit(t.get("symbol", ""))
        if base is None:
            continue
        try:
            fr = float(t["fundingRate"])
            turnover = float(t["turnover24h"])
        except (ValueError, KeyError, TypeError):
            continue
        if turnover >= MIN_TURNOVER_USD:
            out[base] = fr
    return out


def fetch_okx(timeout: int, universe: int) -> dict[str, float]:
    """{base: funding_rate} for the top-`universe` liquid USDT swaps on OKX.

    OKX has no bulk funding endpoint, so the ticker table (one call) ranks
    instruments by 24h turnover and only those get a per-inst funding call,
    in parallel.
    """
    instruments = {
        i["instId"]: _base_from_okx(i["instId"])
        for i in _get_json(OKX_INSTRUMENTS, timeout)["data"]
    }
    tickers = _get_json(OKX_TICKERS, timeout)["data"]
    ranked = []
    for t in tickers:
        inst_id = t.get("instId", "")
        base = instruments.get(inst_id)
        if base is None:
            continue
        try:
            turnover = float(t.get("volCcy24h", 0.0))
        except (ValueError, TypeError):
            continue
        ranked.append((turnover, inst_id, base))
    ranked.sort(reverse=True)
    ranked = ranked[:universe]

    out: dict[str, float] = {}

    def _one(item: tuple[float, str, str]) -> tuple[str, float] | None:
        _, inst_id, base = item
        try:
            d = _get_json(OKX_FUNDING.format(inst=inst_id), timeout)["data"][0]
            return base, float(d["fundingRate"])
        except (ValueError, KeyError, IndexError, OSError):
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(_one, item) for item in ranked]
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                out[res[0]] = res[1]
    return out


def scan(
    bybit: dict[str, float],
    okx: dict[str, float],
    min_ev_bps: float,
) -> list[dict]:
    """All sign-opposite pairs with EV >= min_ev_bps, best first."""
    rows = []
    for base in bybit.keys() & okx.keys():
        direction, ev_bps = slot_ev(bybit[base], okx[base])
        if direction and ev_bps >= min_ev_bps:
            rows.append(
                {
                    "base": base,
                    "direction": direction,
                    "funding_bybit": bybit[base],
                    "funding_okx": okx[base],
                    "ev_bps": round(ev_bps, 2),
                }
            )
    rows.sort(key=lambda r: r["ev_bps"], reverse=True)
    return rows


def append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(
                ["timestamp", "base", "direction", "funding_bybit", "funding_okx", "ev_bps"]
            )
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            writer.writerow(
                [ts, r["base"], r["direction"], r["funding_bybit"], r["funding_okx"], r["ev_bps"]]
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-ev-bps", type=float, default=10.0)
    parser.add_argument("--top", type=int, default=10, help="rows to print")
    parser.add_argument("--csv", default="data/xarb_slots.csv")
    parser.add_argument("--universe", type=int, default=120, help="OKX instruments to poll")
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        bybit = fetch_bybit(args.timeout)
    except OSError as exc:
        print(f"bybit fetch failed: {exc}", file=sys.stderr)
        return 2
    try:
        okx = fetch_okx(args.timeout, args.universe)
    except OSError as exc:
        print(f"okx fetch failed: {exc}", file=sys.stderr)
        return 2

    rows = scan(bybit, okx, args.min_ev_bps)
    append_csv(Path(args.csv), rows)

    print(
        f"venues: bybit={len(bybit)} okx={len(okx)} common={len(bybit.keys() & okx.keys())} "
        f"slots_ev_ge_{args.min_ev_bps}bps={len(rows)}"
    )
    for r in rows[: args.top]:
        print(
            f"  {r['base']:10s} {r['direction']:14s} "
            f"f_bybit={r['funding_bybit'] * 100:+.4f}% f_okx={r['funding_okx'] * 100:+.4f}% "
            f"EV={r['ev_bps']:.1f}bps/slot"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
