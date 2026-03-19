#!/usr/bin/env python3
"""Helpers for projecting optimizer params into live/runtime structures."""
from __future__ import annotations

from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

SIGNAL_BLOCK_KEYS: Tuple[str, ...] = (
    "mean_reversion",
    "trend_sniper",
    "squeeze_breakout",
)

RAW_SIGNAL_KEYS: Tuple[str, ...] = (
    "Haz",
    "Reps",
    "Hold",
    "SL",
    "TP",
    "Trail",
    "HazEx",
    "Coh",
    "Ent",
    "Stab",
    "BE",
)


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _has_any(mapping: Mapping[str, Any], *keys: str) -> bool:
    return any(key in mapping for key in keys)


def _looks_like_signal_payload(payload: Mapping[str, Any]) -> bool:
    if any(key in payload for key in RAW_SIGNAL_KEYS):
        return True
    if any(
        key in payload
        for key in (
            "hazard_min",
            "hazard_max",
            "min_repetitions",
            "hold_minutes",
            "stop_loss_pct",
            "take_profit_pct",
            "trailing_stop_pct",
            "breakeven_trigger_pct",
            "guards",
        )
    ):
        return True
    return False


def normalise_signal_payload(
    payload: Mapping[str, Any],
    *,
    signal_type: str,
) -> Dict[str, Any]:
    guards = payload.get("guards")
    guard_map = guards if isinstance(guards, Mapping) else {}
    is_mean_reversion = str(signal_type or "").strip().lower() == "mean_reversion"
    field_sources = {
        "Haz": ("Haz", "hazard_min" if is_mean_reversion else "hazard_max", "hazard"),
        "Reps": ("Reps", "min_repetitions"),
        "Hold": ("Hold", "hold_minutes", "exit_horizon"),
        "SL": ("SL", "stop_loss_pct", "sl_margin"),
        "TP": ("TP", "take_profit_pct", "tp_margin"),
        "Trail": ("Trail", "trailing_stop_pct"),
        "HazEx": ("HazEx", "hazard_exit_threshold"),
        "Coh": ("Coh", "min_coherence"),
        "Ent": ("Ent", "max_entropy"),
        "Stab": ("Stab", "min_stability"),
        "BE": ("BE", "breakeven_trigger_pct"),
    }

    normalised: Dict[str, Any] = {}
    for target_key in RAW_SIGNAL_KEYS:
        source_keys = field_sources[target_key]
        if _has_any(payload, *source_keys):
            normalised[target_key] = _first_present(payload, *source_keys)

    guard_sources = {
        "Coh": ("min_coherence",),
        "Ent": ("max_entropy",),
        "Stab": ("min_stability",),
    }
    for target_key, source_keys in guard_sources.items():
        if target_key not in normalised and _has_any(guard_map, *source_keys):
            normalised[target_key] = _first_present(guard_map, *source_keys)

    return normalised


def extract_signal_payload(entry: Any, signal_type: str) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, Mapping):
        return None

    nested = entry.get(signal_type)
    if isinstance(nested, Mapping):
        return normalise_signal_payload(nested, signal_type=signal_type)

    if any(isinstance(entry.get(key), Mapping) for key in SIGNAL_BLOCK_KEYS):
        return None

    if not _looks_like_signal_payload(entry):
        return None

    return normalise_signal_payload(entry, signal_type=signal_type)


def iter_signal_payloads(
    params: Any,
    signal_type: str,
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    if not isinstance(params, Mapping):
        return

    for instrument, entry in sorted(params.items(), key=lambda item: str(item[0] or "")):
        symbol = str(instrument or "").strip().upper()
        if not symbol:
            continue
        payload = extract_signal_payload(entry, signal_type)
        if payload is not None:
            yield symbol, payload
