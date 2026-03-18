from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from options_research.models import SignalDirection, UnderlyingBar
from options_research.signals import SEPRegimeSignalConfig, SEPRegimeSignalGenerator, SignalContext


def _write_gate_file(tmp_path: Path, records: list[dict]) -> Path:
    gate_root = tmp_path / "gates"
    gate_root.mkdir(parents=True)
    gate_path = gate_root / "SPY.gates.jsonl"
    gate_path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return gate_root


def _signal_context() -> SignalContext:
    return SignalContext(
        underlying="SPY",
        bars=(
            UnderlyingBar(
                symbol="SPY",
                timestamp=datetime(2026, 3, 2, 5, 0, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.5,
                close=100.5,
                volume=1000,
            ),
            UnderlyingBar(
                symbol="SPY",
                timestamp=datetime(2026, 3, 3, 5, 0, tzinfo=timezone.utc),
                open=100.5,
                high=101.5,
                low=100.1,
                close=101.0,
                volume=1200,
            ),
        ),
        metadata={
            "backtest_start": datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc),
            "backtest_end": datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc),
        },
    )


def test_sep_regime_signal_generator_maps_buy_sell_gate_records(tmp_path) -> None:
    gate_root = _write_gate_file(
        tmp_path,
        [
            {
                "admit": 1,
                "direction": "BUY",
                "regime": {"label": "bull_trend", "confidence": 0.72},
                "hazard": 0.41,
                "source": "structural_extension",
                "components": {"coherence": 0.8},
                "bundle_hits": ["alpha"],
                "ts_ms": int(datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc).timestamp() * 1000),
            },
            {
                "admit": 1,
                "direction": "SELL",
                "regime": {"label": "bear_trend", "confidence": 0.65},
                "hazard": 0.44,
                "source": "squeeze_breakout",
                "ts_ms": int(datetime(2026, 3, 3, 21, 0, tzinfo=timezone.utc).timestamp() * 1000),
            },
        ],
    )
    generator = SEPRegimeSignalGenerator(
        SEPRegimeSignalConfig(
            emit_on_direction_change_only=True,
            gate_root=gate_root,
            gate_file_pattern="{underlying}.gates.jsonl",
        )
    )

    signals = generator.generate(_signal_context())

    assert len(signals) == 2
    assert signals[0].direction is SignalDirection.BULLISH
    assert signals[0].regime == "bull_trend"
    assert signals[0].metadata["source"] == "structural_extension"
    assert signals[1].direction is SignalDirection.BEARISH


def test_sep_regime_signal_generator_reads_nested_regime_confidence(tmp_path) -> None:
    gate_root = _write_gate_file(
        tmp_path,
        [
            {
                "admit": 1,
                "direction": "BUY",
                "regime": {"label": "bull_trend", "confidence": 0.61},
                "hazard": 0.20,
                "source": "regime_manifold",
                "ts_ms": int(datetime(2026, 3, 2, 20, 0, tzinfo=timezone.utc).timestamp() * 1000),
            }
        ],
    )
    generator = SEPRegimeSignalGenerator(
        SEPRegimeSignalConfig(
            min_regime_confidence=0.6,
            gate_root=gate_root,
            gate_file_pattern="{underlying}.gates.jsonl",
        )
    )

    signals = generator.generate(_signal_context())

    assert len(signals) == 1
    assert signals[0].regime == "bull_trend"


def test_sep_regime_signal_generator_respects_source_and_confidence_filters(tmp_path) -> None:
    gate_root = _write_gate_file(
        tmp_path,
        [
            {
                "admit": 1,
                "direction": "BUY",
                "regime_confidence": 0.40,
                "hazard": 0.60,
                "source": "structural_extension",
                "ts_ms": int(datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc).timestamp() * 1000),
            },
            {
                "admit": 1,
                "direction": "BUY",
                "regime_confidence": 0.80,
                "hazard": 0.55,
                "source": "squeeze_breakout",
                "ts_ms": int(datetime(2026, 3, 3, 21, 0, tzinfo=timezone.utc).timestamp() * 1000),
            },
        ],
    )
    generator = SEPRegimeSignalGenerator(
        SEPRegimeSignalConfig(
            min_regime_confidence=0.5,
            allowed_sources=("squeeze_breakout",),
            emit_on_direction_change_only=False,
            gate_root=gate_root,
            gate_file_pattern="{underlying}.gates.jsonl",
        )
    )

    signals = generator.generate(_signal_context())

    assert len(signals) == 1
    assert signals[0].metadata["source"] == "squeeze_breakout"


def test_sep_regime_signal_generator_requires_historical_gate_file(tmp_path) -> None:
    generator = SEPRegimeSignalGenerator(
        SEPRegimeSignalConfig(
            gate_root=tmp_path / "missing",
            gate_file_pattern="{underlying}.gates.jsonl",
            require_gate_file=True,
        )
    )

    with pytest.raises(FileNotFoundError):
        generator.generate(_signal_context())
