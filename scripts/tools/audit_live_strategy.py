#!/usr/bin/env python3
"""Audit that a live strategy YAML matches a chosen optimizer params file."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

from scripts.trading.gate_loader import StrategyProfile


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare a params JSON against the active live strategy YAML."
    )
    parser.add_argument(
        "--params-path",
        default="output/live_params.json",
        help="Path to the optimizer winner params JSON.",
    )
    parser.add_argument(
        "--strategy-path",
        default="config/mean_reversion_strategy.yaml",
        help="Path to the generated live strategy YAML.",
    )
    parser.add_argument(
        "--signal-type",
        default="mean_reversion",
        help="Signal block to compare against the live YAML.",
    )
    parser.add_argument(
        "--ml-primary-gate",
        action="store_true",
        help="Expect archived ML-primary relaxed-gate mode in the live YAML.",
    )
    parser.add_argument(
        "--use-regime",
        action="store_true",
        help="Expect the GPU regime filter in the live YAML.",
    )
    return parser.parse_args()


def _is_close(left: Any, right: Any, *, tol: float = 1e-9) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tol)
    except (TypeError, ValueError):
        return left == right


def _fmt(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _expected_regime_filter(instrument: str, *, use_regime: bool) -> list[str]:
    if not use_regime:
        return []
    is_pacific = any(token in instrument for token in ("JPY", "AUD", "NZD"))
    return [] if is_pacific else ["long_ok", "short_ok"]


def _iter_param_instruments(
    params: dict[str, Any], signal_type: str
) -> Iterable[tuple[str, dict[str, Any]]]:
    for instrument, payload in sorted(params.items()):
        if not isinstance(payload, dict):
            continue
        signal_payload = payload.get(signal_type)
        if isinstance(signal_payload, dict):
            yield instrument.upper(), signal_payload


def main() -> int:
    args = _parse_args()
    params_path = Path(args.params_path)
    strategy_path = Path(args.strategy_path)

    if not params_path.exists():
        print(f"Missing params file: {params_path}", file=sys.stderr)
        return 1
    if not strategy_path.exists():
        print(f"Missing strategy file: {strategy_path}", file=sys.stderr)
        return 1

    params = json.loads(params_path.read_text(encoding="utf-8"))
    profile = StrategyProfile.load(strategy_path)
    mismatches: list[str] = []

    param_instruments = {inst for inst, _ in _iter_param_instruments(params, args.signal_type)}
    strategy_instruments = set(profile.instruments.keys())

    for inst in sorted(param_instruments - strategy_instruments):
        mismatches.append(f"{inst}: missing from strategy YAML")
    for inst in sorted(strategy_instruments - param_instruments):
        mismatches.append(f"{inst}: present in strategy YAML but missing from params JSON")

    for inst, p in _iter_param_instruments(params, args.signal_type):
        live = profile.get(inst)
        if live is None:
            continue

        expected = {
            "hazard_min": p.get("Haz") if args.signal_type == "mean_reversion" else None,
            "hazard_max": None if args.signal_type == "mean_reversion" else p.get("Haz"),
            "min_repetitions": p.get("Reps", 1),
            "stop_loss_pct": p.get("SL"),
            "take_profit_pct": p.get("TP"),
            "trailing_stop_pct": p.get("Trail"),
            "breakeven_trigger_pct": p.get("BE"),
            "hold_minutes": p.get("Hold"),
            "min_coherence": p.get("Coh"),
            "min_stability": p.get("Stab"),
            "max_entropy": p.get("Ent"),
            "allow_fallback": False,
            "ml_primary_gate": bool(args.ml_primary_gate),
            "invert_bundles": True,
            "regime_filter": []
            if args.ml_primary_gate
            else _expected_regime_filter(inst, use_regime=args.use_regime),
        }
        actual = {
            "hazard_min": live.hazard_min,
            "hazard_max": live.hazard_max,
            "min_repetitions": live.min_repetitions,
            "stop_loss_pct": live.stop_loss_pct,
            "take_profit_pct": live.take_profit_pct,
            "trailing_stop_pct": live.trailing_stop_pct,
            "breakeven_trigger_pct": live.breakeven_trigger_pct,
            "hold_minutes": live.hold_minutes,
            "min_coherence": live.guards.get("min_coherence"),
            "min_stability": live.guards.get("min_stability"),
            "max_entropy": live.guards.get("max_entropy"),
            "allow_fallback": live.allow_fallback,
            "ml_primary_gate": live.ml_primary_gate,
            "invert_bundles": live.invert_bundles,
            "regime_filter": list(live.regime_filter or []),
        }

        for field, expected_value in expected.items():
            actual_value = actual[field]
            if isinstance(expected_value, list):
                if actual_value != expected_value:
                    mismatches.append(
                        f"{inst}: {field} expected {expected_value} got {actual_value}"
                    )
                continue
            if not _is_close(actual_value, expected_value):
                mismatches.append(
                    f"{inst}: {field} expected {_fmt(expected_value)} got {_fmt(actual_value)}"
                )

    if mismatches:
        print("LIVE STRATEGY AUDIT FAILED")
        for line in mismatches:
            print(f"- {line}")
        return 1

    print(
        "LIVE STRATEGY AUDIT OK "
        f"({len(param_instruments)} instruments, signal={args.signal_type}, "
        f"params={params_path}, strategy={strategy_path})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
