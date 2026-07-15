#!/usr/bin/env python3
"""Download historical funding rates for a linear perpetual to Parquet.

Funding is the periodic cash flow between longs and shorts on perpetual
futures — the core signal for the delta-neutral carry strategy. Bybit funds
every 8 hours (00:00, 08:00, 16:00 UTC).

Usage:
    python scripts/download_funding.py --symbol BTCUSDT --days 1500
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import get_logger

log = get_logger("funding_download")

DATA_DIR = "data"
API = "https://api.bybit.com/v5/market/funding/history"


def _fetch(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Page through funding history (newest-first, max 200/call)."""
    rows: list[dict] = []
    cursor = end_ms
    while cursor > start_ms:
        url = f"{API}?category=linear&symbol={symbol}&startTime={start_ms}&endTime={cursor}&limit=200"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        if data.get("retCode") != 0:
            log.error("api_error", retCode=data.get("retCode"), msg=data.get("retMsg"))
            break
        batch = data.get("result", {}).get("list", [])
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(b["fundingRateTimestamp"]) for b in batch)
        log.info("fetched", batch=len(batch), total=len(rows),
                 oldest=time.strftime("%Y-%m-%d", time.gmtime(oldest / 1000)))
        if oldest <= start_ms:
            break
        cursor = oldest - 1
        time.sleep(0.2)
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Download Bybit funding rate history")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days", type=int, default=1500)
    args = p.parse_args()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86_400_000

    log.info("start", symbol=args.symbol, days=args.days)
    rows = _fetch(args.symbol, start_ms, now_ms)
    if not rows:
        print("No funding data downloaded.")
        return

    df = pd.DataFrame(rows)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"])
    df["ts"] = pd.to_numeric(df["fundingRateTimestamp"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("time").reset_index(drop=True)

    import os
    os.makedirs(DATA_DIR, exist_ok=True)
    path = f"{DATA_DIR}/{args.symbol}_funding.parquet"
    pq.write_table(pa.Table.from_pandas(df[["time", "fundingRate", "ts"]], preserve_index=False), path)
    print(f"\nSaved {len(df)} funding events -> {path}")
    print(f"Range: {df['time'].iloc[0]}  ..  {df['time'].iloc[-1]}")
    print(f"Rate stats: mean={df['fundingRate'].mean()*100:.4f}%  "
          f"min={df['fundingRate'].min()*100:.4f}%  max={df['fundingRate'].max()*100:.4f}%  "
          f"abs>0.03%: {(df['fundingRate'].abs() > 0.0003).sum()} events")


if __name__ == "__main__":
    main()
