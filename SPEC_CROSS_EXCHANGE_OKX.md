# SPEC: Cross-Exchange Funding Arb (Bybit + OKX)

Status: **Phase 2 — design only, NOT scheduled for implementation until Phase 0
metrics are green** (see PLAN_GROWTH_MONETIZATION.md → Roadmap gates).

---

## 1. Idea

Single-exchange carry (short perp + long spot on Bybit) collects ONE funding
stream and pays spot/perp spread + 4 legs of fees. A cross-exchange arb
collects funding on **both** sides with **zero spot leg**:

```
Bybit:  short PERP  ← receives funding when Bybit funding > 0
OKX:    long  PERP  ← receives funding when OKX funding < 0 (longs get paid)
```

Delta nets to ~0 (same underlying, both legs linear USDT perps). Profit per
8h slot = `funding_bybit + |funding_okx|` when the signs are opposite, minus
borrowing/margin costs. Historically sign-opposite funding appears on tail
alts during volatility (one exchange's perp goes premium while the other's
discounts), printing 0.1–0.5%/8h on BOTH legs.

### Why now (and why not yet)

- Phase 0 (EV rotation + maker execution + universe expansion) must first
  prove the engine picks opportunities profitably on ONE exchange.
- Cross-exchange adds a new failure class: **transfer/rebalance latency**
  between venues and TWO API surfaces to harden.

## 2. Entry math (the gate)

Enter only when ALL hold:

```
EV_slot = f_bybit + |f_okx|            # both streams, per 8h, in bps
costs   = entry_slippage_bps + exit_slippage_bps + borrow_bps_per_slot
EV_slot ≥ min_ev_xchg (default 10 bps)  # 3× stricter than single-exchange
|basis_bybit − basis_okx| ≤ max_basis_div_bps (default 30)  # legs track
```

- Sizing: `notional = min(free_bybit, free_okx) × equity_fraction`, capped by
  the THINNER leg's 24h turnover × 0.1% (exit liquidity).
- Both legs perps → maker execution reuses Phase 0 machinery 1:1
  (post-only at touch, market top-up after timeout).

## 3. Architecture

```
core/exchange_okx.py        # OKX v5 REST wrapper, same facade shape as
                            # BybitExchange: funding, tickers, orders, positions
core/xarb_strategy.py       # XArbStrategy: decide()/execute() mirroring
                            # CarryStrategy (state machine, EV gate, timing)
scripts/run_xarb.py         # runner: scan both venues' funding tables,
                            # rank by combined EV, manage top-N pairs
config: OKX_API_KEY/SECRET/PASSPHRASE in .env (never in git)
```

Reuse directly from Phase 0: `_fmt_step` lot rounding, maker poll/cancel/
top-up flow, funding-slot timing (OKX slots also 00/08/16 UTC), EV-gated
entry, exit-confirm anti-churn, cooldown on failed legs.

### OKX API mapping (v5)

| Need | Bybit (have) | OKX endpoint |
|---|---|---|
| Funding | `/v5/market/tickers` fundingRate | `/api/v5/public/funding-rate` |
| Next slot | ticker `nextFundingTime` | `funding-time` endpoint |
| Prices | `get_tickers` | `/api/v5/market/tickers?instId=SWAP-...-USDT` |
| Order | `place_order` | `/api/v5/trade/order` (clOrdId idempotency) |
| Positions | `get_positions` | `/api/v5/account/positions` |
| Lot rules | `get_instruments_info` | `/api/v5/public/instruments?instType=SWAP` (lotSz, minSz, ctVal — **OKX qty is in contracts**: `qty = base / ctVal`) |

Key OKX gotchas: contract-value sizing (ctVal per contract), passphrase on
auth, `tdMode: cross|isolated` mandatory on orders, demo trading flag for
testnet (`x-simulated-trading: 1`).

## 4. Risk controls (hard requirements)

1. **Leg-failure rollback** — if one leg rejects/partially fills and the
   other filled, immediately market-close the filled leg (reuse the
   open-rollback pattern). An unhedged single-venue perp is NOT acceptable.
2. **Divergence guard** — if `|basis_bybit − basis_okx| > max_basis_div_bps`
   while open, close both (the pair is decoupling; one venue's perp is
   detaching from index — often precedes forced-move/liquidation cascades).
3. **Per-venue margin monitor** — keep utilization < 60% per venue; the
   strategy must refuse new pairs when either venue is above the cap.
4. **Withdrawal-disabled API keys on BOTH venues** (existing audit extends
   to OKX).
5. **Clock/latency budget** — legs placed ≤ 2 s apart; wider ⇒ abort entry.

## 5. Rollout plan

| Stage | Content | Gate to next |
|---|---|---|
| X0 | `exchange_okx.py` + public-data funding scanner (no keys) | 2 weeks of logged sign-opposite slots, ≥ 5 events with EV ≥ 10 bps |
| X1 | Paper runner (both venues, no orders) | Paper PnL positive over 50 slots |
| X2 | LIVE with `--max-notional 20` (1 pair max) | 30 days, no leg-failure incident, realized ≥ model |
| X3 | Scale: top-3 pairs, notional up | — |

## 6. Explicit non-goals (Phase 2+)

- No spot legs, no triangular routing, no auto-transfer between venues
  (manual rebalance only), no third exchange.
