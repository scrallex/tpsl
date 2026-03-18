from scripts.trading.ml_evaluator import MLEvaluator
from scripts.trading.portfolio_manager import _live_ml_gate_enabled


def test_ml_evaluator_bypasses_when_disabled() -> None:
    evaluator = MLEvaluator(["EUR_USD"], enabled=False)

    admitted, reason = evaluator.evaluate_gate(
        "EUR_USD",
        {"direction": "BUY", "admit": 1},
        service=object(),
        current_st=0.25,
        reps=1.0,
        ml_primary_gate=True,
    )

    assert admitted is True
    assert reason == ""
    assert evaluator.models == {}


def test_live_ml_gate_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE_ML_GATE", raising=False)
    assert _live_ml_gate_enabled() is False

    monkeypatch.setenv("LIVE_ENABLE_ML_GATE", "1")
    assert _live_ml_gate_enabled() is True
