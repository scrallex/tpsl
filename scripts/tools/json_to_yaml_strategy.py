#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.trading.live_params import iter_signal_payloads


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--params-path",
        default="output/live_params.json",
        help="Path to the GPU winner params JSON.",
    )
    parser.add_argument(
        "--output-path",
        default="config/mean_reversion_strategy.yaml",
        help="Path to the generated live strategy YAML.",
    )
    parser.add_argument(
        "--signal-type",
        default="mean_reversion",
        help="Signal block to project into the live YAML.",
    )
    parser.add_argument(
        "--ml-primary-gate",
        action="store_true",
        help="Enable archived ML-primary relaxed-gate semantics in the live YAML.",
    )
    parser.add_argument(
        "--use-regime",
        action="store_true",
        help="Project the GPU regime filter into the live YAML.",
    )
    parser.add_argument(
        "--canonical-json-output",
        help="Optional path to write the selected signal params as the promoted live params snapshot.",
    )
    parser.add_argument(
        "--require-st-peak",
        action="store_true",
        help="Require structural-tension peak reversal for mean-reversion entries.",
    )
    return parser.parse_args()


def _is_mean_reversion(signal_type: str) -> bool:
    return str(signal_type or "").strip().lower() == "mean_reversion"


def _regime_filters_for(
    instrument: str,
    *,
    use_regime: bool,
    ml_primary_gate: bool,
) -> list[str]:
    if not use_regime or ml_primary_gate:
        return []
    is_pacific = "JPY" in instrument or "AUD" in instrument or "NZD" in instrument
    return [] if is_pacific else ["long_ok", "short_ok"]


def _build_instrument_profile(
    instrument: str,
    signal_params: Dict[str, Any],
    *,
    signal_type: str,
    ml_primary_gate: bool,
    use_regime: bool,
    require_st_peak: bool,
) -> Dict[str, Any]:
    is_mean_reversion = _is_mean_reversion(signal_type)
    hazard_value = signal_params.get("Haz")
    regime_filters = _regime_filters_for(
        instrument,
        use_regime=use_regime,
        ml_primary_gate=ml_primary_gate,
    )

    guards = {}
    if signal_params.get("Coh") is not None:
        guards["min_coherence"] = signal_params["Coh"]
    if signal_params.get("Ent") is not None:
        guards["max_entropy"] = signal_params["Ent"]
    if signal_params.get("Stab") is not None:
        guards["min_stability"] = signal_params["Stab"]

    instrument_profile: Dict[str, Any] = {
        "session": {"start": "00:00Z", "end": "23:59Z"},
        "invert_bundles": is_mean_reversion,
        "require_st_peak": bool(is_mean_reversion and require_st_peak),
        "allow_fallback": False,
        "ml_primary_gate": bool(ml_primary_gate),
        "hazard_min": hazard_value if is_mean_reversion else None,
        "hazard_max": None if is_mean_reversion else hazard_value,
        "min_repetitions": signal_params.get("Reps", 1),
        "stop_loss_pct": signal_params.get("SL"),
        "take_profit_pct": signal_params.get("TP"),
        "trailing_stop_pct": signal_params.get("Trail"),
        "breakeven_trigger_pct": signal_params.get("BE"),
        "guards": guards,
        "exit": {
            "exit_horizon": 40,
            "hold_rearm": True,
            "max_hold_minutes": signal_params.get("Hold", 1000),
        },
    }
    if regime_filters:
        instrument_profile["regime_filter"] = regime_filters
    return instrument_profile


def _canonical_live_params(
    params: Dict[str, Any],
    signal_type: str,
) -> Dict[str, Dict[str, Any]]:
    return {
        instrument: payload
        for instrument, payload in iter_signal_payloads(params, signal_type)
    }


def generate_yaml() -> int:
    args = _parse_args()
    is_mean_reversion = _is_mean_reversion(args.signal_type)
    output_dict = {
        "global": {
            "direction": "mean_reversion" if is_mean_reversion else "momentum",
            "min_repetitions": 1,
            "hazard_max": 0.99,
            "hazard_exit_threshold": 0.0,
            "exit_horizon": 480,
            "session_exit_minutes": 5,
            "margin_hyst_high": 0.30,
            "min_regime_confidence": 0.0,
            "require_st_peak": bool(is_mean_reversion and args.require_st_peak),
            "allow_fallback": False,
            "ml_primary_gate": bool(args.ml_primary_gate),
            "guard_thresholds": {
                "min_coherence": 0.0,
                "min_stability": 0.0,
                "max_entropy": 3.0,
                "max_coherence_tau_slope": 1.0,
                "max_domain_wall_slope": 1.0,
                "min_low_freq_share": 0.0,
                "max_reynolds_ratio": 100.0,
                "min_temporal_half_life": 0.0,
                "min_spatial_corr_length": 0.0,
                "min_pinned_alignment": 0.0,
            },
        },
        "instruments": {},
    }

    params_path = args.params_path
    target_signal = args.signal_type

    if not os.path.exists(params_path):
        print(f"Error: Could not find {params_path}")
        return 1

    with open(params_path, "r") as f:
        data = json.load(f)

    promoted_params = _canonical_live_params(data, target_signal)
    for instrument, signal_params in promoted_params.items():
        output_dict["instruments"][instrument] = _build_instrument_profile(
            instrument,
            signal_params,
            signal_type=target_signal,
            ml_primary_gate=bool(args.ml_primary_gate),
            use_regime=bool(args.use_regime),
            require_st_peak=bool(args.require_st_peak),
        )

    if not output_dict["instruments"]:
        print(
            f"Error: no instruments found for signal_type={target_signal!r} in {params_path}"
        )
        return 1

    out_path = Path(args.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(output_dict, f, default_flow_style=False, sort_keys=False)

    if args.canonical_json_output:
        canonical_path = Path(args.canonical_json_output)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_text(
            json.dumps(promoted_params, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Successfully wrote {len(output_dict['instruments'])} instrument configs to {out_path}")
    if args.canonical_json_output:
        print(
            "Successfully wrote "
            f"{len(promoted_params)} promoted signal payloads to {args.canonical_json_output}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(generate_yaml())
