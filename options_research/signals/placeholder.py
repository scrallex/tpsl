"""Placeholder signal generator built from the underlying only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean

from options_research.models import SignalDirection, SignalEvent
from options_research.signals.base import SignalContext


@dataclass(frozen=True, slots=True)
class MovingAverageSignalConfig:
    short_window: int = 5
    long_window: int = 20
    min_gap_pct: float = 0.001
    emit_flat_signals: bool = False
    signal_name: str = "placeholder_ma_regime"

    def __post_init__(self) -> None:
        if self.short_window <= 0 or self.long_window <= 0:
            raise ValueError("moving-average windows must be positive")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be less than long_window")
        if self.min_gap_pct < 0:
            raise ValueError("min_gap_pct must be non-negative")
        if not self.signal_name:
            raise ValueError("signal_name is required")


class MovingAverageSignalGenerator:
    """Simple directional placeholder until SEP regime output is bridged in."""

    def __init__(self, config: MovingAverageSignalConfig | None = None) -> None:
        self.config = config or MovingAverageSignalConfig()

    def generate(self, context: SignalContext) -> list[SignalEvent]:
        bars = context.bars
        if len(bars) < self.config.long_window:
            return []

        events: list[SignalEvent] = []
        last_direction: SignalDirection | None = None
        closes = [bar.close for bar in bars]
        inferred_bar_span = self._infer_bar_span(bars)
        for idx in range(self.config.long_window - 1, len(closes)):
            short_avg = mean(closes[idx - self.config.short_window + 1 : idx + 1])
            long_avg = mean(closes[idx - self.config.long_window + 1 : idx + 1])
            gap_pct = (short_avg / long_avg) - 1.0
            if gap_pct > self.config.min_gap_pct:
                direction = SignalDirection.BULLISH
            elif gap_pct < -self.config.min_gap_pct:
                direction = SignalDirection.BEARISH
            elif self.config.emit_flat_signals:
                direction = SignalDirection.FLAT
            else:
                continue

            if direction == last_direction:
                continue

            strength = min(1.0, abs(gap_pct) / max(self.config.min_gap_pct, 1e-6))
            bar = bars[idx]
            occurred_at = self._bar_close_timestamp(
                bars=bars,
                index=idx,
                inferred_bar_span=inferred_bar_span,
            )
            events.append(
                SignalEvent(
                    underlying=context.underlying,
                    occurred_at=occurred_at,
                    direction=direction,
                    signal_name=self.config.signal_name,
                    strength=strength,
                    metadata={
                        "short_window": self.config.short_window,
                        "long_window": self.config.long_window,
                        "gap_pct": gap_pct,
                        "source_bar_timestamp": bar.timestamp.isoformat(),
                        "bar_close_timestamp": occurred_at.isoformat(),
                    },
                )
            )
            last_direction = direction

        return events

    @staticmethod
    def _infer_bar_span(bars) -> timedelta:  # noqa: ANN001
        deltas = [
            bars[index + 1].timestamp - bars[index].timestamp
            for index in range(len(bars) - 1)
            if bars[index + 1].timestamp > bars[index].timestamp
        ]
        if not deltas:
            return timedelta(days=1)
        ordered = sorted(deltas)
        return ordered[len(ordered) // 2]

    @staticmethod
    def _bar_close_timestamp(*, bars, index: int, inferred_bar_span: timedelta):  # noqa: ANN001
        if index < len(bars) - 1:
            next_timestamp = bars[index + 1].timestamp
            if next_timestamp > bars[index].timestamp:
                return next_timestamp - timedelta(microseconds=1)
        return bars[index].timestamp + inferred_bar_span - timedelta(microseconds=1)
