# GPU Canonical Sweep Workflow

## Purpose

Run the canonical mean-reversion research flow on the GPU box with one pinned
end time:

1. sweep `180` days to find the canonical winner
2. replay/export `180`, `90`, `30`, and `7` days using that same canonical
   params file
3. promote the canonical winner into `output/live_params.json` and
   `config/live_params.json`

## Parity Rule

Mean-reversion historical gate caches are now rebuilt as rolling
`64`-candle windows with `stride=1`.

That matches the current live regime service, which evaluates the latest
`64`-candle manifold on every completed `S5` candle.

## Preflight

Install deps and build the native manifold engine if the GPU box is fresh:

```bash
make install
make build-manifold-engine
```

Refresh candle history with enough headroom for the `180`-day canonical window:

```bash
python3 scripts/research/data_store.py \
  --instruments EUR_USD GBP_USD USD_JPY AUD_USD USD_CHF USD_CAD NZD_USD \
  --lookback-days 190
```

Recommended cleanup before a fresh rerun:

```bash
rm -f output/market_data/*.mean_reversion.gates.jsonl
rm -rf output/180day output/90day output/30day output/7day
rm -f output/live_params.json
```

## Canonical Sweep Command

Pin one UTC end time and use it for the sweep and all validation exports:

```bash
export RUN_END_TIME="$(python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).replace(microsecond=0).isoformat())
PY
)"
```

Run the canonical workflow:

```bash
MAX_COMBINATIONS=15000 \
MIN_TRADES=100 \
MAX_TRADES=300 \
REFINE_SWEEP=1 \
SIGNAL_TYPE=mean_reversion \
./run_full_sweep.sh
```

Default canonical behavior is:

- sweep `180` only
- export `180`, `90`, `30`, `7` from the `180`-day winner
- copy the canonical params into each validation window directory
- promote `output/180day/live_params.json` to `output/live_params.json`
- regenerate `config/mean_reversion_strategy.yaml`
- regenerate `config/live_params.json`

## Re-Export Without Re-Sweeping

If the canonical `180`-day params already exist and you only want to replay the
window exports again:

```bash
RUN_END_TIME="$RUN_END_TIME" \
EXPORT_ONLY=1 \
CANONICAL_PARAMS_PATH=output/180day/live_params.json \
./run_full_sweep.sh
```

## Verification

Audit the promoted live profile:

```bash
PYTHONPATH=. python3 scripts/tools/audit_live_strategy.py \
  --params-path output/live_params.json \
  --strategy-path config/mean_reversion_strategy.yaml \
  --signal-type mean_reversion
```

Audit overlap across the canonical and validation windows:

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

Run the regression checks that cover parity-sensitive research/live alignment:

```bash
PYTHONPATH=. pytest -q tests/test_live_alignment.py
```

## Artifacts To Copy Forward

The files that should exist after a successful canonical rerun are:

- `output/180day/live_params.json`
- `output/90day/live_params.json`
- `output/30day/live_params.json`
- `output/7day/live_params.json`
- `output/live_params.json`
- `config/mean_reversion_strategy.yaml`
- `config/live_params.json`
