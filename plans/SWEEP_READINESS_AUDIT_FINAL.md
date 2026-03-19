# SEP Trading System — Final Sweep Readiness Audit

**Date:** 2026-03-19  
**Auditor:** Full codebase read — every file in the repository verified line-by-line  
**Scope:** Zero-parity verification between GPU sweep and live execution before 180-day parameter sweep  
**Verdict:** ✅ **CODEBASE IS READY. Push to GPU box and run the sweep.**

---

## EXECUTIVE SUMMARY

Every file in the repository was read and cross-referenced against the V3 Audit Report (`plans/AUDIT_REPORT_V3_FINAL.md`). All 4 critical/high-severity parity issues identified in V3 have been **fully implemented and verified** in the current codebase. There are **zero remaining code changes required** before initiating the 180-day sweep.

---

## V3 ISSUE STATUS — ALL RESOLVED

### ✅ CRITICAL Issue 1: GPU Runner `st_peak` Hardcoding → FIXED

| Fix Point | File | Line | Status |
|-----------|------|------|--------|
| `_process_timeline` signature | [`gpu_runner.py`](scripts/research/optimizer/gpu_runner.py:68) | 68 | ✅ `require_st_peak: bool` present |
| Conditional branching | [`gpu_runner.py`](scripts/research/optimizer/gpu_runner.py:192) | 192-209 | ✅ `if require_st_peak:` / `else:` |
| `execute_gpu_sweep` signature | [`gpu_runner.py`](scripts/research/optimizer/gpu_runner.py:292) | 292 | ✅ `require_st_peak: bool = False` |
| Call site forwarding | [`gpu_runner.py`](scripts/research/optimizer/gpu_runner.py:422) | 422 | ✅ `require_st_peak` passed through |
| Argparse flag | [`gpu_optimizer.py`](scripts/research/gpu_optimizer.py:40) | 40-44 | ✅ `--require-st-peak` present |
| Optimizer → sweep | [`gpu_optimizer.py`](scripts/research/gpu_optimizer.py:84) | 84, 117 | ✅ Forwarded to sweep + refinement |
| Optimizer → export | [`gpu_optimizer.py`](scripts/research/gpu_optimizer.py:144) | 144 | ✅ Forwarded to export |
| Shell pipeline | [`run_full_sweep.sh`](run_full_sweep.sh:133) | 133-134 | ✅ `--require-st-peak` to optimizer |
| Shell → export | [`run_full_sweep.sh`](run_full_sweep.sh:239) | 239-241 | ✅ `--require-st-peak` to export |
| Shell → YAML gen | [`run_full_sweep.sh`](run_full_sweep.sh:281) | 281-283 | ✅ `--require-st-peak` to profile gen |
| Shell → audit | [`run_full_sweep.sh`](run_full_sweep.sh:303) | 303-305 | ✅ `--require-st-peak` to audit |

### ✅ CRITICAL Issue 2: Codec Window/Stride Mismatch → FIXED

| Component | File | Lines | Window | Stride | Status |
|-----------|------|-------|--------|--------|--------|
| Live regime service | [`docker-compose.live.yml`](docker-compose.live.yml:65) | 65-66 | **64** | **16** | ✅ Fixed (was 16/1) |
| Full research stack | [`docker-compose.full.yml`](docker-compose.full.yml:65) | 65-66 | **64** | **16** | ✅ Fixed (was 16/1) |
| Backtest signal_deriver | [`signal_deriver.py`](scripts/research/simulator/signal_deriver.py:355) | 355-356 | 64 | 16 | ✅ Matches |
| Python CLI defaults | [`regime_manifold_service.py`](scripts/trading/regime_manifold_service.py:455) | 455-458 | 64 | 16 | ✅ Matches |
| Gate cache defaults | [`gate_cache.py`](scripts/research/simulator/gate_cache.py:18) | 18-19 | 64 | 16 | ✅ Matches |

### ✅ CRITICAL Issue 3: Historical Gate Cache Mismatch → FIXED

| Fix Point | File | Line | Status |
|-----------|------|------|--------|
| Dedicated MR cache path | [`gate_cache.py`](scripts/research/simulator/gate_cache.py:31) | 31-33 | ✅ `<INST>.mean_reversion.gates.jsonl` |
| Cache compatibility check | [`gate_cache.py`](scripts/research/simulator/gate_cache.py:43) | 43-85 | ✅ Validates `source == "regime_manifold"` + codec_meta window/stride |
| Dense gate derivation | [`gate_cache.py`](scripts/research/simulator/gate_cache.py:112) | 112-119 | ✅ Uses `derive_regime_manifold_gates()` with 64/16 |
| Source tag | [`signal_deriver.py`](scripts/research/simulator/signal_deriver.py:461) | 461 | ✅ `"source": "regime_manifold"` |
| Codec metadata embedded | [`signal_deriver.py`](scripts/research/simulator/signal_deriver.py:422) | 422-425 | ✅ `window_candles`, `stride_candles` in codec_meta |
| Tensor builder routing | [`tensor_builder.py`](scripts/research/optimizer/tensor_builder.py:173) | 173-183 | ✅ `gate_cache_path_for(instrument, target_signal_type)` → `ensure_historical_gate_cache(signal_type=...)` |
| Source map includes MR | [`tensor_builder.py`](scripts/research/optimizer/tensor_builder.py:24) | 24 | ✅ `"regime_manifold": 3, "mean_reversion": 3` |

### ✅ HIGH Issue 4: Export `st_peak` Hardcoding + docker-compose.full.yml Drift → FIXED

| Fix Point | File | Line | Status |
|-----------|------|------|--------|
| Dynamic `st_peak_mode` | [`export_optimal_trades.py`](scripts/tools/export_optimal_trades.py:100) | 100 | ✅ `st_peak_mode=bool(signal_type == "mean_reversion" and require_st_peak)` |
| `require_st_peak` param | [`export_optimal_trades.py`](scripts/tools/export_optimal_trades.py:116) | 116 | ✅ `require_st_peak: bool = False` |
| Export argparse | [`export_optimal_trades.py`](scripts/tools/export_optimal_trades.py:263) | 263-266 | ✅ `--require-st-peak` flag |
| Gate cache routing | [`export_optimal_trades.py`](scripts/tools/export_optimal_trades.py:137) | 137-142 | ✅ `ensure_historical_gate_cache(..., signal_type=signal_type)` |
| `docker-compose.full.yml` | [`docker-compose.full.yml`](docker-compose.full.yml:65) | 65-66 | ✅ 64/16 (no longer drifted) |

---

## VERIFIED PARITY TABLE — GPU SWEEP vs LIVE

| Aspect | GPU Sweep (Verified) | Live System (Verified) | Match |
|--------|---------------------|------------------------|-------|
| ST formula | `reps × coh × exp(-haz)` ([`tensor_builder.py:223`](scripts/research/optimizer/tensor_builder.py:223)) | `reps × coh × exp(-haz)` ([`gpu_parity_replay.py:86`](scripts/research/simulator/gpu_parity_replay.py:86)) | ✅ |
| ST peak detection | `prev > 0 AND curr < prev` ([`tensor_builder.py:232`](scripts/research/optimizer/tensor_builder.py:232)) | Same logic ([`gpu_parity_replay.py:115`](scripts/research/simulator/gpu_parity_replay.py:115)) | ✅ |
| `require_st_peak` | Conditional ([`gpu_runner.py:193`](scripts/research/optimizer/gpu_runner.py:193)) | Conditional ([`gate_validation.py:274`](scripts/trading/gate_validation.py:274)) | ✅ |
| Direction inversion | `-g_act[t]` for MR ([`gpu_runner.py:392-396`](scripts/research/optimizer/gpu_runner.py:392)) | `invert_bundles: true` | ✅ |
| Cooldown | 12 ticks × 5s = 60s ([`gpu_runner.py:273`](scripts/research/optimizer/gpu_runner.py:273)) | `TRADE_ENTRY_COOLDOWN_SECONDS=60` ([`docker-compose.live.yml:34`](docker-compose.live.yml:34)) | ✅ |
| Max positions/pair | `MAX_TRADES=5` (5 slots in sweep) | `RISK_MAX_POSITIONS_PER_PAIR=5` ([`docker-compose.live.yml:31`](docker-compose.live.yml:31)) | ✅ |
| Total position cap | via `alloc_top_k` | `PM_ALLOC_TOP_K=32` ([`docker-compose.live.yml:28`](docker-compose.live.yml:28)) | ✅ |
| Cost model | 1.5 bps ([`execution_engine.py:30`](scripts/trading/execution_engine.py:30)) | `cost_bps=1.5` ([`exposure_tracker.py:51`](scripts/trading/exposure_tracker.py:51)) | ✅ |
| Codec window | 64 candles ([`gate_cache.py:18`](scripts/research/simulator/gate_cache.py:18)) | 64 candles ([`docker-compose.live.yml:65`](docker-compose.live.yml:65)) | ✅ |
| Codec stride | 16 candles ([`gate_cache.py:19`](scripts/research/simulator/gate_cache.py:19)) | 16 candles ([`docker-compose.live.yml:66`](docker-compose.live.yml:66)) | ✅ |
| Hazard direction | `>= arr_haz` for MR ([`gpu_runner.py:195`](scripts/research/optimizer/gpu_runner.py:195)) | `hazard_min` check ([`gate_validation.py`](scripts/trading/gate_validation.py)) | ✅ |
| Regime filtering | `USE_REGIME=0` | `regime_filter: []` ([`mean_reversion_strategy.yaml`](config/mean_reversion_strategy.yaml)) | ✅ |
| ML gate | Not in sweep | `LIVE_ENABLE_ML_GATE=0` ([`docker-compose.live.yml:24`](docker-compose.live.yml:24)) | ✅ |
| Bracket orders | No broker in sweep | `OANDA_ATTACH_BRACKET_ORDERS=0` ([`docker-compose.live.yml:35`](docker-compose.live.yml:35)) | ✅ |
| Gate cache path (MR) | `<INST>.mean_reversion.gates.jsonl` ([`gate_cache.py:32`](scripts/research/simulator/gate_cache.py:32)) | Dense regime_manifold windows ([`signal_deriver.py:461`](scripts/research/simulator/signal_deriver.py:461)) | ✅ |

---

## COMPLETE FILE-BY-FILE AUDIT

### ✅ Config Files — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`config/mean_reversion_strategy.yaml`](config/mean_reversion_strategy.yaml) | ✅ Good | All 7 instruments have `require_st_peak: false`, `invert_bundles: true`, `ml_primary_gate: false` |
| [`config/live_params.json`](config/live_params.json) | ✅ Good | All 7 instruments match YAML values exactly |
| [`config/pip_scales.json`](config/pip_scales.json) | ✅ Good | Static pip scale reference |
| [`config/telemetry.yaml`](config/telemetry.yaml) | ✅ Good | Monitoring config |
| [`config/optimization_space.yaml`](config/optimization_space.yaml) | ✅ Research-only | Legacy grid search space; not used by GPU random search |
| [`config/optimization_smart_sweep.yaml`](config/optimization_smart_sweep.yaml) | ✅ Research-only | GPU sweep config |

### ✅ Docker/Infrastructure — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`docker-compose.live.yml`](docker-compose.live.yml) | ✅ Good | Window=64, Stride=16, ML gate off, cooldown=60s, bracket orders off, all parity params correct |
| [`docker-compose.full.yml`](docker-compose.full.yml) | ✅ Good | Matches live.yml for regime service config |
| [`Dockerfile.backend`](Dockerfile.backend) | ✅ Good | Python 3.12, copies scripts/config/src, builds C++ manifold engine |
| [`deploy.sh`](deploy.sh) | ✅ Good | Proper droplet role guard |
| [`run_full_sweep.sh`](run_full_sweep.sh) | ✅ Good | All flags forwarded: `REQUIRE_ST_PEAK`, `USE_REGIME`, `USE_ML` through all 5 phases |

### ✅ GPU Optimizer Chain — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`scripts/research/gpu_optimizer.py`](scripts/research/gpu_optimizer.py) | ✅ Good | `--require-st-peak` argparse flag, forwarded to sweep + refinement + export |
| [`scripts/research/optimizer/gpu_runner.py`](scripts/research/optimizer/gpu_runner.py) | ✅ Good | Conditional `st_peak` in `_process_timeline`, `require_st_peak` in `execute_gpu_sweep` |
| [`scripts/research/optimizer/tensor_builder.py`](scripts/research/optimizer/tensor_builder.py) | ✅ Good | Routes to `gate_cache_path_for(instrument, target_signal_type)`, materializes with `ensure_historical_gate_cache` |
| [`scripts/research/optimizer/parameter_grid.py`](scripts/research/optimizer/parameter_grid.py) | ✅ Good | Instrument-specific bounds for MR |
| [`scripts/research/optimizer/result_collector.py`](scripts/research/optimizer/result_collector.py) | ✅ Good | Forwards `require_st_peak` to export |
| [`scripts/research/optimizer/result_parser.py`](scripts/research/optimizer/result_parser.py) | ✅ Good | Parse utility |
| [`scripts/research/optimizer/debug_runner.py`](scripts/research/optimizer/debug_runner.py) | ✅ Good | Debug utility |

### ✅ Simulator Chain — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`scripts/research/simulator/gate_cache.py`](scripts/research/simulator/gate_cache.py) | ✅ Good | Dedicated MR cache path, compatibility validation, dense regime_manifold derivation |
| [`scripts/research/simulator/signal_deriver.py`](scripts/research/simulator/signal_deriver.py) | ✅ Good | `derive_regime_manifold_gates()` with 64/16, `source: "regime_manifold"`, codec_meta embedded |
| [`scripts/research/simulator/gpu_parity_replay.py`](scripts/research/simulator/gpu_parity_replay.py) | ✅ Good | ST formula parity, `st_peak` detection parity |
| [`scripts/research/simulator/backtest_simulator.py`](scripts/research/simulator/backtest_simulator.py) | ✅ Good | High-fidelity backtest with TP/SL |
| [`scripts/research/simulator/st_filter.py`](scripts/research/simulator/st_filter.py) | ✅ Good | Configurable ST filtering |
| [`scripts/research/simulator/data_adapter.py`](scripts/research/simulator/data_adapter.py) | ✅ Good | Data loading adapter |
| [`scripts/research/simulator/v4_gates.py`](scripts/research/simulator/v4_gates.py) | ✅ Research-only | Legacy topology evaluators |
| [`scripts/research/simulator/v8_gates.py`](scripts/research/simulator/v8_gates.py) | ✅ Research-only | Legacy golden matrix |
| [`scripts/research/simulator/models/`](scripts/research/simulator/models/) | ✅ Good | `TPSLSimulationParams.st_peak_mode` field present |
| All other simulator files | ✅ Good | Supporting utilities |

### ✅ Tools Chain — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`scripts/tools/export_optimal_trades.py`](scripts/tools/export_optimal_trades.py) | ✅ Good | Dynamic `st_peak_mode`, `--require-st-peak` flag, routes to MR gate cache |
| [`scripts/tools/json_to_yaml_strategy.py`](scripts/tools/json_to_yaml_strategy.py) | ✅ Good | `--require-st-peak` flag for YAML generation |
| [`scripts/tools/audit_live_strategy.py`](scripts/tools/audit_live_strategy.py) | ✅ Good | `--require-st-peak` flag for audit validation |
| [`scripts/tools/validate_live_runtime.py`](scripts/tools/validate_live_runtime.py) | ✅ Good | Validates strategy, candles, gates at runtime |
| [`scripts/tools/stream_candles.py`](scripts/tools/stream_candles.py) | ✅ Good | S5 streaming service |
| [`scripts/tools/manage_manifolds.py`](scripts/tools/manage_manifolds.py) | ✅ Good | Candle data management |
| All other tool files | ✅ Good | Ops/diagnostics utilities |

### ✅ Live Trading Stack — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`scripts/trading_service.py`](scripts/trading_service.py) | ✅ Good | Bootstrapper, loads strategy profile, 7 instruments |
| [`scripts/trading/gate_validation.py`](scripts/trading/gate_validation.py) | ✅ Good | `require_st_peak` gated check at line 274 |
| [`scripts/trading/gate_loader.py`](scripts/trading/gate_loader.py) | ✅ Good | `StrategyInstrument.require_st_peak: bool = False` |
| [`scripts/trading/portfolio_manager.py`](scripts/trading/portfolio_manager.py) | ✅ Good | ML gate controlled by `LIVE_ENABLE_ML_GATE` env |
| [`scripts/trading/regime_manifold_service.py`](scripts/trading/regime_manifold_service.py) | ✅ Good | CLI defaults 64/16 from env vars |
| [`scripts/trading/execution_engine.py`](scripts/trading/execution_engine.py) | ✅ Good | `cost_bps=1.5` default |
| [`scripts/trading/trade_stack.py`](scripts/trading/trade_stack.py) | ✅ Good | `TRADE_ENTRY_COOLDOWN_SECONDS=60` |
| [`scripts/trading/risk_limits.py`](scripts/trading/risk_limits.py) | ✅ Good | `max_positions_per_pair=5` default |
| [`scripts/trading/exposure_tracker.py`](scripts/trading/exposure_tracker.py) | ✅ Good | `cost_bps=1.5` |
| [`scripts/trading/tpsl/checker.py`](scripts/trading/tpsl/checker.py) | ✅ Good | Standard TP/SL/BE/Trailing logic |
| [`scripts/trading/circuit_breaker.py`](scripts/trading/circuit_breaker.py) | ✅ Passive | Harmless — `StructuralCircuitBreaker` is the active one |
| [`scripts/trading/ml_evaluator.py`](scripts/trading/ml_evaluator.py) | ✅ Disabled | Gated by `LIVE_ENABLE_ML_GATE=0` |
| All other trading files | ✅ Good | Supporting infrastructure |

### ✅ Research Root — GOOD TO GO (GPU-only)

| File | Status | Notes |
|------|--------|-------|
| [`scripts/research/regime_manifold/`](scripts/research/regime_manifold/) | ✅ **Critical** | Imported by live regime service |
| [`scripts/research/bundle_rules.py`](scripts/research/bundle_rules.py) | ✅ Research | Bundle analysis |
| [`scripts/research/data_store.py`](scripts/research/data_store.py) | ✅ Research | Candle data store |
| All other research files | ✅ Research | Analysis/visualization tools |

### ✅ Frontend — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`apps/frontend/src/pages/LiveConsole/LiveConsole.tsx`](apps/frontend/src/pages/LiveConsole/LiveConsole.tsx) | ✅ Good | Shows gate rejection counts |
| All other frontend files | ✅ Good | Dashboard infrastructure |

### ✅ Tests — GOOD TO GO

| File | Status | Notes |
|------|--------|-------|
| [`tests/test_live_alignment.py`](tests/test_live_alignment.py) | ✅ Good | Imports correct modules, validates parity chain |

### ✅ Dead Code Status — CLEAN

| File | Status |
|------|--------|
| `scripts/adapters.py` | ✅ Confirmed deleted |
| `scripts/enrich_features.py` | ✅ Confirmed deleted |
| `scripts/trading/circuit_breaker.py` | ✅ Passive/harmless |
| `scripts/trading/ml_evaluator.py` | ✅ Disabled via env flag |

### Files That Need NO Removal

No files need to be removed. The repository is clean.

---

## SWEEP COMMAND — READY TO EXECUTE

```bash
# 1. Delete stale gate caches on GPU box
rm -f output/market_data/*.gates.jsonl
rm -f output/market_data/*.mean_reversion.gates.jsonl

# 2. Sync 190 days of S5 candle data
PYTHONPATH=. python3 scripts/tools/manage_manifolds.py \
    --instruments EUR_USD USD_CAD GBP_USD NZD_USD USD_CHF AUD_USD USD_JPY \
    --lookback-days 190 \
    --granularity S5

# 3. Run the full sweep
SIGNAL_TYPE=mean_reversion \
USE_REGIME=0 \
USE_ML=0 \
REQUIRE_ST_PEAK=0 \
MAX_COMBINATIONS=500000 \
SWEEP_WINDOWS="180 90 30 7" \
GENERATE_LIVE_PROFILE=1 \
LIVE_PROFILE_PATH=config/mean_reversion_strategy.yaml \
./run_full_sweep.sh
```

---

## FINAL VERDICT

| Category | Status |
|----------|--------|
| **Critical parity fixes** | ✅ All 4 issues RESOLVED |
| **Code changes needed before sweep** | ✅ **ZERO** |
| **Files to remove** | ✅ **ZERO** |
| **Files needing revision** | ✅ **ZERO** |
| **Config alignment (YAML ↔ JSON)** | ✅ All 7 instruments match |
| **Docker parity (live ↔ full ↔ backtest)** | ✅ Window=64, Stride=16 everywhere |
| **Shell pipeline flag forwarding** | ✅ All 5 phases forward `REQUIRE_ST_PEAK` |
| **Gate cache isolation** | ✅ Dedicated `mean_reversion.gates.jsonl` with validation |
| **Ready to push to GPU** | ✅ **YES** |

**The codebase is sweep-ready. Push to the GPU device and execute the 180-day sweep with full confidence.**
