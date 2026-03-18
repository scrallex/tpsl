from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.research.simulator.gpu_parity_replay import collapse_gates_for_gpu_parity
from scripts.tools.export_optimal_trades import (
    _align_profile_for_export,
    _gpu_parity_pnl_bps,
)


def test_align_profile_for_export_disables_regime_when_gpu_sweep_did_not_use_it() -> None:
    profile = SimpleNamespace(
        regime_filter=["long_ok", "short_ok"],
        min_regime_confidence=0.55,
    )

    aligned = _align_profile_for_export(
        profile,
        use_regime=False,
        ml_primary_gate=False,
    )

    assert aligned.regime_filter == []
    assert aligned.min_regime_confidence == 0.0


def test_align_profile_for_export_preserves_regime_when_requested() -> None:
    profile = SimpleNamespace(
        regime_filter=["long_ok", "short_ok"],
        min_regime_confidence=0.55,
    )

    aligned = _align_profile_for_export(
        profile,
        use_regime=True,
        ml_primary_gate=False,
    )

    assert aligned.regime_filter == ["long_ok", "short_ok"]
    assert aligned.min_regime_confidence == 0.55


def test_collapse_gates_for_gpu_parity_uses_live_repetitions_field() -> None:
    collapsed = collapse_gates_for_gpu_parity(
        [
            {
                "ts_ms": 1000,
                "direction": "BUY",
                "hazard": 0.0,
                "components": {"coherence": 0.2, "entropy": 0.1, "stability": 0.0},
            },
            {
                "ts_ms": 2000,
                "direction": "BUY",
                "hazard": 0.0,
                "repetitions": 2.0,
                "components": {"coherence": 0.15, "entropy": 0.1, "stability": 0.0},
            },
        ]
    )

    assert collapsed[1]["_gpu_reps"] == 2.0
    assert collapsed[1]["_gpu_st_peak"] is False


def test_collapse_gates_for_gpu_parity_ignores_admit_flag_like_tensor_loader() -> None:
    collapsed = collapse_gates_for_gpu_parity(
        [
            {
                "ts_ms": 1000,
                "direction": "SELL",
                "admit": 0,
                "hazard": 0.5,
                "components": {"coherence": 0.2, "entropy": 0.1, "stability": 0.0},
            }
        ]
    )

    assert collapsed[0]["_gpu_action"] == -1


def test_collapse_gates_for_gpu_parity_prefers_structure_metrics() -> None:
    collapsed = collapse_gates_for_gpu_parity(
        [
            {
                "ts_ms": 1000,
                "direction": "BUY",
                "hazard": None,
                "structure": {
                    "coherence": 0.25,
                    "entropy": 0.75,
                    "stability": 0.5,
                    "hazard": 0.2,
                },
                "components": {
                    "coherence": 0.1,
                    "entropy": 1.5,
                    "stability": 0.0,
                    "hazard": 0.9,
                },
            }
        ]
    )

    assert collapsed[0]["_gpu_hazard"] == 0.2
    assert collapsed[0]["_gpu_coh"] == 0.25
    assert collapsed[0]["_gpu_ent"] == 0.75
    assert collapsed[0]["_gpu_stab"] == 0.5


def test_gpu_parity_pnl_bps_uses_price_returns_and_fixed_trade_cost() -> None:
    trades = [
        SimpleNamespace(
            direction="LONG",
            entry_price=1.0,
            exit_price=1.002,
        ),
        {
            "direction": "SHORT",
            "entry_price": 1.0,
            "exit_price": 0.999,
        },
    ]

    assert _gpu_parity_pnl_bps(trades) == pytest.approx(27.0)
