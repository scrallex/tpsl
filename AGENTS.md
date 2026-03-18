# SEP Codex Guide

This doc is the single source of truth for how the simplified SEP stack now operates. It reflects the signal-first plan: **understand the gate stream, correlate it with history, then (and only then) stage backtests / execution changes.**

## Distributed Architecture Snapshot
The system is divided into two strict execution environments:
1. **Cloud Droplet (Live & UI)**: Hosts the Valkey streams, the `PortfolioManager`, the OANDA connection, and the React Frontend. Responsible for executing live trades and providing a real-time monitor.
2. **Local GPU Node (Compute & Research)**: Runs the massively parallel vector backtests via `scripts/research/gpu_optimizer.py`. This system pushes optimal structural parameter bounds up to the Droplet via the `/api/strategy/update` proxy webhook.

- Flow: external candle ingest (`scripts/tools/stream_candles.py`) → manifold encoder (`bin/manifold_generator`) → Valkey (`gate:last:{instrument}` / `gate:index:{instrument}`) → `PortfolioManager` → OANDA connector.
- Gate evidence in this checkout is inspected through `scripts/tools/signal_analytics.py` and backend health telemetry. Historical outcome-study helpers referenced in older notes are not part of this repo snapshot.
- No rolling evaluator, quant allocator, or research sidecars remain in the cloud path; everything between Valkey and OANDA is Python and intentionally small.

## Module Orientation
| Area | File(s) | Notes |
| --- | --- | --- |
| Service bootstrap | `scripts/trading_service.py`, `scripts/trading/log_formatters.py`, `scripts/trading/evidence_cache.py` | Creates the OANDA connector, `PortfolioManager`, HTTP API, and evidence cache. |
| Portfolio + risk | `scripts/trading/portfolio_manager.py`, `scripts/trading/risk_limits.py`, `scripts/trading/risk_calculator.py` | Gate loading, session policy, exposure limits, and sizing. |
| Signal tooling | `scripts/tools/signal_analytics.py`, `scripts/tools/backfill_candles.py`, `scripts/tools/seed_valkey_defaults.py`, `scripts/tools/push_config.py` | CLI helpers for inspecting the live stream, restoring short-range candle state, and promoting strategy updates. |
| Data / Streaming | `scripts/tools/stream_candles.py`, `scripts/research/data_store.py`, `scripts/trading/candle_parser.py`, `scripts/trading/retry_utils.py` | Live candle streaming, API retry logic, and canonical historical S5 data fetching. |
| Research / Optim | `scripts/research/gpu_optimizer.py`, `scripts/research/optimizer/tensor_builder.py`, `scripts/research/optimizer/result_parser.py` | Primary massively parallel VRAM backtest + optimization engine. |
| Native metrics | `src/core/*`, `bin/manifold_generator` | C++ encoder that feeds the manifold service; rarely touched unless metric math changes. |
| Frontend | `apps/frontend` | React/Vite dashboard that surfaces health, gates, and the weekly signal analytics panel. |
| Config/docs | `config/mean_reversion_strategy.yaml`, `docs/01_System_Architecture.md`, `docs/03_Operations_and_Analytics.md`, `docs/04_Strategy_and_Optimization.md`, `docs/05_GPU_Handoff.md` | Keep these synchronized with code changes. |

## Signal-First Workflow
1. **Validate gate freshness** via `python3 -m scripts.tools.health_check_service --instruments ...`.
2. **Refresh nearby history if needed** via `scripts/tools/backfill_candles.py` or `scripts/research/data_store.py`.
3. **Inspect / archive the gate stream** with `signal_analytics.py --json > docs/evidence/...` before changing execution logic.
4. **Only after signal confidence is established**, design targeted backtests (grid runner lives under `scripts/research/` for that phase) and consider profile/risk changes.

Everything we deploy, document, or visualize should support steps 1‑3 before we touch step 4.

## Development Workflow
- `make install` sets up Python deps; `make frontend-install` for the dashboard.
- Local stack: `docker compose -f docker-compose.hotband.yml up valkey backend` (add `regime` when you need fresh gates).
- **Optimization**: Run the GPU backtests natively on the Local GPU, then use `push_config.py` to sync bounds to the droplet.
- Tests: `make lint`; targeted `pytest` for modules you touch.
- Deploy: `./deploy.sh` is droplet-only. Research nodes should keep `SEP_NODE_ROLE=gpu`; the script refuses non-droplet hosts unless explicitly overridden.

## Coding & Ops Expectations
- Python: Black/flake8 style, 4 spaces, 120 cols max. Add comments only when logic isn’t obvious.
- C++: match existing `src/core` formatting; rebuild via `make build-core` if touched.
- Secrets stay in `OANDA.env` / `.env.hotband`. Never commit real credentials.
- Kill switch lives in `ops:kill_switch`; confirm its state before allowing live orders.
- Monitoring focus: gate freshness, OANDA connectivity, signal evidence generation, and dashboard health.

## MCP Manifold Tool Usage Guidelines

When using the `manifold` MCP server for codebase analysis, follow this pattern for optimal results:

### 1. Repository Indexing
Always start by ingesting the repository with the correct root directory:
```
mcp--manifold--ingest_repo
  root_dir: "."           # Use current workspace directory
  clear_first: true       # Clear previous index to avoid stale data
  compute_chaos: true     # Enable chaos/complexity scoring
  lite: false             # Full analysis including tests and docs
```

### 2. Verify Index State
Check index statistics to confirm successful ingestion:
```
mcp--manifold--get_index_stats
```
Verify the `Last ingest root` matches your expected workspace path.

### 3. Identify High-Complexity Areas
Use batch chaos scanning to prioritize investigation:
```
mcp--manifold--batch_chaos_scan
  pattern: "*.py"         # Target file type
  scope: "*"              # All directories, or narrow to "scripts/*"
  max_files: 30           # Top N highest-risk files
```

**Chaos Score Interpretation:**
- `0.35-0.39`: Normal complexity
- `0.40-0.44`: High complexity (common in algorithms, simulators)
- `0.45+`: Very high complexity (refactoring candidate)

### 4. Search for Code Patterns
Find specific implementations across the codebase:
```
mcp--manifold--search_code
  query: "class PortfolioManager"    # Python regex supported
  file_pattern: "*.py"               # Narrow search scope
  max_results: 10
```

### 5. Deep Dive Analysis
For critical files identified by chaos scan:
```
mcp--manifold--analyze_code_chaos
  path: "scripts/trading/portfolio_manager.py"
```

This returns detailed metrics: coherence, entropy, stability, chaos score, and structural risks.

### 6. Retrieve File Content
After identifying files via search or chaos scan:
```
mcp--manifold--get_file
  path: "scripts/trading/portfolio_manager.py"
```

**Note:** File paths in the index may include relative path prefixes (e.g., `../../trader/`). Use search to discover exact indexed paths first.

### Recommended Workflow for Deep Dives
1. **Index** the repository with `ingest_repo`
2. **Scan** for high-complexity Python (`*.py`) and C++ (`*.cpp`) files
3. **Search** for key architectural patterns (e.g., "class.*Manager", "def.*evaluate")
4. **Read** complete file content with `read_file` tool for detailed analysis
5. **Analyze** chaos metrics only for files where structural understanding is critical

### When NOT to Use MCP Manifold Tools
- Simple file content reads → Use `read_file` directly
- Directory structure inspection → Use `list_files`
- Quick searches in known files → Use `search_files` (grep-style)
- Real-time code changes → MCP index is snapshot-based, reindex after major changes

Keep this file updated whenever modules move or workflow expectations shift; stale guidance here wastes everyone's time.
