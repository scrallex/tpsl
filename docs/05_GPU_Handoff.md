# GPU Handoff

This checklist is the live-aligned path for moving SEP research onto a local GPU system without reintroducing drift between backtest artifacts and the live stack.

The current deployment candidate is the aligned March 17, 2026 seven-pair mean-reversion sweep recorded in `docs/evidence/2026-03-17_mean_reversion_gpu_sweep_audit.md`.

## 1. Canonical Files

- Live strategy YAML: `config/mean_reversion_strategy.yaml`
- Optimizer winner JSON: `output/live_params.json`
- JSON -> YAML projection: `scripts/tools/json_to_yaml_strategy.py`
- Parity audit: `scripts/tools/audit_live_strategy.py`
- High-fidelity trade export: `scripts/tools/export_optimal_trades.py`
- Webhook promotion helper: `scripts/tools/push_config.py`

## 2. Clone + Bootstrap

```bash
git clone <repo> sep
cd sep
make install
export PYTHONPATH=$PWD
```

If you need to fetch fresh OANDA history or run the high-fidelity simulator from cache misses, place valid credentials in `OANDA.env`.

## 3. Match the Live Runtime Knobs

The active live profile is `config/mean_reversion_strategy.yaml`. For runtime sizing parity, copy the effective values from the live droplet configuration before you start a research cycle.

The docker runtime overrides in `docker-compose.hotband.yml` are the live reference point:

```bash
export STRATEGY_PROFILE=config/mean_reversion_strategy.yaml
export LIVE_ENABLE_ML_GATE=0
export EXPOSURE_SCALE=0.02
export PORTFOLIO_NAV_RISK_PCT=0.02
export PM_MAX_PER_POS_PCT=0.02
export PM_ALLOC_TOP_K=35
export RISK_MAX_TOTAL_POSITIONS=35
export RISK_MAX_POSITIONS_PER_PAIR=5
```

`EXPOSURE_SCALE` is now explicit in `docker-compose.hotband.yml`, so the droplet no longer relies on the code default for that knob.

Sizing clarification:

- The selected live deployment uses `2%` NAV per trade via `PORTFOLIO_NAV_RISK_PCT=0.02` and `PM_MAX_PER_POS_PCT=0.02`.
- That is converted into units with `EXPOSURE_SCALE=0.02`; it is not a 2% gross-notional cap.
- `RISK_MAX_POSITIONS_PER_PAIR` is the live per-instrument ticket cap.
- `RISK_MAX_TOTAL_POSITIONS` is the live total-ticket cap across the whole book.
- Those caps are enforced in the planner before order submission.
- This `2% / 35 tickets / 5 per pair` deployment choice was selected after the stacking audit showed that the aligned 7-pair exports peaked at `35` simultaneous trades.

## 4. Refresh Historical Data

```bash
python3 scripts/research/data_store.py \
  --instruments EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --lookback-days 180
```

This builds or extends the `output/market_data/*.jsonl` cache used by the optimizer and the high-fidelity simulator.

## 5. Run the GPU Search

```bash
python3 scripts/research/gpu_optimizer.py \
  --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --signal-type mean_reversion \
  --lookback-days 180 \
  --max_combinations 5000 \
  --refine \
  --export-trades
```

This writes `output/live_params.json`. The optional `--export-trades` pass runs the slower simulator export for the winning params.

## 6. Regenerate + Audit the Live YAML

```bash
make strategy-yaml
make strategy-audit
```

If the GPU sweep used regime filtering, pass `USE_REGIME=1` to both targets. For the aligned March 17, 2026 mean-reversion sweep, do not do that because the sweep was run without `--use-regime`.

Equivalent raw commands:

```bash
python3 scripts/tools/json_to_yaml_strategy.py \
  --params-path output/live_params.json \
  --output-path config/mean_reversion_strategy.yaml \
  --signal-type mean_reversion

python3 scripts/tools/audit_live_strategy.py \
  --params-path output/live_params.json \
  --strategy-path config/mean_reversion_strategy.yaml \
  --signal-type mean_reversion
```

Do not trust a GPU result for live deployment unless the audit passes.

## 7. Run the High-Fidelity Validation Pass

```bash
python3 scripts/tools/export_optimal_trades.py \
  --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --signal-type mean_reversion \
  --lookback-days 90 \
  --profile-path config/mean_reversion_strategy.yaml \
  --exposure-scale "${EXPOSURE_SCALE}" \
  --per-position-pct-cap "${PM_MAX_PER_POS_PCT}"
```

This path now uses the live-aligned scalar exposure sizing model instead of the old hardcoded `exposure_scale=1.0`.

Read `gpu_parity_pnl_bps` from the resulting `*.trades.json` files when comparing against the sweep. The exposure-sized return metrics are expected to differ.

## 8. Promote to the Droplet

For a persistent repo-managed deployment, the authoritative path is:

1. Regenerate `config/mean_reversion_strategy.yaml`.
2. Commit the updated YAML and any supporting code/docs.
3. Pull the repo on the droplet.
4. Run `./deploy.sh` on the droplet so the backend restarts against the pulled repo state.

The GPU node should keep `SEP_NODE_ROLE=gpu`. `deploy.sh` now refuses non-droplet hosts unless `SEP_ALLOW_NON_DROPLET_DEPLOY=1` is set explicitly.

Webhook promotion is only safe as a persistent source of truth when the live strategy path is writable at runtime.

```bash
python3 scripts/tools/push_config.py \
  --payload output/live_params.json \
  --signal-type mean_reversion \
  --target https://<droplet-host>/api/strategy/update
```

For a single instrument:

```bash
python3 scripts/tools/push_config.py \
  --payload output/live_params.json \
  --signal-type mean_reversion \
  --instrument EUR_USD \
  --target https://<droplet-host>/api/strategy/update
```

## 9. Suggested Research Loop

1. Copy the live runtime risk variables from the droplet.
2. Refresh the local S5 cache.
3. Run a broad GPU search.
4. Regenerate and audit the YAML.
5. Run the slower simulator export on winners.
6. Compare trade count, concurrent-position behavior, and holding time against live.
7. Only then promote to the droplet.

## 10. Deployment Checklist

On the GPU node after committing the repo state you plan to deploy:

```bash
make strategy-fingerprint
```

On the droplet after pulling:

```bash
make strategy-fingerprint
```

The commit SHA and `config/mean_reversion_strategy.yaml` hash must match between both machines.

If the fingerprint command prints `WORKTREE_DIRTY`, commit the deployment changes before using that fingerprint as the source of truth.

Then deploy:

```bash
./deploy.sh
```

Verify the running container:

```bash
docker compose -f docker-compose.hotband.yml exec -T backend python3 - <<'PY'
import os
from scripts.trading.gate_loader import StrategyProfile

print(os.getenv("STRATEGY_PROFILE"))
for key in [
    "LIVE_ENABLE_ML_GATE",
    "EXPOSURE_SCALE",
    "PORTFOLIO_NAV_RISK_PCT",
    "PM_MAX_PER_POS_PCT",
    "PM_ALLOC_TOP_K",
    "RISK_MAX_TOTAL_POSITIONS",
    "RISK_MAX_POSITIONS_PER_PAIR",
]:
    print(f"{key}={os.getenv(key)}")

profile = StrategyProfile.load(os.environ["STRATEGY_PROFILE"])
for instrument in sorted(profile.instruments):
    cfg = profile.instruments[instrument]
    print(
        instrument,
        cfg.hazard_min,
        cfg.guards.min_coherence,
        cfg.guards.max_entropy,
        cfg.exit.max_hold_minutes,
    )
PY
```

That is the exact repo-managed promotion path for replacing the current droplet implementation with the aligned mean-reversion profile.
