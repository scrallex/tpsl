#!/usr/bin/env python3
"""Unit tests for GateEvaluator class.

Tests gate evaluation logic independently from PortfolioManager to ensure:
- Structural metric extraction works correctly
- Guard threshold checks behave as expected
- Semantic and regime filtering work properly
- Edge cases (missing metrics, invalid values) are handled
"""

import pytest
from scripts.trading.gate_evaluator import GateEvaluator, structural_metric, GUARD_KEYS


class TestStructuralMetricExtraction:
    """Test the structural_metric() helper function."""
    
    def test_extract_from_canonical_metrics(self):
        """Metrics under payload.metrics should be extracted."""
        payload = {"metrics": {"coherence": 0.75, "stability": 0.82}}
        assert structural_metric(payload, "coherence") == 0.75
        assert structural_metric(payload, "stability") == 0.82
    
    def test_extract_from_nested_structure(self):
        """Fallback search should find metrics in components/structure."""
        payload = {"components": {"coherence": 0.68}}
        assert structural_metric(payload, "coherence") == 0.68
        
        payload = {"structure": {"entropy": 0.92}}
        assert structural_metric(payload, "entropy") == 0.92
    
    def test_extract_from_root(self):
        """Metrics at root level should be found as last fallback."""
        payload = {"hazard": 0.35}
        assert structural_metric(payload, "hazard") == 0.35
    
    def test_missing_metric_returns_none(self):
        """Missing metrics should return None."""
        payload = {"metrics": {"coherence": 0.75}}
        assert structural_metric(payload, "missing_key") is None
    
    def test_invalid_metric_value_returns_none(self):
        """Non-numeric metric values should return None."""
        payload = {"metrics": {"coherence": "invalid"}}
        assert structural_metric(payload, "coherence") is None
        
        payload = {"metrics": {"stability": None}}
        assert structural_metric(payload, "stability") is None


class TestGateEvaluatorBasics:
    """Test basic gate evaluation flow."""
    
    def test_missing_payload_rejects(self):
        """Empty payload should be rejected."""
        evaluator = GateEvaluator()
        admitted, reasons = evaluator.evaluate({}, guards={}, semantic_filter=[], regime_filter=[])
        assert not admitted
        assert "missing_payload" in reasons
    
    def test_admit_false_rejects_immediately(self):
        """admit=0 should reject regardless of other checks."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 0,
            "repetitions": 5,
            "metrics": {"coherence": 0.99, "stability": 0.99}
        }
        admitted, reasons = evaluator.evaluate(
            payload, guards={"min_coherence": 0.0}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert "admit_false" in reasons
    
    def test_admit_true_with_passing_guards_accepts(self):
        """admit=1 with passing guards should accept."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 3,
            "metrics": {"coherence": 0.80, "stability": 0.85}
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            min_repetitions=1,
            guards={"min_coherence": 0.70, "min_stability": 0.75},
            semantic_filter=[],
            regime_filter=[],
        )
        assert admitted
        assert len(reasons) == 0
    
    def test_repetitions_short_appends_reason(self):
        """Repetitions below minimum should append failure reason."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 2, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, min_repetitions=3, guards={}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert "repetitions_short" in reasons


class TestGuardThresholds:
    """Test individual guard threshold checks."""
    
    def test_min_coherence_guard(self):
        """coherence < min_coherence should fail."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {"coherence": 0.40}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"min_coherence": 0.50}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert any("coherence_low" in r for r in reasons)
    
    def test_max_entropy_guard(self):
        """entropy > max_entropy should fail."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {"entropy": 3.5}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"max_entropy": 2.0}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert "entropy_above_max" in reasons
    
    def test_min_stability_guard(self):
        """stability < min_stability should fail."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {"stability": 0.60}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"min_stability": 0.75}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert "stability_below_min" in reasons
    
    def test_guard_none_threshold_skips_check(self):
        """Guard with None threshold should be ignored."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {"coherence": 0.10}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"min_coherence": None}, semantic_filter=[], regime_filter=[]
        )
        assert admitted
        assert len(reasons) == 0
    
    def test_required_metric_missing_fails(self):
        """Guards with require_metric=True should fail if metric missing."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"max_coherence_tau_slope": 1.0}, semantic_filter=[], regime_filter=[]
        )
        assert not admitted
        assert "coherence_tau_slope_invalid" in reasons
    
    def test_optional_metric_missing_passes(self):
        """Guards with require_metric=False should pass if metric missing."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={"min_coherence": 0.5}, semantic_filter=[], regime_filter=[]
        )
        assert admitted
        assert len(reasons) == 0


class TestSemanticFiltering:
    """Test semantic tag filtering logic."""
    
    def test_no_semantic_filter_passes(self):
        """Empty semantic_filter should pass."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=[], regime_filter=[]
        )
        assert admitted
    
    def test_semantic_filter_with_no_tagger_passes(self):
        """Semantic filter should be skipped if no tagger function provided."""
        evaluator = GateEvaluator(semantic_tagger_fn=None)
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=["trending"], regime_filter=[]
        )
        assert admitted
    
    def test_semantic_filter_matching_passes(self):
        """Gate with matching semantic tags should pass."""
        def mock_tagger(payload):
            return ["trending", "high_volume"]
        
        evaluator = GateEvaluator(semantic_tagger_fn=mock_tagger)
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=["trending"], regime_filter=[]
        )
        assert admitted
    
    def test_semantic_filter_missing_fails(self):
        """Gate missing required semantic tags should fail."""
        def mock_tagger(payload):
            return ["ranging"]
        
        evaluator = GateEvaluator(semantic_tagger_fn=mock_tagger)
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=["trending", "breakout"], regime_filter=[]
        )
        assert not admitted
        assert any("semantic_filter_missing" in r for r in reasons)


class TestRegimeFiltering:
    """Test regime label and confidence filtering."""
    
    def test_no_regime_filter_passes(self):
        """Empty regime filter should pass."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=[], regime_filter=[]
        )
        assert admitted
    
    def test_regime_label_matching_passes(self):
        """Gate with matching regime label should pass."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {},
            "regime": {"label": "trending", "confidence": 0.80}
        }
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=[], regime_filter=["trending", "breakout"]
        )
        assert admitted
    
    def test_regime_label_mismatch_fails(self):
        """Gate with non-matching regime label should fail."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {},
            "regime": {"label": "ranging", "confidence": 0.70}
        }
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=[], regime_filter=["trending"]
        )
        assert not admitted
        assert "regime_filtered" in reasons
    
    def test_regime_missing_fails(self):
        """Gate with no regime should fail if filter requires one."""
        evaluator = GateEvaluator()
        payload = {"admit": 1, "repetitions": 1, "metrics": {}}
        admitted, reasons = evaluator.evaluate(
            payload, guards={}, semantic_filter=[], regime_filter=["trending"]
        )
        assert not admitted
        assert "regime_missing" in reasons
    
    def test_regime_confidence_below_minimum_fails(self):
        """Regime confidence below threshold should fail."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {},
            "regime": {"label": "trending", "confidence": 0.50}
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            guards={},
            semantic_filter=[],
            regime_filter=[],
            min_regime_confidence=0.70,
        )
        assert not admitted
        assert "regime_confidence_low" in reasons
    
    def test_regime_confidence_missing_fails(self):
        """Missing confidence should fail if minimum set."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {},
            "regime": {"label": "trending"}
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            guards={},
            semantic_filter=[],
            regime_filter=[],
            min_regime_confidence=0.60,
        )
        assert not admitted
        assert "regime_confidence_missing" in reasons
    
    def test_regime_legacy_format(self):
        """Legacy regime format (string + root confidence) should work."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {},
            "regime": "trending",
            "regime_confidence": 0.75
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            guards={},
            semantic_filter=[],
            regime_filter=["trending"],
            min_regime_confidence=0.70,
        )
        assert admitted


class TestMultipleFailures:
    """Test scenarios with multiple simultaneous failures."""
    
    def test_multiple_guard_failures(self):
        """Multiple failing guards should all append reasons."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {"coherence": 0.30, "stability": 0.40, "entropy": 5.0}
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            guards={
                "min_coherence": 0.50,
                "min_stability": 0.60,
                "max_entropy": 2.0,
            },
            semantic_filter=[],
            regime_filter=[],
        )
        assert not admitted
        assert any("coherence_low" in r for r in reasons)
        assert "stability_below_min" in reasons
        assert "entropy_above_max" in reasons
    
    def test_repetitions_and_guards_fail(self):
        """Repetitions short + guard failures should list both."""
        evaluator = GateEvaluator()
        payload = {
            "admit": 1,
            "repetitions": 1,
            "metrics": {"coherence": 0.20}
        }
        admitted, reasons = evaluator.evaluate(
            payload,
            min_repetitions=3,
            guards={"min_coherence": 0.50},
            semantic_filter=[],
            regime_filter=[],
        )
        assert not admitted
        assert "repetitions_short" in reasons
        assert any("coherence_low" in r for r in reasons)


class TestGuardKeys:
    """Test that GUARD_KEYS constant is correct."""
    
    def test_guard_keys_complete(self):
        """GUARD_KEYS should include all expected metrics."""
        expected = {
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
        }
        assert set(GUARD_KEYS) == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
