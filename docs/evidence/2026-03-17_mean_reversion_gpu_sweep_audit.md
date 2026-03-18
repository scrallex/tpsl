# 2026-03-17 Mean Reversion GPU Sweep Audit

This document records the full March 17, 2026 mean-reversion research cycle on the GPU node, including the initial failed-parity sweep, the optimizer/live alignment fixes, the aligned rerun, and the final deployment-readiness audit for the droplet.

The aligned rerun supersedes the earlier March 17 sweep for deployment decisions.

## 1. Sweep Timeline

### Initial sweep

Initial command:

```bash
python3 scripts/research/gpu_optimizer.py \
  --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --signal-type mean_reversion \
  --lookback-days 180 \
  --max_combinations 5000 \
  --refine \
  --export-trades
```

Initial artifact:

- Optimizer log: `output/logs/gpu_optimizer_full_20260317_011206.log`

That run is not the deployment candidate. It exposed a real optimizer/live parity break in `repetitions` and `st_peak` handling and was replaced by the aligned rerun documented below.

### Aligned rerun

Aligned rerun command:

```bash
python3 scripts/research/gpu_optimizer.py \
  --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --signal-type mean_reversion \
  --lookback-days 180 \
  --max_combinations 5000 \
  --refine \
  --export-trades
```

Deployment-candidate artifacts:

- Optimizer log: `output/logs/gpu_optimizer_full_aligned_20260317_054703.log`
- Winner JSON: `output/live_params.json`
- Exported trade traces: `output/market_data/*.trades.json`
- Generated live YAML: `config/mean_reversion_strategy.yaml`

## 2. Runtime Knobs Used

The aligned rerun itself was validated under the then-live droplet sizing profile. Those are the research-time validation knobs, not the later selected deployment override.

GPU-side research knobs:

```bash
export STRATEGY_PROFILE=config/mean_reversion_strategy.yaml
export LIVE_ENABLE_ML_GATE=0
export EXPOSURE_SCALE=0.02
export PORTFOLIO_NAV_RISK_PCT=0.05
export PM_MAX_PER_POS_PCT=0.05
export PM_ALLOC_TOP_K=12
export RISK_MAX_TOTAL_POSITIONS=12
```

Research-time droplet/runtime contract during the aligned rerun:

- `docker-compose.hotband.yml` now sets `STRATEGY_PROFILE=/app/config/mean_reversion_strategy.yaml`
- `LIVE_ENABLE_ML_GATE=0`
- `EXPOSURE_SCALE=0.02`
- `PORTFOLIO_NAV_RISK_PCT=0.05`
- `PM_MAX_PER_POS_PCT=0.05`
- `PM_ALLOC_TOP_K=12`
- `RISK_MAX_TOTAL_POSITIONS=12`

Research-time sizing interpretation:

- The aligned rerun used `5%` NAV per position via `PORTFOLIO_NAV_RISK_PCT=0.05` and `PM_MAX_PER_POS_PCT=0.05`.
- `EXPOSURE_SCALE=0.02` converts that NAV budget into units.
- `PM_ALLOC_TOP_K=12` and `RISK_MAX_TOTAL_POSITIONS=12` preserve the stacked-entry behavior used in research.

## 3. Parity Fixes Completed

The deployment candidate includes all of the following parity fixes.

### Export and YAML parity

1. `export_optimal_trades.py` no longer leaks YAML regime filters into no-regime sweeps.
2. `json_to_yaml_strategy.py` and `audit_live_strategy.py` no longer hardcode regime filters for non-Pacific pairs when the sweep ran without `--use-regime`.
3. Export traces now include `gpu_parity_pnl_bps`, which is the correct apples-to-apples comparison with the GPU sweep.

### Live update and promotion safety

1. `trading_service.py` now maps raw GPU winner keys (`Haz`, `Coh`, `Ent`, `Stab`, `Hold`) into the live profile correctly.
2. `Hold` is preserved in minutes instead of being incorrectly converted.
3. A failed runtime persistence attempt no longer leaves a half-applied in-memory profile.

### Core optimizer/live semantic alignment

1. `tensor_builder.py` now reads gate `repetitions` instead of relying on nonexistent `reps`.
2. `tensor_builder.py` and `gpu_parity_replay.py` now prefer live-style metric extraction from `structure` before falling back to `components`.
3. The optimizer/replay ST-peak path was aligned to the live `PortfolioManager` interpretation.

Direct parity verification before the rerun:

- All 7 instruments were checked across the full 180-day window.
- Result: `0` ST-peak mismatches between the optimizer tensor interpretation and the live-style recompute.

## 4. Aligned Sweep Results

The exported trade traces include both GPU-parity and live-sized outcomes. `gpu_parity_pnl_bps` is the correct figure to compare against the sweep. The live-sized return is expected to be larger because it applies the configured NAV budget and exposure scalar.

| Instrument | GPU parity bps | Live-sized bps | Trades | Win rate | Sharpe | Profit factor | Max drawdown ($) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AUD_USD | 5153.9 | 12884.4 | 388 | 39.9% | 3.26 | 2.48 | 18743.60 |
| USD_CHF | 4425.0 | 11077.2 | 258 | 42.2% | 3.34 | 2.88 | 13857.16 |
| GBP_USD | 3693.1 | 9232.7 | 259 | 50.6% | 3.44 | 2.95 | 17814.17 |
| USD_JPY | 2992.1 | 7472.3 | 222 | 34.7% | 2.85 | 3.10 | 14772.51 |
| NZD_USD | 2375.6 | 5939.2 | 530 | 55.7% | 1.72 | 1.36 | 27321.81 |
| USD_CAD | 1683.1 | 4200.5 | 186 | 49.5% | 2.78 | 2.25 | 8981.56 |
| EUR_USD | 1151.7 | 2879.3 | 259 | 39.8% | 1.77 | 1.61 | 22860.41 |

Important findings:

1. All seven instruments remained profitable after the optimizer was aligned to live semantics.
2. `AUD_USD`, `USD_CHF`, `GBP_USD`, and `USD_JPY` are the strongest aligned performers.
3. `EUR_USD` weakened sharply versus the invalid pre-alignment sweep, which is the expected signature of a real bug fix rather than a cosmetic change.
4. `NZD_USD` is still profitable, but it is the noisiest of the set: highest trade count, weakest profit factor, and worst drawdown.
5. `USD_CAD` remains acceptable but is materially weaker than the top four pairs.

## 5. Exact Mean-Reversion Deployment Profile

These are the aligned mean-reversion winners projected into the live YAML.

| Instrument | Hazard min | Min coherence | Max entropy | Max hold (minutes) | SL | TP | BE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EUR_USD | 0.88476 | 0.16634 | 1.20740 | 1427 | 0.00500 | 0.00870 | 0.00169 |
| GBP_USD | 0.86634 | 0.17164 | 1.09080 | 3512 | 0.00531 | 0.00536 | 0.00243 |
| USD_JPY | 0.82428 | 0.17377 | 1.37586 | 2044 | 0.00668 | 0.00946 | 0.00171 |
| AUD_USD | 0.82046 | 0.17690 | 2.05693 | 3388 | 0.00766 | 0.00662 | 0.00235 |
| USD_CHF | 0.91025 | 0.06478 | 1.35938 | 3266 | 0.00583 | 0.00824 | 0.00250 |
| USD_CAD | 0.87927 | 0.16168 | 1.27293 | 3848 | 0.00202 | 0.00495 | 0.00229 |
| NZD_USD | 0.88961 | 0.14791 | 1.02257 | 3960 | 0.00625 | 0.00329 | 0.00233 |

Shared profile properties:

- `regime_filter: []`
- `min_repetitions: 1`
- `invert_bundles: true`
- `allow_fallback: false`
- `ml_primary_gate: false`
- `exit.exit_horizon: 40`
- `exit.hold_rearm: true`

## 6. Deployment Readiness Audit

### What is now proven

For a repo-based promotion, the deployment path is now internally consistent:

1. `output/live_params.json` projects to `config/mean_reversion_strategy.yaml`.
2. `make strategy-audit` passes against that YAML.
3. The backend reads `/app/config/mean_reversion_strategy.yaml` from the repo-mounted `./config` directory.
4. The backend runtime knobs now explicitly match the profiled sizing contract, including `EXPOSURE_SCALE=0.02`.
5. The optimizer tensor path, the replay/export path, and the live admission math now agree on the gate semantics used to select entries.

Validation completed:

- `make strategy-yaml`
- `make strategy-audit`
- `pytest tests/tools/test_export_optimal_trades.py tests/trading/test_trading_service.py tests/trading/test_env_loader.py`
- direct 180-day ST-peak parity check across all seven instruments

### Stacking and sizing follow-up

Additional deployment validation after the aligned rerun confirmed:

1. The live path supports multiple active trades per instrument and manages them as individual tickets with per-trade TP/SL handling.
2. `TradePlanner` now enforces runtime ticket caps instead of a hardcoded five-ticket limit.
3. `RISK_MAX_POSITIONS_PER_PAIR` is the effective per-instrument ticket cap.
4. `RISK_MAX_TOTAL_POSITIONS` is the effective total-ticket cap across the live book.

Observed 7-pair concurrency from the aligned export traces:

- Total trades: `2102`
- Average hold time: `23.614` hours
- Average trades/day over the full 180-day window: `11.678`
- Derived average concurrent tickets over the full 180-day window: `11.49`
- Average daily peak concurrent tickets: `27.89`
- Exact peak concurrent tickets: `35`

Implications at `5%` NAV per trade:

- Derived average concurrency implies about `57.5%` aggregate allocation.
- Average daily peak implies about `139.5%` aggregate allocation.
- Exact observed peak implies about `175%` aggregate allocation.

Observed per-instrument peak concurrency:

- `AUD_USD`: `5`
- `EUR_USD`: `5`
- `GBP_USD`: `5`
- `NZD_USD`: `5`
- `USD_CAD`: `5`
- `USD_CHF`: `5`
- `USD_JPY`: `5`

An uncapped replay rerun with `RISK_MAX_POSITIONS_PER_PAIR=100`, `RISK_MAX_TOTAL_POSITIONS=100`, and `PM_ALLOC_TOP_K=100` produced identical trade traces, so the aligned per-instrument exports were not being clipped by the previous five-ticket planner default.

Selected live deployment choice after this audit:

- `7` pairs active
- `PORTFOLIO_NAV_RISK_PCT=0.02`
- `PM_MAX_PER_POS_PCT=0.02`
- `PM_ALLOC_TOP_K=35`
- `RISK_MAX_TOTAL_POSITIONS=35`
- `RISK_MAX_POSITIONS_PER_PAIR=5`

That combination preserves all observed trades from the aligned 7-pair export set while keeping the observed absolute peak at `35 * 2% = 70%` aggregate allocation.

### What this means operationally

If this repo state is pushed, pulled on the droplet, and deployed with `./deploy.sh`, the backend will load the exact aligned strategy profile together with the selected live sizing override of `2% / 35 total tickets / 5 per pair`.

The remaining uncertainties are market uncertainties, not repo-config drift:

- new live gates arriving after deployment
- live spreads/slippage versus simulated fills
- normal runtime risk controls such as kill switch and margin limits

## 7. Persistent Promotion Path

Under `docker-compose.hotband.yml`, the backend mounts `./config` read-only.

Implications:

1. The persistent source of truth is the repo checkout.
2. The authoritative rollout path is repo commit, droplet pull, and `./deploy.sh`.
3. `push_config.py` is a runtime override helper, not the durable promotion mechanism under the current compose setup.

## 8. Deterministic Rollout Checklist

On the GPU node after committing the repo state you intend to deploy:

```bash
make strategy-fingerprint
```

On the droplet after pulling but before deploy:

```bash
make strategy-fingerprint
```

The commit SHA and the `config/mean_reversion_strategy.yaml` SHA256 hash must match between the GPU node and the droplet checkout.

If `make strategy-fingerprint` prints `WORKTREE_DIRTY`, do not treat that output as a deployment fingerprint until the changes are committed.

`output/live_params.json` is a research artifact and is not part of the persistent droplet deployment contract under the current repo-driven rollout.

Then deploy:

```bash
./deploy.sh
```

Post-deploy verification:

```bash
docker compose -f docker-compose.hotband.yml exec -T backend python3 - <<'PY'
import os
from scripts.trading.gate_loader import StrategyProfile

keys = [
    "STRATEGY_PROFILE",
    "LIVE_ENABLE_ML_GATE",
    "EXPOSURE_SCALE",
    "PORTFOLIO_NAV_RISK_PCT",
    "PM_MAX_PER_POS_PCT",
    "PM_ALLOC_TOP_K",
    "RISK_MAX_TOTAL_POSITIONS",
    "RISK_MAX_POSITIONS_PER_PAIR",
]
for key in keys:
    print(f"{key}={os.getenv(key)}")

profile = StrategyProfile.load(os.environ["STRATEGY_PROFILE"])
for instrument in ["AUD_USD", "USD_CHF", "GBP_USD", "USD_JPY", "NZD_USD", "USD_CAD", "EUR_USD"]:
    cfg = profile.instruments[instrument]
    print(
        instrument,
        cfg.hazard_min,
        cfg.guards.min_coherence,
        cfg.guards.max_entropy,
        cfg.exit.max_hold_minutes,
        cfg.stop_loss_pct,
        cfg.take_profit_pct,
        cfg.breakeven_trigger_pct,
    )
PY
```

That confirms the container is running the exact aligned profile and sizing contract recorded in this document.
