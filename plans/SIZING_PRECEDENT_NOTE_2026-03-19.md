# Sizing Precedent Note

This repository does contain a prior sizing-related deployment change in git
history:

- Commit: `b89f48ec07dba254a08b84448285c284610e86e0`
- Subject: `align live sizing caps with 180-day overlap parity`
- Date: 2026-03-17

That commit modified:

- `docker-compose.live.yml`
- `docker-compose.full.yml`
- `docker-compose.hotband.yml`
- `config/mean_reversion_strategy.yaml`

The current 2026-03-19 sizing change is not introducing a new risk surface; it
is tightening the interpretation of the existing sizing controls from a
margin-style budget:

- `EXPOSURE_SCALE=0.02`
- `PORTFOLIO_NAV_RISK_PCT=0.01`
- `PM_MAX_PER_POS_PCT=0.01`

to an explicit gross-notional budget:

- `EXPOSURE_SCALE=1.0`
- `PORTFOLIO_NAV_RISK_PCT=0.0275`
- `PM_MAX_PER_POS_PCT=0.0275`

The control surface is unchanged:

- same compose files
- same risk-sizing code path
- same total-position cap (`32`)
- same per-pair cap (`5`)

What changed is the audited interpretation of account utilization so that gross
open notional now remains bounded under the requested live NAV policy.
