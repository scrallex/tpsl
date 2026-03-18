#!/usr/bin/env python3
import argparse
import json
import os
import yaml
from pathlib import Path


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
    return parser.parse_args()


def generate_yaml():
    args = _parse_args()
    output_dict = {
        "global": {
            "direction": "momentum",
            "min_repetitions": 1,
            "hazard_max": 0.99,
            "hazard_exit_threshold": 0.0,
            "exit_horizon": 480,
            "session_exit_minutes": 5,
            "margin_hyst_high": 0.30,
            "regime_filter": [],
            "min_regime_confidence": 0.0,
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
        return

    with open(params_path, "r") as f:
        data = json.load(f)

    for inst, full_params in data.items():
        if target_signal not in full_params:
            continue
            
        p = full_params[target_signal]

        # Build instrument profile
        haz = p.get("Haz")
        reps = p.get("Reps", 1)
        hold = p.get("Hold", 1000)
        coh = p.get("Coh")
        ent = p.get("Ent")
        stab = p.get("Stab")

        sl = p.get("SL")
        tp = p.get("TP")
        trail = p.get("Trail")
        be = p.get("BE")

        is_pacific = "JPY" in inst or "AUD" in inst or "NZD" in inst
        regime_filters = []
        if args.use_regime and not args.ml_primary_gate:
            regime_filters = [] if is_pacific else ["long_ok", "short_ok"]

        guards = {}
        if coh is not None:
            guards["min_coherence"] = coh
        if ent is not None:
            guards["max_entropy"] = ent
        if stab is not None:
            guards["min_stability"] = stab

        output_dict["instruments"][inst] = {
            "session": {"start": "00:00Z", "end": "23:59Z"},
            "invert_bundles": True,  # MR flips directional spikes into fades
            "allow_fallback": False,
            "ml_primary_gate": bool(args.ml_primary_gate),
            "regime_filter": regime_filters,
            "hazard_min": haz,  # MR uses hazard_min
            "hazard_max": None,
            "min_repetitions": reps,
            "stop_loss_pct": sl,
            "take_profit_pct": tp,
            "trailing_stop_pct": trail,
            "breakeven_trigger_pct": be,
            "guards": guards,
            "exit": {"exit_horizon": 40, "hold_rearm": True, "max_hold_minutes": hold},
        }

    out_path = Path(args.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(output_dict, f, default_flow_style=False, sort_keys=False)

    print(f"Successfully wrote {len(output_dict['instruments'])} instrument configs to {out_path}")


if __name__ == "__main__":
    generate_yaml()
