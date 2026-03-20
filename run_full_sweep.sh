#!/usr/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Load API Keys so python tools can authenticate with OANDA
if [[ -f "OANDA.env" ]]; then
    set -a; source OANDA.env; set +a
fi
if [[ -f ".env" ]]; then
    set -a; source .env; set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
SIGNAL_TYPE="${SIGNAL_TYPE:-mean_reversion}"
MAX_COMBINATIONS="${MAX_COMBINATIONS:-500000}"
MIN_TRADES="${MIN_TRADES:-10}"
MAX_TRADES="${MAX_TRADES:-300}"
USE_REGIME="${USE_REGIME:-0}"
# Keep the baseline live profile aligned with the structural backtest unless ML is
# explicitly requested for a dedicated export/evaluation pass.
USE_ML="${USE_ML:-0}"
ML_PRIMARY_GATE="${ML_PRIMARY_GATE:-0}"
REQUIRE_ST_PEAK="${REQUIRE_ST_PEAK:-0}"
REBUILD_ML="${REBUILD_ML:-auto}"
REFINE_SWEEP="${REFINE_SWEEP:-0}"
DRY_RUN="${DRY_RUN:-0}"
EXPORT_ONLY="${EXPORT_ONLY:-0}"
VALIDATE_WINDOWS_ONLY="${VALIDATE_WINDOWS_ONLY:-0}"
CANONICAL_WINDOW="${CANONICAL_WINDOW:-180}"
CANONICAL_PARAMS_PATH="${CANONICAL_PARAMS_PATH:-}"
GENERATE_LIVE_PROFILE="${GENERATE_LIVE_PROFILE:-1}"
AUDIT_LIVE_PROFILE="${AUDIT_LIVE_PROFILE:-1}"
LIVE_PROFILE_PATH="${LIVE_PROFILE_PATH:-config/mean_reversion_strategy.yaml}"
CANONICAL_LIVE_PARAMS_PATH="${CANONICAL_LIVE_PARAMS_PATH:-config/live_params.json}"
EXPORT_PARAMS_PATH="${EXPORT_PARAMS_PATH:-}"
EXPORT_END_TIME="${EXPORT_END_TIME:-}"
REFERENCE_EXPORT_ROOT="${REFERENCE_EXPORT_ROOT:-output/LiveParams}"
SWEEP_MODE="${SWEEP_MODE:-canonical}"
VALIDATION_WINDOWS_STR="${VALIDATION_WINDOWS:-90 30 7}"
SWEEP_WINDOWS_STR="${SWEEP_WINDOWS:-180 90 30 7}"
RUN_END_TIME="${RUN_END_TIME:-}"

INSTRUMENTS_STR="${INSTRUMENTS:-EUR_USD USD_CAD GBP_USD NZD_USD USD_CHF AUD_USD USD_JPY}"

read -r -a INSTRUMENTS <<< "$INSTRUMENTS_STR"
read -r -a VALIDATION_WINDOWS <<< "$VALIDATION_WINDOWS_STR"
read -r -a SWEEP_WINDOWS <<< "$SWEEP_WINDOWS_STR"

mkdir -p output/market_data output/ml_data output/models

run_cmd() {
    echo "+ $*"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@"
}

run_logged_cmd() {
    local log_file="$1"
    shift
    echo "+ $* | tee $log_file"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@" 2>&1 | tee "$log_file"
}

resolve_run_end_time() {
    if [[ -n "$RUN_END_TIME" ]]; then
        echo "$RUN_END_TIME"
        return 0
    fi
    "$PYTHON_BIN" - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).replace(microsecond=0).isoformat())
PY
}

resolve_effective_end_time() {
    if [[ -n "$RUN_END_TIME" && -n "$EXPORT_END_TIME" && "$RUN_END_TIME" != "$EXPORT_END_TIME" ]]; then
        echo "RUN_END_TIME and EXPORT_END_TIME must match when both are set." >&2
        exit 1
    fi
    if [[ -n "$RUN_END_TIME" ]]; then
        echo "$RUN_END_TIME"
        return 0
    fi
    if [[ -n "$EXPORT_END_TIME" ]]; then
        echo "$EXPORT_END_TIME"
        return 0
    fi
    resolve_run_end_time
}

unique_windows() {
    local value
    local -A seen=()
    for value in "$@"; do
        if [[ -z "$value" ]]; then
            continue
        fi
        if [[ -z "${seen[$value]+x}" ]]; then
            seen[$value]=1
            echo "$value"
        fi
    done
}

ensure_params_file() {
    local path="$1"
    local label="$2"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    if [[ ! -f "$path" ]]; then
        echo "Missing ${label}: ${path}" >&2
        exit 1
    fi
}

needs_ml_rebuild() {
    case "$REBUILD_ML" in
        always)
            return 0
            ;;
        never)
            return 1
            ;;
        auto)
            for inst in "${INSTRUMENTS[@]}"; do
                if [[ ! -f "output/models/${inst}_histgbm.pkl" ]]; then
                    return 0
                fi
                if [[ ! -f "output/ml_data/${inst}_features.parquet" ]]; then
                    return 0
                fi
            done
            return 1
            ;;
        *)
            echo "Unsupported REBUILD_ML value: $REBUILD_ML" >&2
            exit 1
            ;;
    esac
}

copy_window_trades() {
    local window_dir="$1"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "+ copy output/market_data/*.trades.json -> $window_dir/"
        return 0
    fi
    find "$window_dir" -maxdepth 1 -name "*.trades.json" -delete
    for inst in "${INSTRUMENTS[@]}"; do
        local src="output/market_data/${inst}.trades.json"
        if [[ -f "$src" ]]; then
            cp "$src" "$window_dir/"
        fi
    done
}

promote_canonical_params() {
    local params_file="$1"

    echo "=================================================="
    echo "Phase 3: Promote Canonical Live Params"
    echo "=================================================="

    run_cmd cp "$params_file" output/live_params.json
}

run_window_sweep() {
    local days="$1"
    local end_time="${2:-}"
    local window_dir="output/${days}day"
    local params_rel="${days}day/live_params.json"
    local log_file="${window_dir}/sweep_${days}d.log"

    mkdir -p "$window_dir"
    if [[ "$DRY_RUN" != "1" ]]; then
        rm -f "output/${params_rel}" "$log_file"
    fi

    echo "=================================================="
    echo "Phase 1: ${days}-Day Parameter Optimization Sweep"
    echo "=================================================="

    local optimizer_cmd=(
        env PYTHONPATH=. "$PYTHON_BIN" scripts/research/gpu_optimizer.py
        --instrument "${INSTRUMENTS[@]}"
        --signal-type "$SIGNAL_TYPE"
        --max_combinations "$MAX_COMBINATIONS"
        --lookback-days "$days"
        --min-trades "$MIN_TRADES"
        --max-trades "$MAX_TRADES"
        --output-file "$params_rel"
    )
    if [[ -n "$end_time" ]]; then
        optimizer_cmd+=(--end-time "$end_time")
    fi
    if [[ "$USE_REGIME" == "1" ]]; then
        optimizer_cmd+=(--use-regime)
    fi
    if [[ "$REQUIRE_ST_PEAK" == "1" ]]; then
        optimizer_cmd+=(--require-st-peak)
    fi
    if [[ "$REFINE_SWEEP" == "1" ]]; then
        optimizer_cmd+=(--refine)
    fi
    run_logged_cmd "$log_file" "${optimizer_cmd[@]}"
}

resolve_window_end_time() {
    local days="$1"
    local requested_end_time="${2:-}"
    if [[ -n "$requested_end_time" ]]; then
        echo "$requested_end_time"
        return 0
    fi
    if [[ -n "$EXPORT_END_TIME" ]]; then
        echo "$EXPORT_END_TIME"
        return 0
    fi
    local ref_dir="${REFERENCE_EXPORT_ROOT}/${days}day"
    if [[ -d "$ref_dir" ]]; then
        local ref_file
        ref_file="$(find "$ref_dir" -maxdepth 1 -name '*.trades.json' | sort | head -n 1)"
        if [[ -n "$ref_file" ]]; then
            env REF_FILE="$ref_file" python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["REF_FILE"])
payload = json.loads(path.read_text(encoding="utf-8"))
period = payload.get("period") or {}
end = period.get("end")
if isinstance(end, str) and end:
    print(end)
PY
            return 0
        fi
    fi
    return 1
}

resolve_export_params_for_window() {
    local days="$1"
    local explicit_source="${2:-}"
    local window_params="output/${days}day/live_params.json"

    if [[ -n "$explicit_source" ]]; then
        echo "$explicit_source"
        return 0
    fi
    if [[ -f "$window_params" ]]; then
        echo "$window_params"
        return 0
    fi
    if [[ -f "output/live_params.json" ]]; then
        echo "output/live_params.json"
        return 0
    fi

    echo "Missing params file for ${days}-day export." >&2
    echo "Run the sweep first or set EXPORT_PARAMS_PATH/CANONICAL_PARAMS_PATH to a winner params JSON." >&2
    exit 1
}

prepare_window_params() {
    local days="$1"
    local source_params="$2"
    local window_dir="output/${days}day"
    local window_params="${window_dir}/live_params.json"

    mkdir -p "$window_dir"
    ensure_params_file "$source_params" "export params source"

    if [[ "$source_params" != "$window_params" ]]; then
        echo "+ cp $source_params $window_params" >&2
        if [[ "$DRY_RUN" != "1" ]]; then
            cp "$source_params" "$window_params"
        fi
    fi
    echo "$window_params"
}

export_window_trades() {
    local days="$1"
    local source_params="${2:-}"
    local requested_end_time="${3:-}"
    local window_dir="output/${days}day"
    local resolved_source=""
    local params_file=""
    local end_time=""

    echo "=================================================="
    echo "Phase 2: Export ${days}-Day Trades"
    echo "=================================================="

    resolved_source="$(resolve_export_params_for_window "$days" "$source_params")"
    params_file="$(prepare_window_params "$days" "$resolved_source")"
    if end_time="$(resolve_window_end_time "$days" "$requested_end_time")"; then
        echo "Using pinned export end time for ${days}-day window: ${end_time}"
    fi

    local export_cmd=(
        env PYTHONPATH=. "$PYTHON_BIN" scripts/tools/export_optimal_trades.py
        --instrument "${INSTRUMENTS[@]}"
        --signal-type "$SIGNAL_TYPE"
        --lookback-days "$days"
        --params-file "$params_file"
    )
    if [[ -n "$end_time" ]]; then
        export_cmd+=(--end-time "$end_time")
    fi
    if [[ "$USE_REGIME" == "1" ]]; then
        export_cmd+=(--use-regime)
    fi
    if [[ "$REQUIRE_ST_PEAK" == "1" ]]; then
        export_cmd+=(--require-st-peak)
    fi
    if [[ "$USE_ML" == "1" ]]; then
        export_cmd+=(--use-ml)
        if [[ "$ML_PRIMARY_GATE" == "1" ]]; then
            export_cmd+=(--ml-primary-gate)
        fi
    fi
    run_cmd "${export_cmd[@]}"
    copy_window_trades "$window_dir"
}

restore_baseline_outputs() {
    local baseline_days="$1"
    local baseline_dir="output/${baseline_days}day"

    echo "=================================================="
    echo "Phase 3: Restore ${baseline_days}-Day Baseline Outputs"
    echo "=================================================="

    run_cmd cp "${baseline_dir}/live_params.json" output/live_params.json
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "+ restore ${baseline_dir}/*.trades.json -> output/market_data/"
        return 0
    fi
    for inst in "${INSTRUMENTS[@]}"; do
        local src="${baseline_dir}/${inst}.trades.json"
        if [[ -f "$src" ]]; then
            cp "$src" "output/market_data/${inst}.trades.json"
        fi
    done
}

generate_live_profile() {
    local extra_args=()
    if [[ "$ML_PRIMARY_GATE" == "1" ]]; then
        extra_args+=(--ml-primary-gate)
    fi
    if [[ "$USE_REGIME" == "1" ]]; then
        extra_args+=(--use-regime)
    fi
    if [[ "$REQUIRE_ST_PEAK" == "1" ]]; then
        extra_args+=(--require-st-peak)
    fi
    echo "=================================================="
    echo "Phase 4: Generate Live Strategy Profile"
    echo "=================================================="
    run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/tools/json_to_yaml_strategy.py \
        --params-path output/live_params.json \
        --output-path "$LIVE_PROFILE_PATH" \
        --canonical-json-output "$CANONICAL_LIVE_PARAMS_PATH" \
        --signal-type "$SIGNAL_TYPE" \
        "${extra_args[@]}"
}

audit_live_profile() {
    local extra_args=()
    if [[ "$ML_PRIMARY_GATE" == "1" ]]; then
        extra_args+=(--ml-primary-gate)
    fi
    if [[ "$USE_REGIME" == "1" ]]; then
        extra_args+=(--use-regime)
    fi
    if [[ "$REQUIRE_ST_PEAK" == "1" ]]; then
        extra_args+=(--require-st-peak)
    fi
    echo "=================================================="
    echo "Phase 5: Audit Live Strategy Profile"
    echo "=================================================="
    run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/tools/audit_live_strategy.py \
        --params-path output/live_params.json \
        --strategy-path "$LIVE_PROFILE_PATH" \
        --signal-type "$SIGNAL_TYPE" \
        "${extra_args[@]}"
}

mapfile -t CANONICAL_EXPORT_WINDOWS < <(unique_windows "$CANONICAL_WINDOW" "${VALIDATION_WINDOWS[@]}")
mapfile -t INDEPENDENT_WINDOWS < <(unique_windows "${SWEEP_WINDOWS[@]}")

if [[ "$SWEEP_MODE" != "canonical" && "$SWEEP_MODE" != "independent" ]]; then
    echo "Unsupported SWEEP_MODE: ${SWEEP_MODE}" >&2
    exit 1
fi
if [[ "${#CANONICAL_EXPORT_WINDOWS[@]}" -eq 0 ]]; then
    echo "No canonical export windows configured." >&2
    exit 1
fi
if [[ "${#INDEPENDENT_WINDOWS[@]}" -eq 0 ]]; then
    echo "No sweep windows configured." >&2
    exit 1
fi

echo "Sweep mode: ${SWEEP_MODE}"
if [[ "$SWEEP_MODE" == "canonical" ]]; then
    echo "Canonical sweep window: ${CANONICAL_WINDOW}"
    echo "Canonical export windows: ${CANONICAL_EXPORT_WINDOWS[*]}"
else
    echo "Independent sweep windows: ${INDEPENDENT_WINDOWS[*]}"
fi
echo "Instruments: ${INSTRUMENTS[*]}"
echo "Signal type: ${SIGNAL_TYPE}"
echo "Use regime filter: ${USE_REGIME}"
echo "Require st_peak: ${REQUIRE_ST_PEAK}"
echo "Export only: ${EXPORT_ONLY}"
echo "Validate windows only: ${VALIDATE_WINDOWS_ONLY}"
echo "ML primary gate: ${ML_PRIMARY_GATE}"

PINNED_END_TIME=""
if [[ "$SWEEP_MODE" == "canonical" || "$EXPORT_ONLY" != "1" || -n "$RUN_END_TIME" || -n "$EXPORT_END_TIME" ]]; then
    PINNED_END_TIME="$(resolve_effective_end_time)"
    echo "Pinned run end time: ${PINNED_END_TIME}"
fi

if [[ "$SWEEP_MODE" == "canonical" ]]; then
    CANONICAL_SOURCE_PARAMS="${CANONICAL_PARAMS_PATH:-}"
    if [[ "$VALIDATE_WINDOWS_ONLY" == "1" ]]; then
        if [[ -z "$CANONICAL_SOURCE_PARAMS" ]]; then
            CANONICAL_SOURCE_PARAMS="output/${CANONICAL_WINDOW}day/live_params.json"
        fi
        ensure_params_file "$CANONICAL_SOURCE_PARAMS" "canonical params file"
        EXPORT_ONLY=1
        echo "Skipping GPU optimization sweep; exporting canonical + validation windows from ${CANONICAL_SOURCE_PARAMS}."
    elif [[ "$EXPORT_ONLY" == "1" ]]; then
        if [[ -z "$CANONICAL_SOURCE_PARAMS" ]]; then
            CANONICAL_SOURCE_PARAMS="${EXPORT_PARAMS_PATH:-output/${CANONICAL_WINDOW}day/live_params.json}"
        fi
        ensure_params_file "$CANONICAL_SOURCE_PARAMS" "canonical params file"
        echo "Skipping GPU optimization sweep; exporting canonical + validation windows from ${CANONICAL_SOURCE_PARAMS}."
    else
        run_window_sweep "$CANONICAL_WINDOW" "$PINNED_END_TIME"
        CANONICAL_SOURCE_PARAMS="output/${CANONICAL_WINDOW}day/live_params.json"
    fi

    if [[ "$USE_ML" == "1" ]] && needs_ml_rebuild; then
        echo "=================================================="
        echo "Phase 1.5: Build ML Features and Train Models"
        echo "=================================================="
        run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/tools/build_features.py --instruments "${INSTRUMENTS[@]}"
        run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/research/ml_train.py --instruments "${INSTRUMENTS[@]}"
    fi

    for days in "${CANONICAL_EXPORT_WINDOWS[@]}"; do
        export_window_trades "$days" "$CANONICAL_SOURCE_PARAMS" "$PINNED_END_TIME"
    done

    promote_canonical_params "$CANONICAL_SOURCE_PARAMS"
else
    if [[ "$VALIDATE_WINDOWS_ONLY" == "1" ]]; then
        echo "VALIDATE_WINDOWS_ONLY requires SWEEP_MODE=canonical." >&2
        exit 1
    fi

    if [[ "$EXPORT_ONLY" != "1" ]]; then
        for days in "${INDEPENDENT_WINDOWS[@]}"; do
            run_window_sweep "$days" "$PINNED_END_TIME"
        done
    else
        echo "Skipping GPU optimization sweep; exporting from existing window params."
    fi

    if [[ "$USE_ML" == "1" ]] && needs_ml_rebuild; then
        echo "=================================================="
        echo "Phase 1.5: Build ML Features and Train Models"
        echo "=================================================="
        run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/tools/build_features.py --instruments "${INSTRUMENTS[@]}"
        run_cmd env PYTHONPATH=. "$PYTHON_BIN" scripts/research/ml_train.py --instruments "${INSTRUMENTS[@]}"
    fi

    for days in "${INDEPENDENT_WINDOWS[@]}"; do
        export_window_trades "$days" "${EXPORT_PARAMS_PATH:-}" "$PINNED_END_TIME"
    done

    restore_baseline_outputs "${INDEPENDENT_WINDOWS[0]}"
fi

if [[ "$GENERATE_LIVE_PROFILE" == "1" ]]; then
    generate_live_profile
fi

if [[ "$AUDIT_LIVE_PROFILE" == "1" ]]; then
    audit_live_profile
fi

echo "=================================================="
if [[ "$SWEEP_MODE" == "canonical" ]]; then
    if [[ "$VALIDATE_WINDOWS_ONLY" == "1" || "$EXPORT_ONLY" == "1" ]]; then
        echo "Canonical export replay complete."
    else
        echo "Canonical sweep and validation export complete."
    fi
else
    echo "Independent sweep and export complete."
fi
if [[ "$SWEEP_MODE" == "canonical" ]]; then
    echo "Window outputs are in: $(printf 'output/%sday ' "${CANONICAL_EXPORT_WINDOWS[@]}")"
    echo "Canonical output/live_params.json promoted from ${CANONICAL_SOURCE_PARAMS}."
else
    echo "Window outputs are in: $(printf 'output/%sday ' "${INDEPENDENT_WINDOWS[@]}")"
    echo "Baseline output/live_params.json restored from ${INDEPENDENT_WINDOWS[0]}-day sweep."
fi
if [[ "$GENERATE_LIVE_PROFILE" == "1" ]]; then
    echo "Live profile updated at: ${LIVE_PROFILE_PATH}"
    echo "Promoted live params snapshot updated at: ${CANONICAL_LIVE_PARAMS_PATH}"
fi
echo "=================================================="
