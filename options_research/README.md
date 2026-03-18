## Options Research Package

`options_research/` is an isolated research package for equity and ETF options backtesting.
It is intentionally separate from the existing SEP FX live stack under `scripts/` and does
not import or modify OANDA, live execution, or deployment code.

### Design Goals

- Keep architecture isolated and testable.
- Reuse the signal-first philosophy from SEP: generate a directional view from the
  underlying first, then express it through a defined-risk options structure.
- Use historical option quotes as the source of truth for fills and marks.
- Make interfaces explicit so later implementations can swap in SEP regime outputs,
  richer data adapters, and walk-forward optimization without refactoring the package.

### Package Boundaries

- `models.py`: canonical domain models shared across the package.
- `signals/`: underlying-driven signal interfaces and a placeholder generator.
- `signals/`: SEP regime adapter plus a placeholder MA baseline for smoke tests only.
- `data/`: historical options dataset interfaces, normalized schema helpers, and a
  local file adapter reference implementation.
- `selection/`: contract filtering and spread selection interfaces.
- `strategies/`: mapping from directional signal to options expression intent.
- `execution/`: package-level fill simulation interfaces and fill-policy config.
- `portfolio/`: portfolio state and risk boundary interfaces.
- `backtest/`: single-run and walk-forward runner interfaces.
- `reporting/`: machine-readable metric and reporting interfaces.

### Current Scope

This package now includes concrete local file loading for:

- underlying bars
- option chain rows grouped into point-in-time snapshots
- corporate action metadata

The package now includes concrete execution simulation and portfolio accounting for
defined-risk vertical spreads, plus rolling walk-forward evaluation and promotion checks.

### Current Selection Policy

The v1 selector now implements a concrete vertical debit spread policy:

- signal direction chooses option side:
  - bullish -> calls
  - bearish -> puts
- strict delta-only mode is supported so fallback dependence can be measured explicitly
- expiry is chosen from the configured DTE band by proximity to the preferred DTE
- the long leg must pass liquidity and spread filters and fall within the configured
  absolute delta band
- when vendor delta is unavailable, the long leg can fall back to the closest liquid
  OTM contract so the backtest can still run on quote-driven historical chains
- the short leg must be on the correct strike side of the long leg and is chosen by:
  - configured spread width when present
  - otherwise by delta offset from the long leg
- candidate legs are rejected when the resulting net debit is non-positive or greater
  than or equal to the vertical width

### Current Fill And Portfolio Policy

- open and close fills are package-aware and quote-driven
- `price_reference="mid"` applies a configurable half-spread penalty from the midpoint
- `price_reference="natural"` uses ask for buys and bid for sells
- fills are rejected when any leg price falls outside the quoted market or when the
  resulting package economics are impossible, such as a leg fill outside the quoted market
- debit-to-close exits are allowed when the quoted package requires paying to flatten risk
- mark-to-market uses current package value from the latest chain snapshot and floors
  that value at zero to respect defined-risk economics
- exit decisions are mark-based and currently evaluated in this order:
  - expiry
  - forced exit timestamp
  - stop loss
  - profit target
  - time stop
  - signal reversal
- portfolio risk limits are applied on max-loss dollars derived from the intent:
  `intent.max_loss * multiplier * contracts`
- current portfolio metadata includes realized and unrealized PnL, max-loss exposure by
  underlying, and aggregate Greeks when all required leg Greeks are present

### Current Signal Path

- the default research path is now the SEP regime adapter in
  `options_research/signals/sep_regime.py`, which reads archived historical gate records
  from local `json` or `jsonl` files instead of recomputing simulator gates inside the
  package
- the moving-average generator remains available only as a smoke-test baseline via an
  explicit CLI switch
- the single-run backtest now defaults to `signal_activation_policy="next_snapshot"` so
  daily-close information cannot fill either the same option snapshot or the same
  trading-day option snapshot that produced the signal

### Market Data Ingestion

The package now includes a Market Data client and local dataset builder in:

- `options_research/data/marketdata.py`
- `options_research/tools/ingest_marketdata.py`

Local options-specific environment variables are loaded from `options_research/.env`.
The committed template is `options_research/.env.template`.
Use `MARKETDATA_TOKEN` for auth and `MARKETDATA_DATA_ROOT` to control the local cache root.
The CLI also supports `--use-url-token` when a bearer header is not desired.

Example:

```bash
python -m options_research.tools.ingest_marketdata \
  --symbol SPY \
  --start 2026-03-02T00:00:00+00:00 \
  --end 2026-03-06T23:59:00+00:00 \
  --max-option-days 1
```

Current Market Data constraints confirmed live on March 9, 2026:

- unauthenticated `AAPL` stock candles and historical option chains work in demo mode
- unauthenticated `SPY` historical option chain requests return `401` with
  `Invalid token header. No credentials provided.`
- the options adapter writes normalized historical chain rows from bid/ask source data
  and does not synthesize option prices
- the current token returns real bid/ask, volume, OI, and spot for `SPY`, but Greeks and
  IV are missing in the historical chain payload, so the selector uses the explicit
  closest-liquid-OTM fallback when delta-based targeting is unavailable
- ingestion defaults now capture `1-60` DTE chains so positions opened in the trading
  band can still be marked and closed after they age
- a corporate-actions endpoint is not wired yet, so the dataset builder currently emits
  an explicit empty placeholder file for that dataset

### Backtest CLI

The package now includes a repeatable local backtest CLI in:

- `options_research/tools/run_backtest.py`

Example:

```bash
python -m options_research.tools.run_backtest \
  --underlying SPY \
  --start 2026-01-20T00:00:00+00:00 \
  --end 2026-03-06T23:59:00+00:00 \
  --data-root data/options_research/marketdata \
  --report-path data/options_research/results/spy_backtest_20260120_20260306.json
```

This writes a machine-readable JSON report with metrics, closed positions, equity curve,
and config metadata. The CLI defaults to the SEP signal adapter, safe next-snapshot
activation, and allows `--signal-source ma` only as an explicit baseline path.

### Walk-Forward CLI

The package now includes a rolling walk-forward CLI in:

- `options_research/tools/run_walk_forward.py`

Example:

```bash
python -m options_research.tools.run_walk_forward \
  --underlyings SPY QQQ IWM \
  --start 2025-01-01T00:00:00+00:00 \
  --end 2026-03-01T00:00:00+00:00 \
  --train-days 180 \
  --test-days 60 \
  --step-days 30
```

This reports out-of-sample metrics only and evaluates promotion using configurable
minimum trade count, PF, Sharpe, stability, and fallback-dependence thresholds.

### Alpha Vantage Ingestion

The package now includes an Alpha Vantage client and local dataset builder in:

- `options_research/data/alpha_vantage.py`
- `options_research/tools/ingest_alpha_vantage.py`

Local options-specific environment variables are loaded from `options_research/.env`.
The committed template is `options_research/.env.template`.

Example:

```bash
python -m options_research.tools.ingest_alpha_vantage \
  --symbol SPY \
  --start 2026-03-02T00:00:00+00:00 \
  --end 2026-03-06T00:00:00+00:00 \
  --skip-options \
  --skip-actions
```

Current Alpha Vantage constraints confirmed live on March 9, 2026:

- `TIME_SERIES_DAILY` works on the free key with `outputsize=compact`
- `TIME_SERIES_DAILY` with `outputsize=full` is premium-only
- `HISTORICAL_OPTIONS` is premium-only

That means the current Alpha Vantage integration is immediately usable for recent
underlying-bar ingestion, but not for a real historical options backtest unless the key
is upgraded or the options dataset comes from another vendor.

### Normalized Local Dataset Contract

Supported file formats are `parquet`, `csv`, `json`, and `jsonl`.

Expected underlying-bar columns:

- required: `timestamp`, `open`, `high`, `low`, `close`, `volume`
- optional: `adjusted_close`
- accepted aliases include short OHLCV names like `o`, `h`, `l`, `c`, `v`

Expected option-chain row columns:

- required: `timestamp`, `contract_symbol`, `expiry`, `strike`, `option_type`, `bid`,
  `ask`, `underlying_spot`
- optional: `last`, `implied_volatility`, `delta`, `gamma`, `theta`, `vega`, `volume`,
  `open_interest`, `multiplier`, `underlying`
- accepted aliases include `right` or `type` for option side, `iv` for implied
  volatility, `oi` for open interest, and `spot` for underlying price

Expected corporate-actions columns:

- required: `ex_date`, `action_type`
- optional: `value`, `description`, `symbol`

The local adapter defaults to point-in-time snapshot lookup using the latest chain
timestamp at or before the requested timestamp.

### Initial Assumptions

- v1 targets `SPY` only.
- v1 strategies are defined-risk vertical debit spreads only:
  - long call debit spread for bullish signals
  - long put debit spread for bearish signals
- Datetimes are expected to be timezone-aware.
- Package prices are modeled in standard option premium terms, with a default multiplier
  of 100 shares per contract unless the dataset states otherwise.
