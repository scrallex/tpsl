"""Single-run backtest orchestration for the isolated options research package."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from options_research.data import DataRequest, HistoricalOptionsDataSource
from options_research.models import (
    BacktestResult,
    EquityPoint,
    ExitReason,
    OptionChainSnapshot,
    SignalDirection,
    SignalEvent,
)
from options_research.portfolio import PortfolioState, SimplePortfolioEngine
from options_research.reporting import BasicMetricsCalculator, MetricsCalculator
from options_research.selection import VerticalDebitSpreadSelector
from options_research.signals import SignalContext, UnderlyingSignalGenerator


def _ensure_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    underlying: str
    start: datetime
    end: datetime
    initial_capital: float = 100000.0
    close_open_positions_at_end: bool = True
    signal_lookback_days: int = 365
    signal_activation_policy: Literal["immediate", "next_snapshot"] = "next_snapshot"
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.underlying:
            raise ValueError("underlying is required")
        _ensure_tz_aware(self.start, "start")
        _ensure_tz_aware(self.end, "end")
        if self.end < self.start:
            raise ValueError("end cannot be before start")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.signal_lookback_days < 0:
            raise ValueError("signal_lookback_days must be non-negative")
        if self.signal_activation_policy not in {"immediate", "next_snapshot"}:
            raise ValueError("signal_activation_policy must be 'immediate' or 'next_snapshot'")


class BacktestRunner(Protocol):
    """Runs a point-in-time backtest over historical bars and option chains."""

    def run(self, config: BacktestConfig) -> BacktestResult:
        ...


class SignalDrivenBacktestRunner:
    """Runs a minimal signal-driven options backtest over historical snapshots."""

    def __init__(
        self,
        *,
        data_source: HistoricalOptionsDataSource,
        signal_generator: UnderlyingSignalGenerator,
        selector: VerticalDebitSpreadSelector,
        portfolio_engine: SimplePortfolioEngine | None = None,
        metrics_calculator: MetricsCalculator | None = None,
        allow_overlapping_positions: bool = False,
    ) -> None:
        self.data_source = data_source
        self.signal_generator = signal_generator
        self.selector = selector
        self.portfolio_engine = portfolio_engine or SimplePortfolioEngine()
        self.metrics_calculator = metrics_calculator or BasicMetricsCalculator()
        self.allow_overlapping_positions = allow_overlapping_positions

    def run(self, config: BacktestConfig) -> BacktestResult:
        signal_start = config.start - timedelta(days=config.signal_lookback_days)
        request = DataRequest(
            underlying=config.underlying,
            start=signal_start,
            end=config.end,
        )
        bars = tuple(self.data_source.load_underlying_bars(request))
        bars_in_window = tuple(bar for bar in bars if config.start <= bar.timestamp <= config.end)
        snapshots = tuple(
            sorted(
                self.data_source.iter_option_chain_snapshots(request),
                key=lambda snapshot: snapshot.as_of,
            )
        )
        signals = tuple(
            sorted(
                self.signal_generator.generate(
                    SignalContext(
                        underlying=config.underlying,
                        bars=bars,
                        metadata={
                            "backtest_metadata": dict(config.metadata),
                            "backtest_start": config.start,
                            "backtest_end": config.end,
                        },
                    )
                ),
                key=lambda signal: signal.occurred_at,
            )
        )
        signals = tuple(signal for signal in signals if config.start <= signal.occurred_at <= config.end)
        scheduled_signals = self._schedule_signals(
            signals=signals,
            snapshots=snapshots,
            bars=bars,
            activation_policy=config.signal_activation_policy,
        )

        state = PortfolioState(
            as_of=config.start,
            cash=config.initial_capital,
            equity=config.initial_capital,
        )
        attempted_entries = 0
        rejected_entries = 0
        rejection_breakdown: defaultdict[str, int] = defaultdict(int)
        equity_curve = [
            EquityPoint(
                timestamp=config.start,
                equity=config.initial_capital,
                cash=config.initial_capital,
                drawdown=0.0,
            )
        ]
        signal_index = 0
        current_signal: SignalEvent | None = None
        last_snapshot: OptionChainSnapshot | None = None

        for snapshot in snapshots:
            last_snapshot = snapshot
            state = self.portfolio_engine.mark_to_market(
                state=state,
                snapshots={snapshot.underlying: snapshot},
                as_of=snapshot.as_of,
            )

            new_signals: list[SignalEvent] = []
            while signal_index < len(scheduled_signals) and scheduled_signals[signal_index][0] <= snapshot.as_of:
                _, current_signal = scheduled_signals[signal_index]
                new_signals.append(current_signal)
                signal_index += 1

            for open_position in tuple(state.open_positions):
                exit_eval = self.portfolio_engine.evaluate_exit(
                    position=open_position,
                    snapshot=snapshot,
                    current_signal=current_signal,
                )
                if exit_eval.reason is None:
                    continue
                state, _, close_rejection = self.portfolio_engine.close_position(
                    state=state,
                    position_id=open_position.position_id,
                    snapshot=snapshot,
                    reason=exit_eval.reason,
                )
                if close_rejection is not None:
                    rejection_breakdown[close_rejection] += 1

            entry_signal = self._entry_signal(new_signals)
            if entry_signal is not None:
                attempted_entries += 1
                if (
                    not self.allow_overlapping_positions
                    and any(position.intent.underlying == config.underlying for position in state.open_positions)
                ):
                    rejected_entries += 1
                    rejection_breakdown["open_position_exists"] += 1
                else:
                    selection_outcome = self.selector.select(signal=entry_signal, snapshot=snapshot)
                    if not selection_outcome.accepted or selection_outcome.intent is None:
                        rejected_entries += 1
                        rejection_breakdown[selection_outcome.rejection_reason or "selection_rejected"] += 1
                    else:
                        state, _, open_rejection = self.portfolio_engine.open_position(
                            state=state,
                            intent=selection_outcome.intent,
                            snapshot=snapshot,
                        )
                        if open_rejection is not None:
                            rejected_entries += 1
                            rejection_breakdown[open_rejection] += 1

            equity_curve.append(self._equity_point(state, equity_curve))

        if config.close_open_positions_at_end and last_snapshot is not None and state.open_positions:
            for open_position in tuple(state.open_positions):
                state, _, close_rejection = self.portfolio_engine.close_position(
                    state=state,
                    position_id=open_position.position_id,
                    snapshot=last_snapshot,
                    reason=ExitReason.FORCED_EXIT,
                )
                if close_rejection is not None:
                    rejection_breakdown[close_rejection] += 1
            equity_curve.append(self._equity_point(state, equity_curve))

        base_config = dict(config.metadata)
        base_config.update(
            {
                "attempted_entries": attempted_entries,
                "rejection_breakdown": dict(rejection_breakdown),
                "signals_generated": len(signals),
                "tradable_signals": len(scheduled_signals),
                "signal_activation_policy": config.signal_activation_policy,
                "signal_lookback_days": config.signal_lookback_days,
                "bars_loaded": len(bars_in_window),
                "bars_loaded_for_signals": len(bars),
                "snapshots_loaded": len(snapshots),
            }
        )
        result = BacktestResult(
            strategy_name="signal_driven_vertical_debit_spread",
            started_at=config.start,
            finished_at=state.as_of,
            positions=tuple(state.closed_positions),
            equity_curve=tuple(equity_curve),
            metrics={},
            config=base_config,
            rejected_entries=rejected_entries,
            notes=tuple(self._result_notes(bars=bars_in_window, snapshots=snapshots, signals=signals, state=state)),
        )
        report = self.metrics_calculator.compute(result)
        return replace(
            result,
            metrics=report.metrics,
            strategy_summaries=report.metadata.get("strategy_summaries", {}),
            underlying_summaries=report.metadata.get("underlying_summaries", {}),
            notes=tuple(report.metadata.get("notes", result.notes)),
        )

    @staticmethod
    def _schedule_signals(
        *,
        signals: tuple[SignalEvent, ...],
        snapshots: tuple[OptionChainSnapshot, ...],
        bars,
        activation_policy: Literal["immediate", "next_snapshot"],
    ) -> tuple[tuple[datetime, SignalEvent], ...]:
        if activation_policy == "immediate":
            return tuple((signal.occurred_at, signal) for signal in signals)

        if not snapshots:
            return ()

        scheduled: list[tuple[datetime, SignalEvent]] = []
        daily_bars = SignalDrivenBacktestRunner._is_daily_bar_series(bars)
        snapshot_index = 0
        for signal in signals:
            reference_date = SignalDrivenBacktestRunner._signal_reference_date(signal)
            while snapshot_index < len(snapshots):
                candidate = snapshots[snapshot_index].as_of
                if candidate <= signal.occurred_at:
                    snapshot_index += 1
                    continue
                if daily_bars and candidate.date() <= reference_date:
                    snapshot_index += 1
                    continue
                scheduled.append((candidate, signal))
                break
            if snapshot_index >= len(snapshots):
                break
        return tuple(scheduled)

    @staticmethod
    def _signal_reference_date(signal: SignalEvent):
        source_bar_timestamp = signal.metadata.get("source_bar_timestamp")
        if isinstance(source_bar_timestamp, str):
            try:
                return datetime.fromisoformat(source_bar_timestamp.replace("Z", "+00:00")).date()
            except ValueError:
                pass
        if isinstance(source_bar_timestamp, datetime):
            return source_bar_timestamp.date()
        return signal.occurred_at.date()

    @staticmethod
    def _is_daily_bar_series(bars) -> bool:  # noqa: ANN001
        if len(bars) < 2:
            return False
        deltas = [
            (bars[index + 1].timestamp - bars[index].timestamp).total_seconds()
            for index in range(len(bars) - 1)
            if bars[index + 1].timestamp > bars[index].timestamp
        ]
        if not deltas:
            return False
        deltas.sort()
        median_delta = deltas[len(deltas) // 2]
        return median_delta >= 18 * 3600

    @staticmethod
    def _entry_signal(new_signals: list[SignalEvent]) -> SignalEvent | None:
        if not new_signals:
            return None
        latest = new_signals[-1]
        if latest.direction is SignalDirection.FLAT:
            return None
        return latest

    @staticmethod
    def _equity_point(state: PortfolioState, existing_points: list[EquityPoint]) -> EquityPoint:
        peak_equity = max((point.equity for point in existing_points), default=state.equity)
        drawdown = 0.0
        if peak_equity > 0:
            drawdown = max(0.0, (peak_equity - state.equity) / peak_equity)
        return EquityPoint(
            timestamp=state.as_of,
            equity=state.equity,
            cash=state.cash,
            drawdown=drawdown,
        )

    @staticmethod
    def _result_notes(
        *,
        bars,
        snapshots,
        signals,
        state: PortfolioState,
    ) -> list[str]:
        notes: list[str] = []
        if not bars:
            notes.append("No underlying bars loaded for the requested window.")
        if not snapshots:
            notes.append("No option chain snapshots loaded for the requested window.")
        if not signals:
            notes.append("No signals were generated for the requested window.")
        if state.open_positions:
            notes.append("Some positions remained open at the end of the run.")
        return notes
