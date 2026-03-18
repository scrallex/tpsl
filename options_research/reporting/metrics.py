"""Metric and serialization helpers for options backtest results."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Protocol, Sequence

from options_research.models import BacktestResult, EquityPoint, FilledOptionPosition


@dataclass(frozen=True, slots=True)
class ReportEnvelope:
    result: BacktestResult
    metrics: dict[str, float]
    metadata: dict[str, object] = field(default_factory=dict)


class MetricsCalculator(Protocol):
    """Computes machine-readable performance summaries from a backtest result."""

    def compute(self, result: BacktestResult) -> ReportEnvelope:
        ...


class BasicMetricsCalculator:
    """Computes a minimal machine-readable performance report for a backtest."""

    def compute(self, result: BacktestResult) -> ReportEnvelope:
        realized_pnls = [position.realized_pnl for position in result.positions if position.realized_pnl is not None]
        closed_positions = [position for position in result.positions if position.realized_pnl is not None]
        wins = [pnl for pnl in realized_pnls if pnl > 0]
        losses = [pnl for pnl in realized_pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        total_max_loss = sum(self._position_max_loss(position) for position in result.positions)
        attempted_entries = self._attempted_entries(result)
        fallback_positions = [position for position in closed_positions if self._used_fallback_selection(position)]
        sharpe = self._daily_sharpe(result.equity_curve)
        max_drawdown = self._max_drawdown(result.equity_curve)

        metrics = {
            "total_return": float(result.total_return or 0.0),
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "win_rate": (len(wins) / len(closed_positions)) if closed_positions else 0.0,
            "profit_factor": self._profit_factor(gross_profit, gross_loss),
            "average_hold_hours": self._average_hold_hours(closed_positions),
            "expectancy_per_trade": (sum(realized_pnls) / len(realized_pnls)) if realized_pnls else 0.0,
            "return_on_max_loss": (sum(realized_pnls) / total_max_loss) if total_max_loss > 0 else 0.0,
            "fill_rejection_rate": (result.rejected_entries / attempted_entries) if attempted_entries > 0 else 0.0,
            "fallback_selection_rate": (len(fallback_positions) / len(closed_positions)) if closed_positions else 0.0,
            "total_trades": float(len(result.positions)),
            "attempted_entries": float(attempted_entries),
            "rejected_entries": float(result.rejected_entries),
        }
        metadata = {
            "exit_reason_breakdown": self._exit_reason_breakdown(closed_positions),
            "selection_mode_breakdown": self._selection_mode_breakdown(closed_positions),
            "strategy_summaries": self._summaries_by_key(
                closed_positions,
                key_fn=lambda position: position.intent.strategy_family.value,
            ),
            "underlying_summaries": self._summaries_by_key(
                closed_positions,
                key_fn=lambda position: position.intent.underlying,
            ),
            "rejection_breakdown": dict(result.config.get("rejection_breakdown", {})),
        }
        return ReportEnvelope(result=result, metrics=metrics, metadata=metadata)

    @staticmethod
    def _profit_factor(gross_profit: float, gross_loss: float) -> float:
        if gross_loss > 0:
            return gross_profit / gross_loss
        if gross_profit > 0:
            return float("inf")
        return 0.0

    @staticmethod
    def _position_max_loss(position: FilledOptionPosition) -> float:
        return position.intent.max_loss * position.intent.contract_multiplier * position.intent.contracts

    @staticmethod
    def _attempted_entries(result: BacktestResult) -> int:
        configured = result.config.get("attempted_entries")
        if isinstance(configured, int) and configured >= 0:
            return configured
        return len(result.positions) + result.rejected_entries

    @staticmethod
    def _average_hold_hours(positions: Sequence[FilledOptionPosition]) -> float:
        hold_hours = [
            position.holding_period.total_seconds() / 3600.0
            for position in positions
            if position.holding_period is not None
        ]
        if not hold_hours:
            return 0.0
        return mean(hold_hours)

    @staticmethod
    def _exit_reason_breakdown(positions: Sequence[FilledOptionPosition]) -> dict[str, float]:
        counts: defaultdict[str, float] = defaultdict(float)
        for position in positions:
            if position.exit_reason is None:
                continue
            counts[position.exit_reason.value] += 1.0
        return dict(counts)

    def _summaries_by_key(
        self,
        positions: Sequence[FilledOptionPosition],
        *,
        key_fn,
    ) -> dict[str, dict[str, float]]:
        grouped: defaultdict[str, list[FilledOptionPosition]] = defaultdict(list)
        for position in positions:
            grouped[key_fn(position)].append(position)

        summaries: dict[str, dict[str, float]] = {}
        for key, group_positions in grouped.items():
            pnls = [position.realized_pnl or 0.0 for position in group_positions]
            wins = [pnl for pnl in pnls if pnl > 0]
            losses = [pnl for pnl in pnls if pnl < 0]
            gross_profit = sum(wins)
            gross_loss = abs(sum(losses))
            total_max_loss = sum(self._position_max_loss(position) for position in group_positions)
            summaries[key] = {
                "trades": float(len(group_positions)),
                "win_rate": (len(wins) / len(group_positions)) if group_positions else 0.0,
                "total_pnl": sum(pnls),
                "average_pnl": mean(pnls) if pnls else 0.0,
                "profit_factor": self._profit_factor(gross_profit, gross_loss),
                "average_hold_hours": self._average_hold_hours(group_positions),
                "return_on_max_loss": (sum(pnls) / total_max_loss) if total_max_loss > 0 else 0.0,
                "fallback_selection_rate": (
                    sum(1.0 for position in group_positions if self._used_fallback_selection(position)) / len(group_positions)
                )
                if group_positions
                else 0.0,
            }
        return summaries

    @staticmethod
    def _selection_mode_breakdown(positions: Sequence[FilledOptionPosition]) -> dict[str, float]:
        counts: defaultdict[str, float] = defaultdict(float)
        for position in positions:
            mode = str(position.intent.metadata.get("long_leg_selection_mode") or "unknown")
            counts[mode] += 1.0
        return dict(counts)

    @staticmethod
    def _used_fallback_selection(position: FilledOptionPosition) -> bool:
        return position.intent.metadata.get("long_leg_selection_mode") == "closest_liquid_otm_fallback"

    @staticmethod
    def _max_drawdown(equity_curve: Sequence[EquityPoint]) -> float:
        if not equity_curve:
            return 0.0
        peak = equity_curve[0].equity
        max_drawdown = 0.0
        for point in equity_curve:
            peak = max(peak, point.equity)
            if peak <= 0:
                continue
            drawdown = (peak - point.equity) / peak
            max_drawdown = max(max_drawdown, drawdown)
        return max_drawdown

    @staticmethod
    def _daily_sharpe(equity_curve: Sequence[EquityPoint]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        last_by_date: dict[object, float] = {}
        for point in equity_curve:
            last_by_date[point.timestamp.date()] = point.equity
        ordered = [equity for _, equity in sorted(last_by_date.items())]
        if len(ordered) < 2:
            return 0.0
        returns = []
        for previous, current in zip(ordered[:-1], ordered[1:], strict=True):
            if previous <= 0:
                continue
            returns.append((current / previous) - 1.0)
        if len(returns) < 2:
            return 0.0
        std_dev = pstdev(returns)
        if std_dev == 0:
            return 0.0
        return (mean(returns) / std_dev) * (252.0 ** 0.5)
