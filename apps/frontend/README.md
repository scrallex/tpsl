# SEP Live Dashboard

This React/Vite SPA serves as the real-time operational window into the live droplet execution environment. It connects directly to the backend Python API to surface Valkey metrics, streaming gates, active positions, and the weekly evidence baseline.

## Architecture

The frontend is a lightweight monitor mapping directly to backend read-only endpoints. It does not contain strategy execution code or parameter generation math.

- **Vite**: Ultra-fast module bundler.
- **React**: Core UI components.
- **TailwindCSS**: Utilitarian styling.

### Key Components

- `HealthMonitor`: Displays connectivity statuses (OANDA API, Valkey memory limits, Gate staleness) pulled from `health_check_service.py`.
- `GateStream`: The live scrolling window of `MarketManifoldEncoder` outputs.
- `WeeklyOutcomePanel`: Renders the `output/evidence/outcome_weekly_costs.json` artifact to validate live simulation alignment vs. historical backtests.

## Setup Instructions

Ensure Node (`v18+`) and `pnpm` are installed.

```bash
cd apps/frontend
pnpm install
```

*(Alternatively, use `make frontend-install` from the repository root).*

## Running Locally

To start the Vite dev server with hot-module reloading:

```bash
pnpm run dev
```

The frontend will attempt to connect to the backend API defined in `.env.local` (defaulting to `http://localhost:8000`).

## Deployment

The production bundle is generated via `pnpm build`. 

During full system deployments, the script `./deploy.sh` automatically compiles the static assets into `dist/` and serves them via an optimized `nginx` container bridging the Python backend API routes.
