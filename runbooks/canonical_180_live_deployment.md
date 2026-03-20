# Canonical 180-Day Live Deployment

## Purpose

Deploy the canonical 180-day mean-reversion profile with gross-notional sizing
that:

- keeps historical peak overlap inside the 70-80% NAV target band
- keeps the configured hard cap below 100% NAV
- does not block historically observed trade opportunities

Research prerequisite:

- build the canonical artifacts with [gpu_canonical_sweep.md](/sep/tpsl/runbooks/gpu_canonical_sweep.md)
- the expected mean-reversion history now comes from rolling `64x1` manifold
  gates, matching the current live service cadence

## Canonical Inputs

- Canonical params: `output/180day/live_params.json`
- Canonical live profile: `config/mean_reversion_strategy.yaml`
- Promoted live params: `config/live_params.json`
- Validation windows: `output/90day`, `output/30day`, `output/7day`

## Required Live Sizing Policy

The audited deployment policy is:

- `EXPOSURE_SCALE=1.0`
- `PORTFOLIO_NAV_RISK_PCT=0.0275`
- `PM_MAX_PER_POS_PCT=0.0275`
- `PM_ALLOC_TOP_K=32`
- `RISK_MAX_TOTAL_POSITIONS=32`
- `RISK_MAX_POSITIONS_PER_PAIR=5`

Interpretation:

- Each trade targets `2.75%` of account NAV in gross notional.
- At the audited `27`-trade historical peak overlap, gross utilization is `74.25%` NAV.
- At the configured `32`-trade ceiling, hard-cap utilization is `88.00%` NAV.
- Per-pair stacking remains `5`, which matches the observed historical maximum.

## Required Checks

1. Confirm the canonical profile is promoted:

```bash
test -f output/180day/live_params.json
test -f output/live_params.json
test -f config/live_params.json
```

2. Audit the live strategy payload:

```bash
PYTHONPATH=. python3 scripts/tools/audit_live_strategy.py \
  --params-path output/live_params.json \
  --strategy-path config/mean_reversion_strategy.yaml \
  --signal-type mean_reversion
```

3. Audit cross-instrument overlap from exported trades:

```bash
python3 scripts/tools/audit_portfolio_overlap.py \
  --window-dir output/180day \
  --window-dir output/90day \
  --window-dir output/30day \
  --window-dir output/7day \
  --nav 100000 \
  --exposure-scale 1.0 \
  --alloc-top-k 32 \
  --projected-gross-pct 2.75
```

Expected results:

- `180day` max concurrent trades: `27`
- `90day` max concurrent trades: `27`
- `30day` max concurrent trades: `24`
- `7day` max concurrent trades: `17`
- projected utilization at `2.75%` gross per trade:
  - `180day`: `74.25%`
  - `90day`: `74.25%`
  - `30day`: `66.00%`
  - `7day`: `46.75%`
  - hard cap at `32` total positions: `88.00%`

4. Run the regression tests that cover the audit math and live parity:

```bash
PYTHONPATH=. pytest -q tests/test_live_alignment.py
```

## Deployment Guard

Do not deploy if any of the following is false:

- `audit_live_strategy.py` returns `OK`
- overlap audit deviates materially from the expected maxima above
- compose defaults do not match the required live sizing policy
- tests fail

## Deployment Command

After the checks above pass:

```bash
docker compose -f docker-compose.live.yml up -d --build
```

## Post-Deploy Verification

- Check the live risk summary telemetry for current NAV and exposure.
- Verify that open gross exposure remains below the configured `88%` NAV hard cap.
- Verify that no entry is rejected due to `max_total_positions_reached` or
  `max_stack_limit_reached` during ordinary operation.
