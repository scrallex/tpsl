#!/usr/bin/env python3
"""Shared gate validation logic for live trading and backtesting."""
from __future__ import annotations


from dataclasses import replace
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.trading.portfolio_manager import StrategyInstrument


logger = logging.getLogger(__name__)

_ML_PRIMARY_RELAXED_MIN_COHERENCE = 0.001
_ML_PRIMARY_RELAXED_MAX_ENTROPY = 5.0


def _ml_primary_enabled(profile: "StrategyInstrument", params: Any) -> bool:
    return bool(
        getattr(params, "ml_primary_gate", False)
        or getattr(profile, "ml_primary_gate", False)
    )


def relaxed_gate_profile(profile: "StrategyInstrument") -> "StrategyInstrument":
    """Return a profile view that mirrors the archived ML-primary gate behavior."""
    relaxed_guards = {key: None for key in profile.guards}
    relaxed_guards["min_coherence"] = _ML_PRIMARY_RELAXED_MIN_COHERENCE
    relaxed_guards["min_stability"] = 0.0
    relaxed_guards["max_entropy"] = _ML_PRIMARY_RELAXED_MAX_ENTROPY
    return replace(
        profile,
        hazard_min=0.0,
        hazard_max=None,
        min_repetitions=1,
        regime_filter=[],
        min_regime_confidence=0.0,
        guards=relaxed_guards,
        require_st_peak=False,
    )


def _extract_structural_metric(
    payload: Mapping[str, Any], key: str
) -> Tuple[Optional[float], bool]:
    sources: Sequence[Any] = (
        payload.get("components"),
        payload.get("structure"),
        payload.get("metrics"),
        payload,
    )
    for source in sources:
        if isinstance(source, Mapping) and key in source:
            try:
                return float(source[key]), True
            except Exception:
                return None, True
    return None, False


def structural_metric(payload: Mapping[str, Any], key: str) -> Optional[float]:
    value, found = _extract_structural_metric(payload, key)
    if not found or value is None:
        return None
    return value


def _apply_guard(
    payload: Mapping[str, Any],
    *,
    threshold: Optional[float],
    metric_key: str,
    compare: Any,
    invalid_reason: str,
    fail_reason: str,
    include_values: bool = False,
    require_metric: bool = False,
) -> List[str]:
    if threshold is None:
        return []
    value, found = _extract_structural_metric(payload, metric_key)
    if not found:
        return [invalid_reason] if require_metric else []
    if value is None:
        return [invalid_reason]
    try:
        value_f = float(value)
        threshold_f = float(threshold)
    except Exception:
        return [invalid_reason]
    try:
        if compare(value_f, threshold_f):
            if include_values:
                return [f"{fail_reason}:{value_f:.3f}<{threshold_f:.3f}"]
            return [fail_reason]
    except Exception:
        return [invalid_reason]
    return []


def _regime_payload(
    payload: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[float]]:
    regime = payload.get("regime")
    if isinstance(regime, Mapping):
        label = regime.get("label")
        confidence = regime.get("confidence")
        try:
            conf_value = float(confidence) if confidence is not None else None
        except Exception:
            conf_value = None
        label_value = str(label).strip().lower() if isinstance(label, str) else None
        return label_value, conf_value
    if isinstance(regime, str):
        label_value = regime.strip().lower()
    else:
        label_value = None
    confidence = payload.get("regime_confidence")
    try:
        conf_value = float(confidence) if confidence is not None else None
    except Exception:
        conf_value = None
    return label_value, conf_value


def _semantic_tags_for(payload: Dict[str, Any]) -> List[str]:
    return []


def gate_evaluation(
    payload: Dict[str, Any], profile: "StrategyInstrument"
) -> Tuple[bool, List[str]]:
    reasons: List[str] = list(payload.get("reasons", []))
    if not payload:
        return False, ["missing_payload"]

    hazard = payload.get("hazard")
    if hazard is None:
        return False, ["missing_hazard"]

    direction = str(payload.get("direction", "")).upper()
    if direction in ("", "FLAT"):
        return False, ["flat_direction"]
    try:
        haz_float = float(hazard)
    except (ValueError, TypeError):
        return False, ["invalid_hazard"]
    if profile.hazard_max is not None and haz_float > profile.hazard_max:
        return False, [
            f"hazard_exceeds_max: {haz_float:.3f} > {profile.hazard_max:.3f}"
        ]
    if profile.hazard_min is not None and haz_float < profile.hazard_min:
        return False, [f"hazard_below_min: {haz_float:.3f} < {profile.hazard_min:.3f}"]

    repetitions = payload.get("repetitions")
    try:
        rep_int = int(repetitions)
    except Exception:
        rep_int = None
    if rep_int is None or rep_int < max(1, profile.min_repetitions):
        reasons.append("repetitions_short")

    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_coherence"),
            metric_key="coherence",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="coherence_invalid",
            fail_reason="coherence_low",
            include_values=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_stability"),
            metric_key="stability",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="stability_invalid",
            fail_reason="stability_below_min",
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("max_entropy"),
            metric_key="entropy",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="entropy_invalid",
            fail_reason="entropy_above_max",
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("max_coherence_tau_slope"),
            metric_key="coherence_tau_slope",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="coherence_tau_slope_invalid",
            fail_reason="coherence_tau_slope_above_max",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("max_domain_wall_slope"),
            metric_key="domain_wall_slope",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="domain_wall_slope_invalid",
            fail_reason="domain_wall_slope_above_max",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_low_freq_share"),
            metric_key="spectral_lowf_share",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="spectral_lowf_share_invalid",
            fail_reason="spectral_lowf_share_below_min",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("max_reynolds_ratio"),
            metric_key="reynolds_ratio",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="reynolds_invalid",
            fail_reason="reynolds_above_max",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_temporal_half_life"),
            metric_key="temporal_half_life",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="temporal_half_life_invalid",
            fail_reason="temporal_half_life_below_min",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_spatial_corr_length"),
            metric_key="spatial_corr_length",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="spatial_corr_length_invalid",
            fail_reason="spatial_corr_length_below_min",
            require_metric=True,
        )
    )
    reasons.extend(
        _apply_guard(
            payload,
            threshold=profile.guards.get("min_pinned_alignment"),
            metric_key="pinned_alignment",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="pinned_alignment_invalid",
            fail_reason="pinned_alignment_below_min",
            require_metric=True,
        )
    )

    if getattr(profile, "require_st_peak", getattr(profile, "invert_bundles", False)):
        if not payload.get("st_peak"):
            reasons.append("st_no_peak_reversal")

    required_tags = [
        tag.lower()
        for tag in profile.semantic_filter
        if isinstance(tag, str) and tag.strip()
    ]
    if required_tags:
        observed = {tag.lower() for tag in _semantic_tags_for(payload)}
        missing = [tag for tag in required_tags if tag not in observed]
        if missing:
            reasons.append("semantic_filter_missing:" + ",".join(missing))

    regime_label, regime_confidence = _regime_payload(payload)
    regime_filters = [
        tag.lower()
        for tag in profile.regime_filter
        if isinstance(tag, str) and tag.strip()
    ]
    if regime_filters:
        if not regime_label:
            reasons.append("regime_missing")
        elif regime_label not in regime_filters:
            reasons.append("regime_filtered")
    if profile.min_regime_confidence and profile.min_regime_confidence > 0:
        if regime_confidence is None:
            reasons.append("regime_confidence_missing")
        elif regime_confidence < profile.min_regime_confidence:
            reasons.append("regime_confidence_low")

    admitted = len(reasons) == 0

    if payload.get("admit") == 1 and not admitted:
        coh_value = structural_metric(payload, "coherence")
        logger.warning(
            "Rejected pre-admitted gate! Reasons: %s. Reps: %s Coherence: %s RegimeConf: %s",
            reasons,
            payload.get("repetitions"),
            coh_value,
            regime_confidence,
        )

    return admitted, reasons


def gate_is_admitted(payload: Dict[str, Any], profile: StrategyInstrument) -> bool:
    admitted, _ = gate_evaluation(payload, profile)
    return admitted


def apply_st_peak_override(
    gate_payload: Dict[str, Any],
    admitted: bool,
    gate_reasons: List[str],
    st_peak_mode: bool,
    has_current_gate: bool,
) -> Tuple[bool, List[str]]:
    # ST constraints are fully vetted in `st_filter.py` and merged in `gate_evaluation`.
    # Removing the override to ensure GPU parameter bounds (haz, coh, ent) are correctly enforced.
    return admitted, gate_reasons


def evaluate_bundles(
    gate_payload: Dict[str, Any],
    admitted: bool,
    gate_reasons: List[str],
    disable_bundle_overrides: bool = False,
) -> Tuple[bool, List[str], bool]:
    bundle_hits = gate_payload.get("bundle_hits") or []
    is_bundle_entry = False

    if not bundle_hits:
        return admitted, gate_reasons, is_bundle_entry

    is_blocked = any(
        str(h.get("action") or "").lower() == "quarantine" for h in bundle_hits
    )
    if is_blocked:
        return admitted, gate_reasons, is_bundle_entry

    has_action = any(
        str(h.get("action") or "").lower()
        in {"promote", "fade", "buy", "sell", "short", "long", "scalp"}
        for h in bundle_hits
    )

    if has_action:
        is_bundle_entry = True

        if not admitted:
            original_reasons = gate_payload.get("reasons", [])
            if not isinstance(original_reasons, list):
                original_reasons = (
                    [] if not original_reasons else [str(original_reasons)]
                )

            is_st_blocked = any(str(r).startswith("st_") for r in original_reasons)
            if not is_st_blocked and not disable_bundle_overrides:
                admitted = True
                gate_reasons = []

    return admitted, gate_reasons, is_bundle_entry


def evaluate_gate_and_bundles(
    gate_payload: Dict[str, Any],
    profile: "StrategyInstrument",
    params: Any,
    current_gate_exists: bool,
) -> Tuple[bool, List[str], bool]:
    effective_profile = (
        relaxed_gate_profile(profile)
        if _ml_primary_enabled(profile, params)
        else profile
    )
    admitted, gate_reasons = gate_evaluation(gate_payload, effective_profile)

    admitted, gate_reasons = apply_st_peak_override(
        gate_payload,
        admitted,
        gate_reasons,
        getattr(params, "st_peak_mode", False),
        current_gate_exists,
    )

    admitted, gate_reasons, is_bundle_entry = evaluate_bundles(
        gate_payload,
        admitted,
        gate_reasons,
        getattr(params, "disable_bundle_overrides", False),
    )

    if getattr(params, "bundles_only", False) and not is_bundle_entry:
        admitted = False

    return admitted, gate_reasons, is_bundle_entry
