from types import SimpleNamespace

from scripts.trading.api_serializers import serialize_gate_metrics
from scripts.trading.gate_loader import StrategyInstrument


class DummyPortfolioManager:
    def __init__(self, strategy: object, payloads: dict[str, dict[str, object]]) -> None:
        self.strategy = strategy
        self._payloads = payloads

    def latest_gate_payloads(self) -> dict[str, dict[str, object]]:
        return self._payloads


def _profile(*, ml_primary_gate: bool = False) -> StrategyInstrument:
    return StrategyInstrument(
        symbol="EUR_USD",
        hazard_max=None,
        hazard_min=0.1,
        min_repetitions=2,
        guards={
            "min_coherence": 0.5,
            "min_stability": 0.2,
            "max_entropy": 1.0,
        },
        session=None,
        ml_primary_gate=ml_primary_gate,
    )


def test_serialize_gate_metrics_uses_live_gate_shape_without_bundle_fields() -> None:
    strategy = SimpleNamespace(get=lambda instrument: _profile())
    payload = {
        "ts_ms": 1_710_000_000_000,
        "direction": "BUY",
        "hazard": 0.25,
        "repetitions": 3,
        "st_peak": True,
        "ml_prob": 0.42,
        "structure": {
            "coherence": 0.7,
            "stability": 0.4,
            "entropy": 0.5,
            "coherence_tau_slope": -0.02,
        },
        "reasons": ["ml_confidence_low:0.42<0.98"],
        "bundle_hits": [{"id": "NB001"}],
        "bundle_blocks": ["CB002"],
    }
    portfolio_manager = DummyPortfolioManager(strategy, {"EUR_USD": payload})

    result = serialize_gate_metrics(["EUR_USD"], portfolio_manager)
    gate = result["gates"][0]

    assert gate["admit"] is False
    assert gate["direction"] == "BUY"
    assert gate["st_peak"] is True
    assert gate["ml_probability"] == 0.42
    assert gate["reasons"] == ["ml_confidence_low"]
    assert gate["reason_details"] == ["ml_confidence_low:0.42<0.98"]
    assert gate["structure"]["coherence"] == 0.7
    assert "bundle_hits" not in gate
    assert "bundle_blocks" not in gate
    assert "raw" not in gate


def test_serialize_gate_metrics_relaxes_guards_for_ml_primary_profiles() -> None:
    strategy = SimpleNamespace(get=lambda instrument: _profile(ml_primary_gate=True))
    payload = {
        "ts_ms": 1_710_000_000_000,
        "direction": "SELL",
        "hazard": 0.25,
        "repetitions": 1,
        "structure": {
            "coherence": 0.05,
            "stability": 0.01,
            "entropy": 2.5,
        },
    }
    portfolio_manager = DummyPortfolioManager(strategy, {"EUR_USD": payload})

    result = serialize_gate_metrics(["EUR_USD"], portfolio_manager)
    gate = result["gates"][0]

    assert gate["admit"] is True
    assert gate["reasons"] == []
