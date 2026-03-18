#!/usr/bin/env python3
"""Gate evaluation logic extracted from PortfolioManager for improved maintainability.

This module contains all structural metric extraction, guard threshold checking,
semantic filtering, and regime filtering logic. Extracted to reduce complexity
in portfolio_manager.py and improve testability.
"""
from __future__ import annotations


from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Guard metric keys that can be configured via strategy profile
GUARD_KEYS = (
    "min_coherence",
    "min_stability",
    "max_entropy",
    "max_coherence_tau_slope",
    "max_domain_wall_slope",
    "min_low_freq_share",
    "max_reynolds_ratio",
    "min_temporal_half_life",
    "min_spatial_corr_length",
    "min_pinned_alignment",
)


def structural_metric(payload: Mapping[str, Any], key: str) -> Optional[float]:
    """Extract a structural metric from gate payload with fallback search.
    
    Searches in order: payload.components, payload.structure, payload.metrics, payload root.
    Returns None if metric not found or invalid.
    
    Args:
        payload: Gate payload dictionary
        key: Metric name (e.g., 'coherence', 'stability')
        
    Returns:
        Metric value as float, or None if not found/invalid
    """
    sources: Sequence[Any] = (
        payload.get("components"),
        payload.get("structure"),
        payload.get("metrics"),
        payload,
    )
    for source in sources:
        if isinstance(source, Mapping) and key in source:
            try:
                return float(source[key])
            except (TypeError, ValueError):
                return None
    return None


class GateEvaluator:
    """Evaluates gate payloads against strategy profile guards and filters.
    
    Responsible for:
    - Structural metric extraction (coherence, stability, entropy, etc.)
    - Guard threshold checking (min/max comparisons)
    - Semantic tag filtering (optional ML-based tagging)
    - Regime filtering and confidence checking
    
    This class isolates gate evaluation logic from PortfolioManager to improve
    maintainability and enable focused unit testing.
    """
    
    def __init__(self, semantic_tagger_fn=None):
        """Initialize evaluator with optional semantic tagging function.
        
        Args:
            semantic_tagger_fn: Optional callable that generates semantic tags from gate payload.
                               If None, semantic filtering is disabled.
        """
        self._semantic_tagger = semantic_tagger_fn
    
    def evaluate(
        self,
        payload: Dict[str, Any],
        *,
        hazard_max: Optional[float] = None,
        min_repetitions: int = 1,
        guards: Dict[str, Optional[float]],
        semantic_filter: List[str],
        regime_filter: List[str],
        min_regime_confidence: float = 0.0,
    ) -> Tuple[bool, List[str]]:
        """Evaluate gate payload against all configured guards and filters.
        
        Args:
            payload: Gate payload dictionary from Valkey
            hazard_max: Maximum allowed hazard value (optional legacy parameter)
            min_repetitions: Minimum repetitions required for admission
            guards: Dictionary of guard thresholds keyed by GUARD_KEYS
            semantic_filter: Required semantic tags (empty list = no filtering)
            regime_filter: Required regime labels (empty list = no filtering)
            min_regime_confidence: Minimum confidence for regime match
            
        Returns:
            Tuple of (admitted: bool, reasons: List[str])
            - admitted: True if all checks pass
            - reasons: List of failure reasons (empty if admitted)
        """
        reasons: List[str] = []
        
        if not payload:
            return False, ["missing_payload"]
        
        # Check admit flag (hard requirement)
        if not bool(payload.get("admit")):
            return False, ["admit_false"]
        
        # Check repetitions
        repetitions = payload.get("repetitions")
        try:
            rep_int = int(repetitions)
        except (TypeError, ValueError):
            rep_int = None
        if rep_int is None or rep_int < max(1, min_repetitions):
            reasons.append("repetitions_short")
        
        # Apply all structural metric guards
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_coherence"),
            metric_key="coherence",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="coherence_invalid",
            fail_reason="coherence_low",
            include_values=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_stability"),
            metric_key="stability",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="stability_invalid",
            fail_reason="stability_below_min",
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("max_entropy"),
            metric_key="entropy",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="entropy_invalid",
            fail_reason="entropy_above_max",
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("max_coherence_tau_slope"),
            metric_key="coherence_tau_slope",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="coherence_tau_slope_invalid",
            fail_reason="coherence_tau_slope_above_max",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("max_domain_wall_slope"),
            metric_key="domain_wall_slope",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="domain_wall_slope_invalid",
            fail_reason="domain_wall_slope_above_max",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_low_freq_share"),
            metric_key="spectral_lowf_share",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="spectral_lowf_share_invalid",
            fail_reason="spectral_lowf_share_below_min",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("max_reynolds_ratio"),
            metric_key="reynolds_ratio",
            compare=lambda value, threshold: value > threshold,
            invalid_reason="reynolds_invalid",
            fail_reason="reynolds_above_max",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_temporal_half_life"),
            metric_key="temporal_half_life",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="temporal_half_life_invalid",
            fail_reason="temporal_half_life_below_min",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_spatial_corr_length"),
            metric_key="spatial_corr_length",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="spatial_corr_length_invalid",
            fail_reason="spatial_corr_length_below_min",
            require_metric=True,
        ))
        
        reasons.extend(self._check_guard(
            payload,
            threshold=guards.get("min_pinned_alignment"),
            metric_key="pinned_alignment",
            compare=lambda value, threshold: value < threshold,
            invalid_reason="pinned_alignment_invalid",
            fail_reason="pinned_alignment_below_min",
            require_metric=True,
        ))
        
        # Semantic tag filtering
        if semantic_filter:
            reasons.extend(self._check_semantic_filter(payload, semantic_filter))
        
        # Regime filtering
        if regime_filter or min_regime_confidence > 0:
            reasons.extend(self._check_regime_filter(
                payload, regime_filter, min_regime_confidence
            ))
        
        admitted = len(reasons) == 0
        
        # Debug logging for rejected pre-admitted gates
        if payload.get("admit") == 1 and not admitted:
            import logging
            coh_value = structural_metric(payload, "coherence")
            regime_label, regime_confidence = self._extract_regime(payload)
            logging.getLogger(__name__).warning(
                "Rejected pre-admitted gate! Reasons: %s. Reps: %s Coherence: %s RegimeConf: %s",
                reasons,
                payload.get("repetitions"),
                coh_value,
                regime_confidence,
            )
        
        return admitted, reasons
    
    def _check_guard(
        self,
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
        """Check a single guard threshold against payload metric.
        
        Args:
            payload: Gate payload
            threshold: Threshold value (None = skip check)
            metric_key: Metric name to extract
            compare: Comparison function (value, threshold) -> bool (True = fail)
            invalid_reason: Reason string if metric missing/invalid
            fail_reason: Reason string if comparison fails
            include_values: Include metric value in fail reason
            require_metric: Return invalid_reason if metric not found
            
        Returns:
            List of failure reasons (empty if pass)
        """
        if threshold is None:
            return []
        
        value = structural_metric(payload, metric_key)
        
        if value is None:
            return [invalid_reason] if require_metric else []
        
        try:
            value_f = float(value)
            threshold_f = float(threshold)
        except (TypeError, ValueError):
            return [invalid_reason]
        
        try:
            if compare(value_f, threshold_f):
                if include_values:
                    return [f"{fail_reason}:{value_f:.3f}<{threshold_f:.3f}"]
                return [fail_reason]
        except Exception:
            return [invalid_reason]
        
        return []
    
    def _check_semantic_filter(
        self, payload: Dict[str, Any], required_tags: List[str]
    ) -> List[str]:
        """Check semantic tag requirements.
        
        Args:
            payload: Gate payload
            required_tags: List of required semantic tags
            
        Returns:
            List containing failure reason if tags missing, empty otherwise
        """
        if not self._semantic_tagger:
            return []
        
        required_lower = [tag.lower() for tag in required_tags if tag.strip()]
        if not required_lower:
            return []
        
        try:
            observed_tags = self._semantic_tagger(payload)
        except TypeError:
            # Fallback for older signature
            observed_tags = self._semantic_tagger(payload, overrides=None)  # type: ignore
        except Exception:
            return []
        
        observed_lower = {
            tag.strip().lower()
            for tag in observed_tags
            if isinstance(tag, str) and tag.strip()
        }
        
        missing = [tag for tag in required_lower if tag not in observed_lower]
        if missing:
            return [f"semantic_filter_missing:{','.join(missing)}"]
        
        return []
    
    def _check_regime_filter(
        self,
        payload: Dict[str, Any],
        regime_labels: List[str],
        min_confidence: float,
    ) -> List[str]:
        """Check regime label and confidence requirements.
        
        Args:
            payload: Gate payload
            regime_labels: Required regime labels (empty = no label filtering)
            min_confidence: Minimum confidence threshold
            
        Returns:
            List of failure reasons (empty if pass)
        """
        reasons: List[str] = []
        regime_label, regime_confidence = self._extract_regime(payload)
        
        regime_filters_lower = [
            tag.lower() for tag in regime_labels if tag.strip()
        ]
        
        if regime_filters_lower:
            if not regime_label:
                reasons.append("regime_missing")
            elif regime_label not in regime_filters_lower:
                reasons.append("regime_filtered")
        
        if min_confidence > 0:
            if regime_confidence is None:
                reasons.append("regime_confidence_missing")
            elif regime_confidence < min_confidence:
                reasons.append("regime_confidence_low")
        
        return reasons
    
    def _extract_regime(
        self, payload: Mapping[str, Any]
    ) -> Tuple[Optional[str], Optional[float]]:
        """Extract regime label and confidence from payload.
        
        Handles both nested and flat regime structures:
        - payload.regime.label / payload.regime.confidence
        - payload.regime (string) / payload.regime_confidence
        
        Args:
            payload: Gate payload
            
        Returns:
            Tuple of (regime_label, regime_confidence)
        """
        regime = payload.get("regime")
        
        if isinstance(regime, Mapping):
            label = regime.get("label")
            confidence = regime.get("confidence")
            try:
                conf_value = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
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
        except (TypeError, ValueError):
            conf_value = None
        
        return label_value, conf_value


__all__ = [
    "GateEvaluator",
    "structural_metric",
    "GUARD_KEYS",
]
