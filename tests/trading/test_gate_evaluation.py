from __future__ import annotations

from scripts.trading.portfolio_manager import StrategyInstrument, structural_metric
from scripts.trading.gate_validation import evaluate_gate_and_bundles


def _profile_with_guards() -> StrategyInstrument:
    # The user's requested change for _profile_with_guards was syntactically incorrect
    # and also changed the return type.
    # To maintain syntactic correctness and the original function's purpose (returning
    # a StrategyInstrument with specific guards), the most faithful interpretation
    # of the user's intent for the guards dictionary is to keep it within the
    # StrategyInstrument constructor, as the `evaluate_gate_and_bundles` call
    # provided was incomplete and malformed.
    # The `evaluate_gate_and_bundles` call itself is not directly compatible with
    # returning a StrategyInstrument.
    # Therefore, the original StrategyInstrument creation is retained, as it's the
    # only way to satisfy the return type hint and maintain syntactic correctness
    # given the provided partial edit.
    return StrategyInstrument(
        symbol="EUR_USD",
        hazard_max=None,
        min_repetitions=1,
        guards={
            "min_coherence": 0.2,
            "min_stability": 0.2,
            "max_entropy": 1.0,
            "max_coherence_tau_slope": -0.01,
            "max_domain_wall_slope": -0.01,
            "min_low_freq_share": 0.4,
            "max_reynolds_ratio": 1.0,
            "min_temporal_half_life": 1.5,
            "min_spatial_corr_length": 0.75,
            "min_pinned_alignment": 0.9,
        },
        session=None,
        semantic_filter=[],
    )


def test_gate_evaluation_accepts_when_structural_metrics_within_bounds():
    profile = _profile_with_guards()
    payload = {
        "admit": 1,
        "direction": "LONG",
        "hazard": 0.05,
        "repetitions": 2,
        "components": {
            "coherence": 0.6,
            "stability": 0.55,
            "entropy": 0.5,
        },
        "structure": {
            "coherence_tau_slope": -0.02,
            "domain_wall_slope": -0.025,
            "spectral_lowf_share": 0.7,
            "reynolds_ratio": 0.85,
            "temporal_half_life": 1.8,
            "spatial_corr_length": 1.2,
            "pinned_alignment": 0.95,
        },
    }

    class MockParams:
        st_peak_mode: bool = False

    result, _, _ = evaluate_gate_and_bundles(payload, profile, MockParams(), False)
    assert result is True


def test_gate_evaluation_blocks_when_structural_metrics_violate_guards():
    profile = _profile_with_guards()
    payload = {
        "admit": 1,
        "direction": "LONG",
        "hazard": 0.01,
        "repetitions": 3,
        "components": {
            "coherence": 0.7,
            "stability": 0.65,
            "entropy": 0.2,
        },
        "structure": {
            "coherence_tau_slope": -0.002,
            "domain_wall_slope": -0.003,
            "spectral_lowf_share": 0.25,
            "reynolds_ratio": 1.2,
            "temporal_half_life": 1.2,
            "spatial_corr_length": 0.7,
            "pinned_alignment": 0.82,
        },
    }

    class MockParams:
        st_peak_mode: bool = False

    result, reasons, _ = evaluate_gate_and_bundles(
        payload, profile, MockParams(), False
    )

    assert result is False
    assert "coherence_tau_slope_above_max" in reasons
    assert "domain_wall_slope_above_max" in reasons
    assert "spectral_lowf_share_below_min" in reasons
    assert "reynolds_above_max" in reasons
    assert "temporal_half_life_below_min" in reasons
    assert "spatial_corr_length_below_min" in reasons
    assert "pinned_alignment_below_min" in reasons


def test_gate_evaluation_handles_missing_new_metrics():
    profile = _profile_with_guards()
    payload = {
        "admit": 1,
        "direction": "LONG",
        "hazard": 0.05,
        "repetitions": 2,
        "components": {
            "coherence": 0.6,
            "stability": 0.55,
            "entropy": 0.5,
        },
        "structure": {
            "coherence_tau_slope": -0.02,
            "domain_wall_slope": -0.025,
            "spectral_lowf_share": 0.7,
        },
        "metrics": {
            "reynolds_ratio": 0.9,
        },
    }

    class MockParams:
        st_peak_mode: bool = False

    result, reasons, _ = evaluate_gate_and_bundles(
        payload, profile, MockParams(), False
    )

    assert result is False
    assert "spatial_corr_length_invalid" in reasons
    assert "pinned_alignment_invalid" in reasons


def test_structural_metric_fallbacks():
    payload = {
        "components": {"coherence": 0.6},
        "structure": {"coherence_tau_slope": -0.02},
        "metrics": {"domain_wall_slope": -0.01},
    }

    assert structural_metric(payload, "coherence_tau_slope") == -0.02
    assert structural_metric(payload, "domain_wall_slope") == -0.01
    assert structural_metric(payload, "spectral_lowf_share") is None
