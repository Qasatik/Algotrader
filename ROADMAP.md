# 📈 Roadmap — improving the bot & increasing profit

Priorities: **P0** = do first (biggest profit impact), **P1** = high value, **P2** = polish/scale.
Each item lists **Effort** (S/M/L) and the **Profit lever** it targets.

> ⚠️ The current model is a *dummy* (`scripts/bootstrap_model.py`). Nothing here matters
> until **[M1]** is done — a trained, validated model is the single biggest profit driver.

---

## 🧠 1. Model & ML  (biggest edge)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| M1 | **Training pipeline**: collect historical klines → label next-N return → train/val/test split → save checkpoint. Without this you have no real edge. | **P0** | L | Core alpha |
| M2 | **Backtesting engine** (vectorized + event-driven) with realistic fees, slippage, funding. Validate before any live trade. | **P0** | L | Avoid losing money |
| M3 | **Better labels**: triple-barrier method (up/down/neutral by threshold × horizon) instead of naive next-candle. | **P1** | M | Signal quality |
| M4 | **Richer features**: funding rate, open interest, liquidations, cross-asset returns (ETH, DXY), volume profile, order-flow imbalance. | **P1** | M | Predictive power |
| M5 | **Architecture upgrade**: Transformer / Temporal Fusion Transformer instead of GRU; or ensemble GRU + XGBoost. | **P1** | M | Accuracy |
| M6 | **Walk-forward optimization** + **purged k-fold CV** to avoid lookahead/overfit. | **P1** | M | Robustness |
| M7 | **Hyperparameter tuning** (Optuna) on seq_len, hidden, threshold, stop %. | **P2** | M | Edge tuning |
| M8 | **Regime detection** (HMM / clustering) → switch strategy between trend & range markets. | **P2** | L | Adaptivity |
| M9 | **Online retraining**: nightly retrain on fresh data; **MLflow** model registry + versioning. | **P2** | L | Stay current |

---

## 🎯 2. Strategy  (more & better signals)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| S1 | **Multi-strategy framework**: run ML + mean-reversion + momentum in parallel, combine via voting/ensemble. | **P1** | M | Diversified edge |
| S2 | **Funding-rate arbitrage**: spot/perp basis or perp/perp funding harvest (near-market-neutral). | **P1** | M | Steady yield |
| S3 | **Adaptive SL/TP**: ATR-based stops + **trailing stop** to let winners run. | **P1** | S | Risk/reward |
| S4 | **Multi-timeframe confirmation**: 1m signal confirmed by 5m/15m trend. | **P1** | S | Fewer false signals |
| S5 | **Market-making** pass: post limit orders both sides around mid, capture spread + maker rebates. | **P2** | L | Fee income |
| S6 | **Confidence-weighted sizing**: scale size with model confidence (not binary threshold). | **P2** | S | Capital efficiency |

---

## ⚡ 3. Execution  (keep more of each trade)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| E1 | **Maker (limit) orders** instead of market — Bybit pays negative fees (rebate) to makers. Biggest easy win on costs. | **P0** | M | Lower fees |
| E2 | **Smart entry**: join best bid/ask, reprice on top-of-book changes; fall back to market after timeout. | **P1** | M | Less slippage |
| E3 | **TWAP/VWAP** slicing for larger sizes to reduce market impact. | **P2** | M | Fill quality |
| E4 | **Order-book imbalance execution**: only cross when book supports your direction. | **P1** | S | Better fills |
| E5 | **Latency budget logging**: measure signal→order→fill; optimize hot path. | **P2** | S | Speed |

---

## 🛡️ 4. Risk management  (protect capital → compounding)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| R1 | **Max daily drawdown circuit breaker** (e.g. −3%/day → auto-stop). | **P0** | S | Survival |
| R2 | **Consecutive-loss kill switch** (N losses in a row → pause + Telegram alert). | **P0** | S | Tilt control |
| R3 | **Kelly / fractional-Kelly position sizing** from backtested win-rate & payoff. | **P1** | M | Optimal growth |
| R4 | **Volatility targeting**: scale exposure inversely to recent volatility. | **P1** | M | Smoother equity |
| R5 | **Correlation-aware** multi-symbol sizing (don't double-count BTC/ETH risk). | **P2** | M | True diversification |
| R6 | **VaR / exposure caps** per symbol and portfolio-wide. | **P2** | M | Tail risk |

---

## 📊 5. Data & portfolio  (more instruments, better data)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| D1 | **Historical data pipeline**: bulk-download klines/trades → **Parquet** (or ClickHouse) for fast backtests. | **P0** | M | Backtest fuel |
| D2 | **Multi-symbol trading**: trade a basket (BTC, ETH, SOL…) to spread risk and raise opportunity count. | **P1** | M | More trades |
| D3 | **L2 order-book + trade-tape** storage for microstructure features & realistic backtests. | **P2** | L | Micro alpha |
| D4 | **Alternative data**: social sentiment, on-chain flows, macro (rates/DXY). | **P2** | L | Extra signal |

---

## 🔭 6. Monitoring & ops  (catch problems fast)

| ID | Task | Priority | Effort | Profit lever |
|----|------|----------|--------|--------------|
| O1 | **Trade log DB** (PostgreSQL/ClickHouse): every fill, PnL, fees for analytics. | **P1** | M | Attribution |
| O2 | **Grafana dashboards**: equity curve, win-rate, Sharpe, max DD, latency, fees. | **P1** | S | Visibility |
| O3 | **Telegram alerts**: fill, SL/TP hit, drawdown breach, model drift, WS disconnect. | **P1** | S | Fast reaction |
| O4 | **Daily performance report** (auto-posted to Telegram each UTC midnight). | **P2** | S | Accountability |
| O5 | **Health watchdog**: separate process restarts the bot if `/metrics` is stale. | **P2** | S | Uptime |

---

## ✅ Suggested execution order (first 2 weeks)

1. **M1 + D1** — training pipeline + historical data → get a real model.
2. **M2** — backtesting engine → prove the edge exists (or doesn't) before risking capital.
3. **E1** — switch to maker limit orders → instantly cut/earn fees.
4. **R1 + R2** — daily drawdown + consecutive-loss breakers → survive.
5. **S3** — trailing/ATR stops → improve reward:risk.
6. **O1 + O3** — trade DB + Telegram alerts → see what's happening.
7. **D2** — go multi-symbol → more opportunities, smoother equity.

> **Reality check:** no model = no edge. Spend 80% of effort on **M1/M2/D1** (data + training + backtesting). Execution and risk tweaks multiply an existing edge — they don't create one.
