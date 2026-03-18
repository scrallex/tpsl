## Historical SEP Gates

`sep_historical_gates/` exports SEP-style gate artifacts from historical intraday
underlying bars without modifying the live FX stack.

Current behavior:

- fetches intraday stock candles from Market Data
- runs the existing SEP manifold encoder over those candles
- applies the SEP-style adaptive hazard threshold, regime filter, and confidence filter
- writes raw gate events as `jsonl` for consumption by `options_research/signals/sep_regime.py`
- can run a machine-readable forward-return study on exported gate files before any
  options expression work

Default assumptions:

- Market Data equity history is minute-based, so the exporter currently uses `1` minute bars
  rather than the live FX `S5` cadence
- the default `hazard_max` is `1.0` to avoid inheriting the FX-only cap from
  `config/mean_reversion_strategy.yaml` for equities
- bundle annotations follow the existing SEP bundle rules when the bundle config is available

Example:

```bash
python -m sep_historical_gates.cli \
  --symbol SPY \
  --start 2026-01-20T14:30:00+00:00 \
  --end 2026-03-06T21:00:00+00:00 \
  --resolution-minutes 1 \
  --output data/options_research/gates/SPY.gates.jsonl
```

Outcome study example:

```bash
python -m sep_historical_gates.study_cli \
  --symbol SPY \
  --gate-path data/options_research/gates/SPY.gates.jsonl \
  --output data/options_research/results/spy_gate_outcome_study.json
```

Daily gate-compression study example:

```bash
python -m sep_historical_gates.compression_cli \
  --symbol SPY \
  --gate-path data/options_research/gates/SPY.gates.jsonl \
  --output data/options_research/results/spy_gate_compression_study.json
```

Compression study assumptions:

- entry price is the same-day regular-session close
- decision rules are `first_admitted`, `strongest_admitted`, `last_admitted`, and
  `majority_direction`
- `confidence` and `bundle_hits` are treated as non-decisioning fields until their
  distributions become materially informative
- the default holding horizons are close-to-close `1d` and `3d`
