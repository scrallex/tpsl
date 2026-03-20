# Live Deployment Alignment Audit

**Date:** 2026-03-19  
**Scope:** Canonical 180-day mean-reversion deployment readiness, with explicit
audit of live sizing, overlap, and entry-cap alignment  
**Canonical params:** `output/180day/live_params.json`  
**Validation windows:** `output/90day`, `output/30day`, `output/7day`

## Executive Verdict

### Before this audit

The strategy logic was parity-aligned, but the **live sizing semantics were not
aligned with the deployment requirement** "gross open notionals should stay
comfortably inside account NAV."

Under the prior live compose defaults:

- `EXPOSURE_SCALE=0.02`
- `PORTFOLIO_NAV_RISK_PCT=0.01`
- `PM_MAX_PER_POS_PCT=0.01`
- `PM_ALLOC_TOP_K=32`

the live stack budgeted approximately:

- `50.00% NAV` gross notional **per trade**
- `1600.00% NAV` gross notional **at the configured 32-trade ceiling**

That setup is only conservative if interpreted as a **margin-style** policy
(`2%` margin rate), not as a **gross-notional** policy.

### After this audit

The deployment policy was shifted to a direct gross-notional interpretation:

- `EXPOSURE_SCALE=1.0`
- `PORTFOLIO_NAV_RISK_PCT=0.0275`
- `PM_MAX_PER_POS_PCT=0.0275`
- `PM_ALLOC_TOP_K=32`
- `RISK_MAX_TOTAL_POSITIONS=32`
- `RISK_MAX_POSITIONS_PER_PAIR=5`

This audited policy yields:

- `2.75% NAV` gross notional per trade
- `74.25% NAV` at the audited `27`-trade historical peak
- `88.00% NAV` at the configured `32`-trade hard ceiling
- no historical entry blockage from total-position or per-pair caps

## Proven Code-Path Alignment

### 1. Live sizing math

`RiskSizer` builds scalar caps from NAV and converts them into gross-notional
limits through `exposure_scale`.

- `scripts/trading/risk_calculator.py:32-62`

This means the live interpretation of NAV usage depends entirely on the tuple:

- `PORTFOLIO_NAV_RISK_PCT`
- `PM_MAX_PER_POS_PCT`
- `PM_ALLOC_TOP_K`
- `EXPOSURE_SCALE`

### 2. Live dynamic limits

The live portfolio loop computes dynamic gross-notional limits from NAV every
cycle and pushes them into the risk manager.

- `scripts/trading/portfolio_manager.py:70-72`
- `scripts/trading/portfolio_manager.py:351-367`

Specifically:

- per-trade scalar cap comes from `caps.per_position_cap`
- total gross cap comes from `notional_caps.portfolio_cap`
- per-pair gross cap is `notional_caps.per_position_cap * max_positions_per_pair`

### 3. Live entry caps

The actual live entry blocks are enforced by the trade planner:

- `scripts/trading/trade_planner.py:111-129`

Historically observed entries will be blocked only if one of these caps is hit:

- `max_positions_per_pair`
- `max_total_positions`
- duplicate same-signal suppression
- opposite-side conflict

### 4. Export / replay sizing parity

The high-fidelity export path resolves the same three sizing knobs from the
environment and passes them into the GPU-parity replay:

- `scripts/tools/export_optimal_trades.py:123-159`
- `scripts/research/simulator/gpu_parity_replay.py:221-229`
- `scripts/research/simulator/gpu_parity_replay.py:366-378`

This confirms that the exported validation traces and the live stack are using
the same sizing model, even though the historical exports themselves are
per-instrument replays rather than a native multi-asset portfolio simulator.

## Overlap Audit Results

The finalized exports were audited with:

```bash
python3 scripts/tools/audit_portfolio_overlap.py \
  --window-dir output/180day \
  --window-dir output/90day \
  --window-dir output/30day \
  --window-dir output/7day \
  --nav 100000 \
  --exposure-scale 0.02 \
  --alloc-top-k 32 \
  --projected-gross-pct 2.75
```

Observed historical overlap:

| Window | Total Trades | Max Concurrent | Peak Gross @ Old Config | Peak Margin-Style Usage @ Old Config |
| --- | ---: | ---: | ---: | ---: |
| 180day | 1805 | 27 | 1349.99% NAV | 27.00% NAV |
| 90day | 835 | 27 | 1349.99% NAV | 27.00% NAV |
| 30day | 258 | 24 | 1199.99% NAV | 24.00% NAV |
| 7day | 59 | 17 | 849.99% NAV | 17.00% NAV |

Observed per-instrument stacking maxima:

| Window | EUR_USD | USD_CAD | GBP_USD | NZD_USD | USD_CHF | AUD_USD | USD_JPY |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 180day | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| 90day | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| 30day | 5 | 5 | 4 | 3 | 5 | 5 | 4 |
| 7day | 3 | 2 | 4 | 3 | 5 | 2 | 4 |

### Interpretation

The live cap `RISK_MAX_POSITIONS_PER_PAIR=5` matches the observed historical
maximum exactly. The live cap `PM_ALLOC_TOP_K=32` sits **above** the observed
historical global maximum of `27`.

Therefore:

- the live stack would not have blocked any historically observed opportunity
  due to `max_total_positions`
- the live stack would not have blocked any historically observed opportunity
  due to `max_positions_per_pair`

## Required Sizing Policy

The observed peak overlap across the canonical and validation windows is `27`
concurrent positions.

### Derived bounds

- Gross `% NAV` per trade to hit `75% NAV` at the observed peak:
  - `75 / 27 = 2.7778%`
- Gross `% NAV` per trade to stay under `100% NAV` even if all `32` live slots fill:
  - `100 / 32 = 3.1250%`

Any gross trade size inside `2.593%` to `2.963%` would keep the observed
`27`-trade peak inside the requested `70-80%` band while also keeping the
configured `32`-trade ceiling below `100% NAV`.

### Chosen policy

This audit rounds slightly down to a cleaner operating value:

- `2.75% NAV` gross notional per trade

Resulting projected utilization:

| Window | Peak Concurrent | Projected Peak Utilization @ 2.75%/trade |
| --- | ---: | ---: |
| 180day | 27 | 74.25% NAV |
| 90day | 27 | 74.25% NAV |
| 30day | 24 | 66.00% NAV |
| 7day | 17 | 46.75% NAV |

Configured hard cap with `32` total positions:

- `32 * 2.75% = 88.00% NAV`

## Repo Changes Made

### 1. Live and full compose defaults updated

`docker-compose.live.yml:25-34` now sets:

- `EXPOSURE_SCALE=1.0`
- `PORTFOLIO_NAV_RISK_PCT=0.0275`
- `PM_MAX_PER_POS_PCT=0.0275`
- `PM_ALLOC_TOP_K=32`
- `RISK_MAX_TOTAL_POSITIONS=32`
- `RISK_MAX_POSITIONS_PER_PAIR=5`

`docker-compose.full.yml` mirrors the same sizing policy for parity-sensitive
replays.

### 2. Reproducible overlap audit tool added

- `scripts/tools/audit_portfolio_overlap.py`

### 3. Operational deployment runbook added

- `runbooks/canonical_180_live_deployment.md`

## Evidence Gate Result

Evidence Gate was started locally and run against the deployment-safety change.

### Initial result

The first Evidence Gate pass escalated because the repo had no:

- supporting tests for the overlap-audit flow
- runbook / operational handling evidence
- explicit deployment audit document

This audit added all three:

- `tests/test_portfolio_overlap_audit.py`
- `runbooks/canonical_180_live_deployment.md`
- `plans/LIVE_DEPLOYMENT_ALIGNMENT_AUDIT_2026-03-19.md`

### Refreshed result after adding tests, docs, and runbook

`change-impact` and strict `action` were both rerun against:

- `docker-compose.live.yml`
- `docker-compose.full.yml`
- `scripts/tools/audit_portfolio_overlap.py`
- `tests/test_portfolio_overlap_audit.py`
- `runbooks/canonical_180_live_deployment.md`
- `plans/LIVE_DEPLOYMENT_ALIGNMENT_AUDIT_2026-03-19.md`
- `plans/SIZING_PRECEDENT_NOTE_2026-03-19.md`

Final strict action result:

- `allowed: false`
- `status: block`
- decision: `escalate`
- remaining missing evidence: **no prior PR or incident precedent was found**

This means the repo now has:

- code evidence
- regression-test evidence
- runbook evidence
- documentation
- a surfaced git-history sizing note

but Evidence Gate still refuses an `admit` because it cannot resolve the change
to a native prior PR or incident twin case.

## Residual Risk

1. The validation exports are still instrument-by-instrument replays, not a
   native multi-asset portfolio simulator.
2. Portfolio-level admission confidence comes from the aggregate overlap audit
   of exported trades plus the live cap math, not from a joint optimizer.
3. There is no prior deployment precedent in the repository history for this
   exact gross-notional sizing policy.

## Deployment Position

### Not acceptable

Do **not** deploy the canonical 180-day profile with the old sizing semantics:

- `EXPOSURE_SCALE=0.02`
- `PORTFOLIO_NAV_RISK_PCT=0.01`
- `PM_MAX_PER_POS_PCT=0.01`

That configuration violates the requested gross-NAV policy by allowing
historical peak gross usage of roughly `1350% NAV`.

### Acceptable target configuration

Deploy only with the patched gross-notional sizing policy and the runbook checks
in `runbooks/canonical_180_live_deployment.md`.

Under that configuration:

- historically observed opportunities fit under the live entry caps
- gross NAV usage remains inside the requested band at the audited peak
- the configured hard cap remains below total NAV
