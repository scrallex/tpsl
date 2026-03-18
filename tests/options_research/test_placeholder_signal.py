from __future__ import annotations

from datetime import datetime, timedelta, timezone

from options_research.models import SignalDirection, UnderlyingBar
from options_research.signals import (
    MovingAverageSignalConfig,
    MovingAverageSignalGenerator,
    SignalContext,
)


def build_bars(closes: list[float]) -> tuple[UnderlyingBar, ...]:
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    bars = []
    for idx, close in enumerate(closes):
        timestamp = start + timedelta(minutes=5 * idx)
        bars.append(
            UnderlyingBar(
                symbol="SPY",
                timestamp=timestamp,
                open=close - 0.25,
                high=close + 0.25,
                low=close - 0.50,
                close=close,
                volume=1000 + idx,
            )
        )
    return tuple(bars)


def test_placeholder_signal_generator_emits_bullish_transition() -> None:
    bars = build_bars(
        [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.4, 101.8, 102.2, 102.6]
    )
    generator = MovingAverageSignalGenerator(
        MovingAverageSignalConfig(short_window=3, long_window=5, min_gap_pct=0.001)
    )

    events = generator.generate(SignalContext(underlying="SPY", bars=bars))

    assert len(events) == 1
    assert events[0].direction is SignalDirection.BULLISH
    assert events[0].underlying == "SPY"
    assert events[0].occurred_at > bars[4].timestamp
    assert events[0].metadata["source_bar_timestamp"] == bars[4].timestamp.isoformat()


def test_placeholder_signal_generator_emits_bearish_transition() -> None:
    bars = build_bars(
        [102.6, 102.2, 101.8, 101.4, 101.0, 100.8, 100.6, 100.4, 100.2, 100.0]
    )
    generator = MovingAverageSignalGenerator(
        MovingAverageSignalConfig(short_window=3, long_window=5, min_gap_pct=0.001)
    )

    events = generator.generate(SignalContext(underlying="SPY", bars=bars))

    assert len(events) == 1
    assert events[0].direction is SignalDirection.BEARISH
    assert events[0].underlying == "SPY"
    assert events[0].occurred_at > bars[4].timestamp
