# SEP Trader — Systematic Equilibrium Protocol

> Signal-first algorithmic trading system built on structural manifold analysis of OANDA FX markets.

**Status:** Active development · Mar 2026  
**Stack:** Python 3.12 · C++ (manifold encoder) · Valkey/Redis · React/Vite · Docker Compose · OANDA V20  
**Instruments:** EUR\_USD, GBP\_USD, USD\_JPY, AUD\_USD, USD\_CHF, USD\_CAD, NZD\_USD

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Data Flow](#system-data-flow)
- [Repository Layout](#repository-layout)
- [Quick Start](#quick-start)
- [Docker Services](#docker-services)
- [Configuration](#configuration)
- [Signal-First Workflow](#signal-first-workflow)
- [API Reference](#api-reference)
- [Research & Optimization](#research--optimization)
- [Testing](#testing)
- [Deployment](#deployment)
- [Known Issues & Roadmap](#known-issues--roadmap)
- [Documentation Index](#documentation-index)

---

## Distributed Architecture Overview

SEP Trader uses a **manifold-gate** architecture deployed across two distinct environments:
- **Cloud Droplet**: A headless execution system that runs the Valkey Streams, the PortfolioManager, and the React Frontend to orchestrate actual OANDA orders.
- **Local GPU Node**: A heavy-compute node that executes the 1000+ variant VRAM parameter sweep using `.gates.jsonl` caches. This node periodically pushes the most structurally sound tensor bounds up to the active Droplet via HTTP webhook sync.

The structural metrics (coherence, stability, entropy, hazard) encoded by the C++ manifold_generator produce binary **gate signals** (admit/deny) that the portfolio manager uses.

```
┌──────────────┐     ┌───────────────────┐     ┌─────────────────┐
│ OANDA V20    │────▶│ stream_candles.py  │────▶│  Valkey/Redis   │
│ (Market Data)│     │ (S5 candle fetch)  │     │  candle streams  │
└──────────────┘     └───────────────────┘     └────────┬────────┘
                                                         │
                     ┌───────────────────┐               │
                     │ manifold_generator │◀──────────────┘
                     │ (C++ encoder)      │
                     └────────┬──────────┘
                              │ gate:last:{PAIR}
                              │ gate:index:{PAIR}
                     ┌────────▼──────────┐
                     │     Valkey/Redis   │
                     │   (gate signals)   │
                     └────────┬──────────┘
                              │
                     ┌────────▼──────────┐     ┌──────────────┐
                     │ PortfolioManager   │────▶│  OANDA V20   │
                     │ (Python service)   │     │  (Execution) │
                     └────────┬──────────┘     └──────────────┘
                              │
                     ┌────────▼──────────┐
                     │  HTTP API (:8000)  │
                     │  React Dashboard   │
                     └───────────────────┘
```

### Core Principles

1. **Signal first** — understand the gate stream and correlate with historical outcomes before changing execution parameters.
2. **Directional Mean Reversion** — Structural metrics (coherence, entropy) predict breakout magnitude but are non-directional. SEP Trader dynamically assigns `BUY` or `SELL` vectors to high-tension `mean_revert` regimes inversely based on the trailing 15-minute price action (Fading the Pump/Dump).
3. **Optional P98 Adaptive Machine Learning** — A HistGBM inference layer can evaluate gates with added technical context (RSI, Volatility, `st_peak`), but it is now opt-in for live trading so the default droplet behavior stays aligned with the structural backtest and generated strategy profile.
4. **Independent Trade Stacking** — When evaluating and executing trades, each entry is structurally unique. If a new signal triggers while a trade is already active, the engine natively stacks them, independently tracking concurrent trades with their own localized SL/TP bounds and time expiry limits rather than merging execution state.
5. **Intentionally small** — no rolling evaluator, quant allocator, or research sidecars in the live path; everything between Valkey and OANDA is Python.
6. **Kill switch** — the `ops:kill_switch` key in Valkey halts all live order flow instantly.

---

## System Data Flow

| Stage | Component | Description |
|-------|-----------|-------------|
| **Ingest** | [`scripts/tools/stream_candles.py`](scripts/tools/stream_candles.py) | Polls OANDA for S5 candles per instrument, pushes to Valkey stream |
| **Encode** | `bin/manifold_generator` + [`src/core/`](src/core/) | Native encoder produces structural manifold gates; this checkout tracks the shared core/bindings sources used by the Python extension |
| **Gate Store** | Valkey | `gate:last:{PAIR}` (latest gate JSON), `gate:index:{PAIR}` (sorted set history) |
| **Decision** | [`scripts/trading/portfolio_manager.py`](scripts/trading/portfolio_manager.py) | Evaluates gate signals against strategy profile guards and session windows |
| **Execution** | [`scripts/trading/oanda.py`](scripts/trading/oanda.py) | Places/closes orders via OANDA V20 REST API |
| **Risk** | [`scripts/trading/risk_limits.py`](scripts/trading/risk_limits.py) | Exposure caps, position-count limits, and live admission enforcement |
| **API** | [`scripts/trading/api.py`](scripts/trading/api.py) | HTTP endpoints for health, pricing, account, positions, kill switch |
| **Dashboard** | [`apps/frontend/`](apps/frontend/) | React/Vite SPA surfacing health, gates, and weekly signal analytics |

---

## Repository Layout

``` 
/sep/trader
├── AGENTS.md                          # Agent coding/ops rules (source of truth)
├── README.md                          # ← You are here
├── Makefile                           # Build/dev/lint targets
├── deploy.sh                          # Production deployment script
├── docker-compose.hotband.yml         # Docker Compose stack definition
├── Dockerfile.backend                 # Python 3.12-slim + native extension build
├── requirements.txt                   # Python dependencies
├── .env.template                      # Environment variable reference
├── OANDA.env.template                 # OANDA credential template
│
├── scripts/
│   ├── trading_service.py             # Service bootstrap (main entrypoint)
│   ├── trading/
│   │   ├── portfolio_manager.py       # Gate loading, session policy, trade stack
│   │   ├── risk_limits.py             # Risk sizing and exposure enforcement
│   │   ├── oanda.py                   # OANDA V20 REST client wrapper
│   │   ├── api.py                     # HTTP API handler
│   │   ├── regime_manifold_service.py # Streaming gate writer (regime sidecar)
│   │   ├── tpsl/                      # Take-profit / stop-loss logic
│   │   ├── candle_utils.py            # Candle normalization helpers
│   │   └── guards.py                  # Pre-trade guard checks
│   ├── tools/
│   │   ├── stream_candles.py          # Live OANDA candle streamer → Valkey
│   │   ├── backfill_candles.py        # OANDA → Valkey candle backfill helper
│   │   ├── signal_analytics.py        # CLI signal inspection helpers
│   │   ├── build_manifold_engine.sh   # Native Python extension build helper
│   │   ├── push_config.py             # Promote optimizer winners via webhook
│   │   ├── seed_valkey_defaults.py    # Bootstrap kill switch + NAV keys
│   │   └── health_check_service.py    # Live system monitoring helpers
│   └── research/
│       ├── regime_manifold/           # Python codec over manifold_engine
│       └── simulator/
│           ├── backtest_simulator.py  # Core backtesting engine
│           ├── signal_deriver.py      # Gate → signal derivation
│           └── tracker.py             # Simulated position / trade tracking
│
├── src/                               # C++ native code
│   ├── core/
│   │   ├── byte_stream_manifold.cpp   # Structural byte-stream analysis
│   │   ├── structural_entropy.cpp     # Entropy/coherence/stability metrics
│   │   ├── manifold_builder.cpp       # Manifold construction pipeline
│   │   ├── trading_signals.cpp        # C++ signal generation
│   │   ├── oanda_client.cpp           # C++ OANDA HTTP client
│   │   └── io_utils.cpp               # File I/O helpers
│
├── apps/frontend/                     # React/Vite dashboard
│   ├── src/pages/LiveConsole/         # Main dashboard component
│   ├── src/types/api.d.ts             # OpenAPI-derived type definitions
│   └── public/static/sep.openapi.yaml # API specification
│
├── config/
│   ├── mean_reversion_strategy.yaml   # Active live strategy profile
│   ├── optimization_smart_sweep.yaml  # Smart sweep parameter space
│   ├── optimization_space.yaml        # Full optimization parameter space
│   └── pair_registry.json             # Instrument registry
│
├── tests/
│   ├── conftest.py                    # Pytest path setup
│   ├── trading/                       # Trading path unit tests
│   ├── tools/                         # CLI / promotion workflow tests
│   ├── options_research/              # Research-stack coverage
│   └── sep_historical_gates/          # Historical outcome-study helpers
│
├── ops/
│   ├── cron/                          # Cron job scripts (freshness, backfill)
│   ├── monitoring/                    # Prometheus rules/alerts
│   └── systemd/                       # Systemd service definitions
│
└── docs/                              # System documentation
    ├── 01_System_Architecture.md      # Architecture & concepts
    ├── 03_Operations_and_Analytics.md # Ops procedures
    ├── 04_Strategy_and_Optimization.md # Research/optimizer workflow
    └── 05_GPU_Handoff.md              # GPU system handoff checklist
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- OANDA V20 API credentials (practice or live)
- Node.js 18+ and pnpm (for frontend development only)

### 1. Environment Setup

```bash
# Clone and enter the project
cd sep

# Copy env templates
cp .env.template .env.hotband
cp OANDA.env.template OANDA.env

# Edit OANDA.env with your credentials
# OANDA_API_KEY=your_key
# OANDA_ACCOUNT_ID=your_account
# OANDA_ENVIRONMENT=practice   # or 'live'

# Install Python dependencies
make install
```

### 2. Local Development (Docker)

```bash
# Start core services (Valkey + backend)
docker compose -f docker-compose.hotband.yml up valkey backend

# Add the regime gate encoder when you need fresh gates
docker compose -f docker-compose.hotband.yml up valkey backend regime

# Full stack with frontend, streamer, and regime service
docker compose -f docker-compose.hotband.yml up

# Verify health
curl http://localhost:8000/health
```

### 3. Manual Service Start (No Docker)

```bash
# Start the trading service directly
make start
# or
python3 scripts/trading_service.py

# Build the native manifold extension when working with the regime codec locally
make build-manifold-engine
```

### 4. Frontend Development

```bash
make frontend-install
cd apps/frontend && npm run dev
```

---

## Docker Services

Defined in [`docker-compose.hotband.yml`](docker-compose.hotband.yml):

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| `valkey` | `sep-valkey` | 6379 | Redis 7 Alpine with AOF persistence |
| `backend` | `sep-backend` | 8000 | Trading service + HTTP API |
| `regime` | `sep-regime` | 9105 | Regime manifold gate encoder (Prometheus metrics) |
| `streamer` | `sep-streamer` | — | Candle streaming from OANDA → Valkey |
| `frontend` | `sep-frontend` | 80, 443 | Nginx + React SPA |

### Service Dependencies

```
frontend → backend → valkey
regime → valkey
streamer → valkey
```

---

## Configuration

### Strategy Profile — [`config/mean_reversion_strategy.yaml`](config/mean_reversion_strategy.yaml)

The active strategy profile defines per-instrument parameters:

```yaml
global:
  direction: momentum          # Signal direction interpretation
  min_repetitions: 3           # Gate repetitions before entry
  hazard_max: 0.35             # Maximum hazard for entry
  hazard_exit_threshold: 0.60  # Hazard level triggering exit
  exit_horizon: 40             # Candles to hold before exit
  guard_thresholds:            # Structural metric guards
    min_coherence: 0.0
    min_stability: 0.0
    max_entropy: 4.0
    # ... (10 guard dimensions)

instruments:
  EUR_USD:
    session: { start: "00:00Z", end: "23:59Z" }
    hazard_max: 0.6
    min_repetitions: 1
    exit: { exit_horizon: 40, max_hold_minutes: 40 }
```

### Environment Variables — [`.env.template`](.env.template)

Key variables:
- `OANDA_API_KEY` / `OANDA_ACCOUNT_ID` — broker credentials
- `VALKEY_URL` — Redis connection string (default `redis://valkey:6379/0`)
- `HOTBAND_PAIRS` — comma-separated instrument list
- `LIVE_TRADING_ENABLED` — master enable for live orders
- `KILL_SWITCH` — halt all trading (also in Valkey as `ops:kill_switch`)
- `PORTFOLIO_RECONCILE_SECONDS` — optional interval (seconds) for auto `ExposureTracker` reconcile; defaults to `0` (disabled) to preserve backtest parity. Set only when you explicitly need unattended broker syncs.

### Valkey Key Schema

| Key Pattern | Type | Description |
|-------------|------|-------------|
| `gate:last:{PAIR}` | String (JSON) | Latest gate payload for instrument |
| `gate:index:{PAIR}` | Sorted Set | Historical gate payloads indexed by `ts_ms` |
| `candle:{PAIR}:S5` | Stream | Raw S5 candle stream |
| `ops:kill_switch` | String | `0` (normal) or `1` (halt) |
| `ops:nav_snapshot` | String | Latest NAV value |

---

## Signal-First Workflow

This is the operational discipline for the system. All execution parameter changes must follow this sequence:

### Step 1 — Validate Gate Freshness

```bash
python3 -m scripts.tools.health_check_service \
    --instruments EUR_USD GBP_USD USD_JPY
```

### Step 2 — Backfill Historical Data

```bash
python3 -m scripts.research.data_store \
    --instruments EUR_USD GBP_USD \
    --lookback-days 14
```

### Step 3 — Snapshot Gate Analytics

```bash
python3 scripts/tools/signal_analytics.py \
    --profile config/mean_reversion_strategy.yaml \
    --lookback-minutes 1440 \
    --json > docs/evidence/signal_analytics_latest.json
```

### Step 4 — Review in Dashboard

The weekly signal analytics panel in the frontend dashboard visualizes the study output. Archive every snapshot under `docs/evidence/`.

### Step 5 — Only Then Adjust Parameters

After establishing signal confidence, use the live-aligned GPU optimizer:

```bash
python3 scripts/research/gpu_optimizer.py \
    --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
    --signal-type mean_reversion \
    --lookback-days 180 \
    --max_combinations 5000 \
    --refine \
    --export-trades
```

---

## API Reference

The backend exposes a JSON HTTP API on port 8000. Full spec at [`apps/frontend/public/static/sep.openapi.yaml`](apps/frontend/public/static/sep.openapi.yaml).

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |
| GET | `/api/health` | Detailed health status |
| GET | `/api/pricing` | Live bid/ask pricing |
| GET | `/api/oanda/account` | OANDA account summary |
| GET | `/api/oanda/positions` | Open positions |
| GET | `/api/oanda/open-trades` | Active trades |
| GET | `/api/kill-switch` | Kill switch state |
| POST | `/api/kill-switch` | Toggle kill switch |
| GET | `/api/coherence/status` | Latest coherence metrics |
| GET | `/api/metrics/nav` | NAV time series |
| GET | `/api/backtests/status` | Backtest run status |
| GET | `/api/backtests/latest` | Latest backtest results |

---

## Research & Optimization

### Global Base-Pair S5 Data Backfill

Before running multi-month optimizations, ensure a dense, S5-level dataset is fully locally cached with their respective structural embeddings (`.signatures.jsonl`):

```bash
python3 -m scripts.research.data_store \
    --instruments EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
    --lookback-days 180
```

### GPU Structural Optimizer

The primary research tool for massively parallel parameter exploration runs natively on VRAM using PyTorch. It can evaluate millions of bounds analytically across multiple instruments in seconds:

```bash
python3 scripts/research/gpu_optimizer.py \
    --instrument EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
    --signal-type mean_reversion \
    --lookback-days 180 \
    --max_combinations 5000
```

*Note: The optimizer natively parses S5 candle data and derives necessary metric gates (`.gates.jsonl`) locally prior to allocation into PyTorch CUDA tensors.*

### Distributed Droplet Sync
After discovering strong bounds on the Local GPU Node, configurations can be uploaded to the Cloud Droplet via webhook:
```bash
python3 scripts/tools/push_config.py \
    --payload output/live_params.json \
    --signal-type mean_reversion \
    --target https://droplet-ip/api/strategy/update
```


### Signal Deriver

The [`signal_deriver.py`](scripts/research/simulator/signal_deriver.py) translates raw gate records into actionable signals using the same logic the live system uses, ensuring backtest/live parity.

---

## Testing

```bash
# Syntax check across all modules
make lint

# Run all tests
python3 -m pytest tests/ -v

# Run specific test suites
python3 -m pytest tests/trading/test_trading_service.py -v
python3 -m pytest tests/trading/test_risk_limits.py -v
python3 -m pytest tests/tools/test_push_config.py -v
```

Coverage exists across `tests/trading/`, `tests/tools/`, `tests/options_research/`, and `tests/sep_historical_gates/`. The main remaining gap is end-to-end coverage for the live gate → portfolio manager → order-planning path.

---

## Deployment

### Production Deploy

```bash
# Full deploy on the droplet only
./deploy.sh
```

The deploy script:
1. Refuses to run unless `SEP_NODE_ROLE=droplet` or an explicit override is set
2. Loads `.env.hotband` + `OANDA.env` credentials
3. Validates OANDA credentials are present
4. Stops existing containers, pulls images, rebuilds
5. Starts all services via Docker Compose
6. Seeds Valkey defaults (kill switch, NAV snapshot)
7. Retries health checks (5 attempts, 10s intervals)

Research nodes should keep `SEP_NODE_ROLE=gpu`. Promotion from the GPU box is limited to artifacts and webhook sync via [`scripts/tools/push_config.py`](scripts/tools/push_config.py), not `./deploy.sh`.

### Systemd Services

Located in [`ops/systemd/`](ops/systemd/):
- `sep-backfill.service` — periodic candle backfill
- `sep-data-downloader.service` — live candle streaming
- `sep-manifold.service` — native manifold gate writer

### Monitoring

- **Prometheus** rules at [`ops/monitoring/`](ops/monitoring/)
- **Grafana** dashboard at [`dashboards/grafana.json`](dashboards/grafana.json)
- **Cron jobs** at [`ops/cron/`](ops/cron/) — gate freshness checks, Slack alerts

---

## Known Issues & Roadmap

### Current Gaps

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 1 | **No CI/CD pipeline** — no GitHub Actions or automated checks on push/PR | High | Root |
| 2 | **No end-to-end execution-path integration test** — the live gate → portfolio manager → order-planning flow still lacks dedicated coverage | High | [`tests/trading/`](tests/trading/) |
| 3 | **Evidence archival is still manual** — weekly analytics and optimizer artifacts are not auto-snapshotted with a strategy fingerprint and git SHA | Medium | [`docs/evidence/`](docs/evidence/) + [`scripts/tools/`](scripts/tools/) |

### Roadmap

- [ ] Add CI pipeline (lint + targeted pytest on push)
- [ ] Add integration test suite for the gate → portfolio manager path
- [ ] Add structured logging correlation IDs across services
- [ ] Implement automated weekly evidence archival

---

## Documentation Index

| Document | Description |
|----------|-------------|
| [`README.md`](README.md) | This file — comprehensive system overview |
| [`AGENTS.md`](AGENTS.md) | Agent coding rules and operational expectations |
| [`docs/01_System_Architecture.md`](docs/01_System_Architecture.md) | Architecture deep-dive and component map |
| [`docs/03_Operations_and_Analytics.md`](docs/03_Operations_and_Analytics.md) | Day-to-day operations procedures |
| [`docs/04_Strategy_and_Optimization.md`](docs/04_Strategy_and_Optimization.md) | Strategy profile and optimizer workflow |
| [`docs/05_GPU_Handoff.md`](docs/05_GPU_Handoff.md) | GPU migration and parity checklist |
| [`apps/frontend/public/static/sep.openapi.yaml`](apps/frontend/public/static/sep.openapi.yaml) | OpenAPI specification |
| [`config/mean_reversion_strategy.yaml`](config/mean_reversion_strategy.yaml) | Active strategy profile reference |

---

## License

Private / Proprietary — not for redistribution.
