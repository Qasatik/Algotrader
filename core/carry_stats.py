"""Aggregate carry trade-log statistics by period (day / month / year).

Reads the persistent CSV produced by :class:`CarryStrategy._log_trade` and
groups events into time buckets so the Telegram bot and CLI can answer
"how much did we make this day / month / year?".
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.carry_strategy import DEFAULT_TRADE_LOG


@dataclass
class PeriodStats:
    """Aggregated stats for one time bucket."""

    label: str  # e.g. "2026-07-15" / "2026-07" / "2026"
    opens: int = 0
    closes: int = 0
    rebalances: int = 0
    avg_funding_pct: float = 0.0  # avg funding rate at entry (%)
    avg_basis_bps: float = 0.0  # avg basis at entry (bps)
    total_qty: float = 0.0  # sum of qty across opens


def _bucket_key(ts_iso: str, period: str) -> str | None:
    """Return a period bucket label, or None if the timestamp is unparseable."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if period == "day":
        return dt.strftime("%Y-%m-%d")
    if period == "month":
        return dt.strftime("%Y-%m")
    if period == "year":
        return dt.strftime("%Y")
    return dt.strftime("%Y-%m-%d")


def load_stats(
    period: str = "day",
    path: str = DEFAULT_TRADE_LOG,
) -> list[PeriodStats]:
    """Read the CSV trade log and return per-bucket stats (oldest first).

    Args:
        period: "day", "month", or "year".
        path: CSV file path.
    """
    p = Path(path)
    if not p.exists():
        return []

    buckets: dict[str, dict] = defaultdict(
        lambda: {"opens": 0, "closes": 0, "rebalances": 0,
                 "funding_sum": 0.0, "funding_n": 0,
                 "basis_sum": 0.0, "basis_n": 0, "qty_sum": 0.0}
    )
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            key = _bucket_key(row.get("timestamp", ""), period)
            if key is None:
                continue
            b = buckets[key]
            action = row.get("action", "")
            if action == "open":
                b["opens"] += 1
                b["qty_sum"] += float(row.get("qty", 0) or 0)
                try:
                    b["funding_sum"] += float(row.get("funding_rate", 0) or 0)
                    b["funding_n"] += 1
                except ValueError:
                    pass
            elif action == "close":
                b["closes"] += 1
            elif action == "rebalance":
                b["rebalances"] += 1
            try:
                b["basis_sum"] += float(row.get("basis_bps", 0) or 0)
                b["basis_n"] += 1
            except ValueError:
                pass

    out: list[PeriodStats] = []
    for label in sorted(buckets):
        b = buckets[label]
        out.append(PeriodStats(
            label=label,
            opens=b["opens"],
            closes=b["closes"],
            rebalances=b["rebalances"],
            avg_funding_pct=(b["funding_sum"] / b["funding_n"] * 100)
            if b["funding_n"] else 0.0,
            avg_basis_bps=(b["basis_sum"] / b["basis_n"]) if b["basis_n"] else 0.0,
            total_qty=b["qty_sum"],
        ))
    return out


def format_stats(period: str, stats: list[PeriodStats]) -> str:
    """Render period stats as a human-readable multi-line string."""
    if not stats:
        return f"No trades logged yet (period: {period})."
    name = {"day": "Daily", "month": "Monthly", "year": "Yearly"}.get(period, period)
    lines = [f"📊 *{name} Carry Stats*\n"]
    lines.append(f"`{'Period':<12} {'Open':>4} {'Close':>5} {'Rebal':>5} "
                 f"{'AvgFund':>8} {'AvgBasis':>8}`")
    for s in stats[-12:]:  # last 12 buckets
        lines.append(
            f"`{s.label:<12} {s.opens:>4} {s.closes:>5} {s.rebalances:>5} "
            f"{s.avg_funding_pct:>+7.4f}% {s.avg_basis_bps:>+6.1f}bps`"
        )
    # totals
    t_open = sum(s.opens for s in stats)
    t_close = sum(s.closes for s in stats)
    lines.append(f"\n*Total:* {t_open} opens, {t_close} closes")
    return "\n".join(lines)
