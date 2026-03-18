# Operations & Analytics Runbook

This document covers the day-to-day operations, emergency procedures, and signal analytics workflow for the live SEP trading droplet.

## 1. Day-to-Day Operations

### Gate Freshness
Before any system operations, ensure the C++ engine is successfully deriving gates:
```bash
# Validate service health and gate recency from the live streams
python3 -m scripts.tools.health_check_service --instruments EUR_USD GBP_USD USD_JPY
```

### Health Monitoring
The live React/Vite dashboard provides a real-time HTTP health monitor (OANDA API, Valkey memory limits, Gate staleness) pulled from `health_check_service.py`.

## 2. Signal-First Analytics Workflow

No execution changes or backtests should be trusted without first establishing ground-truth signal confidence.

1. **Backfill Candle History**: Rebuild short-range Valkey candle history if the live feed was interrupted.
   ```bash
   python3 scripts/tools/backfill_candles.py --lookback-hours 24 --granularity M1
   ```
2. **Inspect the Gate Stream**: Export a current analytics snapshot from the active Valkey gate stream.
   ```bash
   python3 scripts/tools/signal_analytics.py \
     --profile config/mean_reversion_strategy.yaml \
     --lookback-minutes 1440 \
     --json > docs/evidence/signal_analytics_latest.json
   ```
3. **Review**: Surface the results in the dashboard and compare admit-rate / reject-reason drift before changing execution logic.
4. **Archive**: Keep JSON snapshots under `docs/evidence/` so GPU research can be compared against the same gate regime later.

## 3. Emergency Procedures

### Master Kill Switch
Toggle the hard block in Valkey to immediately halt all new entries and initiate active position flattening.
```bash
valkey-cli set ops:kill_switch 1
```

### Automated Circuit Breakers
`scripts/trading/circuit_breaker.py` continuously evaluates equity drawdowns according to `config/live_account_risk_limits.py`. If a hard daily loss threshold is breached (e.g., `DAILY_LOSS_PCT = 0.05`), the circuit breaker forcefully engages the `ops:kill_switch` natively without human intervention.

## 4. Deployment

Deployments synchronize the backend Python image, the React Frontend static bundle (served via Nginx), and the C++ structural engine. 

```bash
# Full deployment via docker compose rebuild
./deploy.sh
```
`deploy.sh` is droplet-only and now refuses hosts unless `SEP_NODE_ROLE=droplet`. Keep `SEP_NODE_ROLE=gpu` on the research box.

Systemd manages process persistence for the independent ingest (`sep-data-downloader.service`) and the C++ bridge (`sep-manifold.service`).

### Strategy Promotion Contract

For the current hotband docker deployment, the persistent live strategy source of truth is the repo checkout plus backend restart:

1. Regenerate `config/mean_reversion_strategy.yaml` from `output/live_params.json`.
2. Audit the YAML against the JSON artifact.
3. Commit the updated repo state.
4. Pull the repo on the droplet.
5. Run `./deploy.sh` so the backend restarts against the pulled repo state and reloads `STRATEGY_PROFILE=/app/config/mean_reversion_strategy.yaml`.

`/api/strategy/update` is not the authoritative persistent path under a read-only config mount. Use the webhook only when the strategy file is intentionally writable at runtime.

### Exact Mirroring Checklist

To confirm the droplet is about to run the exact profiled strategy and not a drifted checkout:

1. On the GPU node, commit the deployment state and run `make strategy-fingerprint`.
2. Push the repo.
3. On the droplet, pull the repo and run `make strategy-fingerprint`.
4. Confirm the commit SHA and the `config/mean_reversion_strategy.yaml` SHA256 hash match.
5. Run `./deploy.sh`.

The backend runtime for the aligned March 17, 2026 deployment candidate is:

```bash
STRATEGY_PROFILE=/app/config/mean_reversion_strategy.yaml
LIVE_ENABLE_ML_GATE=0
EXPOSURE_SCALE=0.02
PORTFOLIO_NAV_RISK_PCT=0.02
PM_MAX_PER_POS_PCT=0.02
PM_ALLOC_TOP_K=35
RISK_MAX_TOTAL_POSITIONS=35
RISK_MAX_POSITIONS_PER_PAIR=5
```

Post-deploy, verify the running container:

```bash
docker compose -f docker-compose.hotband.yml exec -T backend python3 - <<'PY'
import os
from scripts.trading.gate_loader import StrategyProfile

for key in [
    "STRATEGY_PROFILE",
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
print(sorted(profile.instruments))
PY
```
