# Strategy Config & GPU Optimization

The SEP Trading System eschews hardcoded entry variables in favor of JSON/YAML configuration maps. This separates parameter grid searches from pipeline architecture.

## 1. Configuration Hierarchy

### Live Configuration
1. **`config/mean_reversion_strategy.yaml`**: The primary operational map. It is generated from `output/live_params.json` via `scripts/tools/json_to_yaml_strategy.py` and should be parity-checked with `scripts/tools/audit_live_strategy.py` before promotion.
2. **`config/bundle_strategy.yaml`**: Evaluates `coherence_slope`, `minimum_semantic_hits`, and deciles to either "promote" or "quarantine" structural signatures via `bundle_rules.py`.
3. **`config/live_account_risk_limits.py`**: Hardcoded Python constants serving as the bottom-tier kill switch (evaluated by the circuit breakers).

### Offline Search Ranges
1. **`config/optimization_space.yaml`**: Dictates the grid search boundaries for the Local GPU Node (`coherence_minimums`, `hazard_ranges`, `repetition_steps`).

## 2. Massively Parallel GPU Optimizer

The GPU optimizer evaluates hundreds of thousands of structural constraint configurations per second. Rather than iterating sequentially through time and simulating each bar like `TPSLBacktestSimulator`, this engine evaluates logical signals simultaneously across all time steps via tensor masking operations on the local GPU.

### Component Flow
1. **`gpu_runner.py`**: The orchestration payload. Bootstraps PyTorch, sets multiprocessing contexts, and executes the combinatorial bounds.
2. **`tensor_builder.py`**: Converts raw dense TSV history (Direction, Coherence, Entropy) into continuous float32 `torch.Tensor` objects.
3. **`result_parser.py`**: Uses massive bitwise masking (`AND` operations) to compute the intersection of signal validity vectors against forward returns (TP/SL). Returns groups of the highest-yielding parameter intersections.

### Updating Matrix Boundaries
To insert new bounds (e.g., minimum temporal half-life):
1. Add the column ingestion step in `tensor_builder.py`.
2. Map the boundary arrays inside `generate_combos` of `gpu_runner.py`.
3. Chain the boolean slice natively via torch dot products in the primary eval loop.

The output of the optimizer is the params artifact (`output/live_params.json`). Convert it to the live YAML with `scripts/tools/json_to_yaml_strategy.py`, audit it with `scripts/tools/audit_live_strategy.py`, then promote it.

Important promotion rules:

1. `--use-regime` is now explicit in both the YAML generator and the audit. If the sweep did not use regime filtering, the generated YAML must keep `regime_filter: []`.
2. The authoritative persistent deployment path is repo-based: regenerate YAML, commit it, pull it on the droplet, then restart the backend.
3. `scripts/tools/push_config.py` can update a running service, but it is only a persistent promotion path when the live strategy file is writable at runtime.

## 3. March 17 2026 Audit

The detailed evidence pack for the March 17, 2026 seven-pair mean-reversion sweep lives in `docs/evidence/2026-03-17_mean_reversion_gpu_sweep_audit.md`.

That audit covers two stages:

1. The initial March 17 sweep exposed a real optimizer/live parity bug in `repetitions` and `st_peak` handling and should not be used for deployment.
2. After aligning `tensor_builder.py`, `gpu_parity_replay.py`, the YAML/audit path, and the runtime mapping path, the aligned rerun became the deployment candidate.

Current conclusions from the aligned rerun:

1. The repo-based YAML generation, audit flow, optimizer tensor semantics, replay/export path, and backend strategy loading path are now internally consistent for the profiled no-regime run.
2. The aligned winner set remained profitable across all 7 pairs after the bug fix, with `AUD_USD`, `USD_CHF`, `GBP_USD`, and `USD_JPY` as the strongest performers.
3. The persistent promotion path is repo push, droplet pull, and `./deploy.sh`, not webhook-only runtime mutation.
4. The deployment evidence should be treated as valid for this exact repo state and exact runtime knob set, not as a generic guarantee for future research runs.
