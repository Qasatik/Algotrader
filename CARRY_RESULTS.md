# Funding-Rate Carry — Backtest Results

**Strategy:** delta-neutral funding carry (short perpetual + long spot).
When funding is positive, longs pay shorts → short perp + long spot collects the
funding cash flow with **no directional price risk**.

**Data:** 4,500 funding events for `BTCUSDT`, 2022-06-07 → 2026-07-15 (~4.1 yrs),
3 events/day (00:00 / 08:00 / 16:00 UTC). Mean rate +0.0062%, 148 extreme events
(|rate| > 0.03%).

**Costs (realistic):** perp taker 0.055% + spot taker 0.10% + slippage 0.02% per
leg → **round-trip 0.31%**.

---

## Headline result

| Variant | Total return | CAGR | Sharpe | Max DD | Time in mkt |
|---|---|---|---|---|---|
| **Passive carry** (hold 4 yr) | **+32.0%** | **+6.99%/yr** | 19.3 | -1.3% | 100% |
| Theoretical max (free flips) | +37.2% | +9.06%/yr | — | — | 100% |
| Best timed strategy | +1.3% | +0.32%/yr | 0.72 | -1.0% | 3.7% |

### The verdict

1. **Passive carry works.** Simply holding short-perp + long-spot continuously
   yields **~7%/yr, delta-neutral**, with a max drawdown of only **-1.3%**.
   This is a real, robust, structurally-backed edge (longs pay for leverage).

2. **Timing destroys the edge.** Entering/exiting on extreme funding loses to
   costs: the 0.31% round-trip dwarfs the ~0.01–0.05% collected per event, and
   funding mean-reverts fast. Every timed config under-performs passive carry.
   The theoretical max (perfect foresight, free flips) is only ~5 pts above
   passive over 4 years — there is almost no timing alpha to capture.

3. **Conclusion: don't time it. Run it passively.** The optimal implementation
   is a low-touch delta-neutral position held long-term, rebalanced only to
   keep the hedge aligned.

---

## Parameter sweep (timed strategy)

Best Sharpe among timed configs: entry ≥ 0.05%, max hold 20 events → 11 trades,
+1.3%, Sharpe 0.72. All lower thresholds are negative because costs eat the
small per-event funding. Full grid in `data/BTCUSDT_carry_result.json`.

## Per-year (best timed config)

| Year | Trades | Return | Sharpe |
|---|---|---|---|
| 2022 | 3 | -0.01% | -2.24 |
| 2023 | 1 | +0.00% | 0.48 |
| 2024 | 7 | +0.02% | 2.38 |
| 2025 | 1 | -0.00% | -0.80 |
| 2026 | 0 | 0.00% | 0.00 |

Opportunities cluster in 2024 (the bull-run funding spikes); other years are
near-flat. Passive carry, by contrast, earns steadily every year.

---

## ⚠️ Critical caveats — what the backtest does NOT capture

The +7%/yr passive number is the **funding cash-flow yield** under an idealized
delta-neutral hedge (perp price == spot price at all times). In production:

- **Basis / liquidation tail risk.** During a short squeeze the perpetual can
  spike far above spot. The short-perp leg can be **liquidated** before the spot
  hedge is sold, realising a large loss that does not appear in this backtest.
  This is the dominant real-world risk of funding carry. Mitigations: low
  leverage (≤2–3×), wide maintenance margin, and automated de-risking on basis
  blow-outs.
- **Hedge rebalancing cost.** As price drifts, the perp notional and spot
  notional diverge and must be rebalanced periodically (adds trades/fees). Not
  modelled here; budget ~1–2%/yr in rebalance costs on top of the 0.31% entry.
- **Short-spot leg (rate < 0).** When funding is negative the hedge is a spot
  *short*, which requires borrowing the asset (margin lending) with its own
  borrow cost. BTC funding is almost always positive, so the rate > 0 branch
  (short perp + long spot) dominates and is fully executable on a spot account.
- **Execution.** Fills assumed at mid with fixed slippage; real spreads widen
  in volatility.

Net realistic expectation after rebalancing + tail-risk buffer:
**~4–6%/yr**, delta-neutral — still a genuine, low-correlation yield source.

---

## How this fits the trading plan

- This is a **capital-preservation / yield** strategy, not a growth strategy.
  It complements (does not replace) directional plays.
- For the "double the deposit, save half in BTC" goal: a delta-neutral carry
  sleeve earning ~5%/yr with low drawdown is a reasonable **stable-yield
  component**, while BTC holdings provide the directional upside.
- ✅ Built: a live [`CarryStrategy`](core/carry_strategy.py) with a basis-blowout
  guard (flattens on perp premium > 50 bps), negative-funding exit, hedge-drift
  rebalance, and conservative sizing (≤2× leverage). Run it on **testnet** — see
  the "Live testnet run" section below.

---

## Live testnet run

The live strategy is in [`core/carry_strategy.py`](core/carry_strategy.py) and is
driven by [`scripts/run_carry_testnet.py`](scripts/run_carry_testnet.py). It runs
on the **Bybit demo/testnet** by default (no real money).

**1. Set testnet API keys** in `.env` (get them at https://testnet.bybit.com):
```
BYBIT_API_KEY=...        # testnet key
BYBIT_API_SECRET=...     # testnet secret
PAPER_MODE=true
```

**2. Dry-run first** (decisions only, no orders):
```bash
PYTHONPATH=. python3 scripts/run_carry_testnet.py --dry-run --interval 60
```
You'll see one line per poll: `· [none     ] funding=+0.0100% basis=+2.0bps ...`

**3. Live on testnet** (places real demo orders):
```bash
PYTHONPATH=. python3 scripts/run_carry_testnet.py --interval 300
```
Tune risk: `--leverage 2 --equity-fraction 0.5 --basis-guard-bps 50`.

**What to validate on testnet before going live:**
- Both legs fill (perp short + spot long) and net delta ≈ 0.
- Funding actually settles to the short-perp side every 8h.
- The basis guard triggers and flattens cleanly (test by watching a volatile period).
- Graceful shutdown (`Ctrl+C`) flattens any open position.

---

## Reproduce (backtest)

```bash
PYTHONPATH=. python3 scripts/download_funding.py --symbol BTCUSDT --days 1500
PYTHONPATH=. python3 scripts/backtest_carry.py --symbol BTCUSDT
```

Tests: `PYTHONPATH=. python3 -m pytest tests/ -v` (38 tests, incl. carry + strategy).
