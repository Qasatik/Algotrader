# Walk-Forward Validation Results

**Symbol:** BTCUSDT 5m · **Data:** 2022-06 → 2026-07 (4 years) · **Method:** rolling 12-month train / 6-month out-of-sample test, fresh model per window, 4 epochs.

## Verdict: ❌ Strategy is NOT robustly profitable

Every out-of-sample regime lost money. The earlier single-window +8.6% result was
overfitting to one favorable recent period — exactly the trap walk-forward exposes.

### Per-regime results (confidence threshold 0.50)

| Test period (regime) | Test acc | Trades | Return | Sharpe |
|---|---|---|---|---|
| 2023-06 → 2023-12 | 0.54 | 278 | **−53.2%** | −5.7 |
| 2023-12 → 2024-06 | 0.45 | 343 | **−47.8%** | −4.3 |
| 2024-06 → 2024-12 | 0.46 | 323 | **−44.4%** | −3.9 |
| 2024-12 → 2025-06 | 0.50 | 113 | **−35.6%** | −4.5 |
| 2025-06 → 2025-12 | 0.45 | 188 | **−38.1%** | −4.5 |
| 2025-12 → 2026-06 | 0.46 | 135 | **−1.5%** | −0.1 |

Test accuracy (~0.45–0.54) sits at the FLAT-class baseline (54%) → the GRU is not
learning a usable directional edge from price-only features at this timeframe.

## What this means
- The model + strategy combination does **not** survive out-of-sample, multi-regime testing.
- This is the **normal** outcome in algorithmic trading — most ideas fail here.
- The infrastructure (data → train → walk-forward → honest metrics) is correct and working;
  the **strategy itself** needs fundamentally better signal.
