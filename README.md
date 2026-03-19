# SEP Trader

Signal-first FX trading system for OANDA majors.

## Architecture

- `docker-compose.live.yml`: canonical droplet stack with `valkey`, `backend`, `regime`, `frontend`, and `streamer`.
- `docker-compose.full.yml`: compatibility alias for the same stack during local/dev work.
- `scripts/trading_service.py`: live backend entrypoint.
- `scripts/tools/stream_candles.py`: OANDA candle ingest into Valkey.
- `scripts/research/gpu_optimizer.py`: GPU-node optimizer and backtest workflow.
- `config/mean_reversion_strategy.yaml`: live strategy source of truth consumed by the backend.
- `config/live_params.json`: promoted single-signal params snapshot kept in sync with the active live YAML.

Live flow:

```text
OANDA candles -> stream_candles.py -> Valkey -> manifold/regime gates -> PortfolioManager -> OANDA orders
```

## Repo Layout

```text
config/                 Live profiles, promoted params snapshot, runtime config
ops/                    Cron, monitoring, and systemd helpers
scripts/trading/        Live execution path
scripts/tools/          Runtime and promotion utilities
scripts/research/       GPU-node research and optimizer stack
src/core/               Native manifold engine sources
apps/frontend/          Optional dashboard
docker-compose.live.yml Live deployment stack
docker-compose.full.yml Compatibility alias
deploy.sh               Droplet deployment entrypoint
```

## Environment

Create the ignored runtime env files the stack expects:

```bash
cat > .env.hotband <<'EOF'
SEP_NODE_ROLE=gpu
VALKEY_URL=redis://valkey:6379/0
HOTBAND_PAIRS=EUR_USD,USD_JPY,AUD_USD,USD_CHF,NZD_USD,GBP_USD,USD_CAD
EOF

cat > OANDA.env <<'EOF'
OANDA_ACCOUNT_ID=...
OANDA_API_KEY=...
OANDA_ENVIRONMENT=practice
EOF
```

Use `SEP_NODE_ROLE=gpu` on research nodes. `deploy.sh` refuses non-droplet hosts unless `SEP_ALLOW_NON_DROPLET_DEPLOY=1`.

## Local Commands

Install Python dependencies:

```bash
make install
```

Build the native manifold module:

```bash
make build-manifold-engine
```

Run syntax checks for the Python paths that ship in this repo:

```bash
make lint
```

## Docker Stacks

Live stack:

```bash
docker compose -f docker-compose.live.yml up --build
```

Full stack:

```bash
docker compose -f docker-compose.full.yml up --build
```

Health check:

```bash
curl http://localhost:8000/health
```

## Strategy Parity

Strategy parity commands target `output/live_params.json`, the optimizer artifact exported by the sweep. Promoted live params are written to `config/live_params.json`.

Audit the active YAML against the params artifact:

```bash
make strategy-audit
```

Emit the current fingerprint:

```bash
make strategy-fingerprint
```

Run both:

```bash
make parity-check
```

## GPU Workflow

Refresh history:

```bash
python3 scripts/research/data_store.py \
  --instruments EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --lookback-days 180
```

Run the optimizer:

```bash
python3 scripts/research/gpu_optimizer.py \
  --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --signal-type mean_reversion \
  --lookback-days 180 \
  --max_combinations 15000 \
  --refine \
  --export-trades \
  --min-trades 100 \
  --max-trades 300
```

Project optimizer output into the live YAML:

```bash
make strategy-yaml
make parity-check
```

If you want the live profile to keep structural-tension peak gating enabled during promotion:

```bash
make strategy-yaml REQUIRE_ST_PEAK=1
make parity-check REQUIRE_ST_PEAK=1
```

Validate the live runtime after deployment:

```bash
make validate-live-runtime VALIDATION_REDIS_URL=redis://localhost:6379/0
```

Promote the winner over the webhook:

```bash
make push-config TARGET=http://127.0.0.1:8000/api/strategy/update
```

## Deployment

`deploy.sh` now defaults to the canonical live stack, runs `make parity-check` before building containers, and validates that streamed candles and gate payloads are present after the stack comes up.

Droplet deploy:

```bash
SEP_NODE_ROLE=droplet ./deploy.sh
```

Compatibility alias:

```bash
SEP_NODE_ROLE=droplet SEP_DEPLOY_STACK=full ./deploy.sh
```

From the repo root:

```bash
make deploy-live
```

## Evidence Artifacts

Runtime evidence endpoints now read from `output/evidence/` by default:

- `output/evidence/outcome_weekly_costs.json`
- `output/evidence/roc_regime_summary.json`

Backtest API artifacts live under `output/backtests/`.
