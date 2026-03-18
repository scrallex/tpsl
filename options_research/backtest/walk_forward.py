"""Rolling walk-forward evaluation and promotion checks for options research."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Callable, Protocol, Sequence

from options_research.backtest.runner import BacktestConfig, BacktestRunner
from options_research.models import BacktestResult, FilledOptionPosition


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        if self.train_end < self.train_start:
            raise ValueError("train_end cannot be before train_start")
        if self.test_end < self.test_start:
            raise ValueError("test_end cannot be before test_start")
        if self.test_start < self.train_end:
            raise ValueError("test_start cannot overlap the training window")


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_span: timedelta
    test_span: timedelta
    step_span: timedelta

    def __post_init__(self) -> None:
        for field_name in ("train_span", "test_span", "step_span"):
            if getattr(self, field_name) <= timedelta(0):
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    min_oos_trades: int = 100
    min_profit_factor: float = 1.2
    min_sharpe: float = 1.0
    min_positive_window_ratio: float = 0.60
    max_fallback_selection_rate: float = 0.25

    def __post_init__(self) -> None:
        if self.min_oos_trades <= 0:
            raise ValueError("min_oos_trades must be positive")
        if self.min_profit_factor <= 0:
            raise ValueError("min_profit_factor must be positive")
        if self.min_sharpe < 0:
            raise ValueError("min_sharpe must be non-negative")
        if not 0.0 <= self.min_positive_window_ratio <= 1.0:
            raise ValueError("min_positive_window_ratio must be within [0.0, 1.0]")
        if not 0.0 <= self.max_fallback_selection_rate <= 1.0:
            raise ValueError("max_fallback_selection_rate must be within [0.0, 1.0]")


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    promotable: bool
    failed_checks: tuple[str, ...]
    summary: dict[str, float]


@dataclass(frozen=True, slots=True)
class WalkForwardWindowResult:
    underlying: str
    window: WalkForwardWindow
    train_result: BacktestResult
    test_result: BacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    windows: tuple[WalkForwardWindowResult, ...]
    oos_metrics: dict[str, float]
    underlying_metrics: dict[str, dict[str, float]]
    promotion: PromotionEvaluation
    metadata: dict[str, object] = field(default_factory=dict)


class WalkForwardRunner(Protocol):
    """Produces sequential out-of-sample backtest results over rolling windows."""

    def build_windows(self, *, start: datetime, end: datetime, config: WalkForwardConfig) -> Sequence[WalkForwardWindow]:
        ...

    def run(
        self,
        *,
        underlyings: Sequence[str],
        start: datetime,
        end: datetime,
        config: WalkForwardConfig,
        backtest_defaults: dict[str, object] | None = None,
        backtest_config_overrides: dict[str, object] | None = None,
    ) -> WalkForwardReport:
        ...


RunnerFactory = BacktestRunner | Callable[[str], BacktestRunner]


class RollingWalkForwardRunner:
    """Runs fixed-parameter rolling train/test evaluations and promotion checks."""

    def __init__(
        self,
        runner_factory: RunnerFactory,
        *,
        initial_capital: float = 100000.0,
        promotion_criteria: PromotionCriteria | None = None,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self.runner_factory = runner_factory
        self.initial_capital = initial_capital
        self.promotion_criteria = promotion_criteria or PromotionCriteria()

    def build_windows(self, *, start: datetime, end: datetime, config: WalkForwardConfig) -> tuple[WalkForwardWindow, ...]:
        if end <= start:
            raise ValueError("end must be after start")
        windows: list[WalkForwardWindow] = []
        cursor = start
        while cursor + config.train_span + config.test_span <= end:
            train_start = cursor
            train_end = cursor + config.train_span
            test_start = train_end
            test_end = test_start + config.test_span
            windows.append(
                WalkForwardWindow(
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )
            cursor += config.step_span
        return tuple(windows)

    def run(
        self,
        *,
        underlyings: Sequence[str],
        start: datetime,
        end: datetime,
        config: WalkForwardConfig,
        backtest_defaults: dict[str, object] | None = None,
        backtest_config_overrides: dict[str, object] | None = None,
    ) -> WalkForwardReport:
        defaults = dict(backtest_defaults or {})
        overrides = dict(backtest_config_overrides or {})
        windows = self.build_windows(start=start, end=end, config=config)
        results: list[WalkForwardWindowResult] = []

        for underlying in underlyings:
            runner = self._resolve_runner(underlying)
            for window in windows:
                train_result = runner.run(
                    BacktestConfig(
                        **overrides,
                        underlying=underlying,
                        start=window.train_start,
                        end=window.train_end,
                        initial_capital=self.initial_capital,
                        metadata={**defaults, "walk_forward_phase": "train"},
                    )
                )
                test_result = runner.run(
                    BacktestConfig(
                        **overrides,
                        underlying=underlying,
                        start=window.test_start,
                        end=window.test_end,
                        initial_capital=self.initial_capital,
                        metadata={**defaults, "walk_forward_phase": "test"},
                    )
                )
                results.append(
                    WalkForwardWindowResult(
                        underlying=underlying,
                        window=window,
                        train_result=train_result,
                        test_result=test_result,
                    )
                )

        oos_results = [window_result.test_result for window_result in results]
        oos_metrics = self._aggregate_results(oos_results)
        underlying_metrics = {
            underlying: self._aggregate_results(
                [window_result.test_result for window_result in results if window_result.underlying == underlying]
            )
            for underlying in underlyings
        }
        promotion = self._evaluate_promotion(oos_metrics)
        metadata = {
            "window_count": len(windows),
            "underlyings": list(underlyings),
            "criteria": {
                "min_oos_trades": self.promotion_criteria.min_oos_trades,
                "min_profit_factor": self.promotion_criteria.min_profit_factor,
                "min_sharpe": self.promotion_criteria.min_sharpe,
                "min_positive_window_ratio": self.promotion_criteria.min_positive_window_ratio,
                "max_fallback_selection_rate": self.promotion_criteria.max_fallback_selection_rate,
            },
        }
        return WalkForwardReport(
            windows=tuple(results),
            oos_metrics=oos_metrics,
            underlying_metrics=underlying_metrics,
            promotion=promotion,
            metadata=metadata,
        )

    def _resolve_runner(self, underlying: str) -> BacktestRunner:
        if callable(self.runner_factory):
            return self.runner_factory(underlying)
        return self.runner_factory

    def _evaluate_promotion(self, oos_metrics: dict[str, float]) -> PromotionEvaluation:
        failed_checks: list[str] = []
        if oos_metrics.get("oos_trades", 0.0) < self.promotion_criteria.min_oos_trades:
            failed_checks.append("min_oos_trades")
        if oos_metrics.get("oos_profit_factor", 0.0) < self.promotion_criteria.min_profit_factor:
            failed_checks.append("profit_factor")
        if oos_metrics.get("oos_sharpe", 0.0) < self.promotion_criteria.min_sharpe:
            failed_checks.append("sharpe")
        if oos_metrics.get("positive_window_ratio", 0.0) < self.promotion_criteria.min_positive_window_ratio:
            failed_checks.append("window_stability")
        if oos_metrics.get("fallback_selection_rate", 0.0) > self.promotion_criteria.max_fallback_selection_rate:
            failed_checks.append("fallback_dependence")
        return PromotionEvaluation(
            promotable=not failed_checks,
            failed_checks=tuple(failed_checks),
            summary=dict(oos_metrics),
        )

    def _aggregate_results(self, results: Sequence[BacktestResult]) -> dict[str, float]:
        positions = [
            position
            for result in results
            for position in result.positions
            if position.realized_pnl is not None
        ]
        pnls = [float(position.realized_pnl or 0.0) for position in positions]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        fallback_positions = sum(1 for position in positions if self._used_fallback_selection(position))
        positive_windows = sum(
            1
            for result in results
            if result.metrics.get("profit_factor", 0.0) >= 1.0 and result.metrics.get("total_return", 0.0) > 0.0
        )
        test_returns = self._collect_daily_returns(results)

        oos_sharpe = 0.0
        if len(test_returns) >= 2:
            volatility = pstdev(test_returns)
            if volatility > 0:
                oos_sharpe = (mean(test_returns) / volatility) * (252.0 ** 0.5)

        return {
            "oos_trades": float(len(positions)),
            "oos_profit_factor": self._profit_factor(gross_profit, gross_loss),
            "oos_sharpe": oos_sharpe,
            "fallback_selection_rate": (fallback_positions / len(positions)) if positions else 0.0,
            "positive_window_ratio": (positive_windows / len(results)) if results else 0.0,
            "window_return_mean": mean([result.metrics.get("total_return", 0.0) for result in results]) if results else 0.0,
            "window_return_std": (
                pstdev([result.metrics.get("total_return", 0.0) for result in results]) if len(results) > 1 else 0.0
            ),
        }

    @staticmethod
    def _profit_factor(gross_profit: float, gross_loss: float) -> float:
        if gross_loss > 0:
            return gross_profit / gross_loss
        if gross_profit > 0:
            return float("inf")
        return 0.0

    @staticmethod
    def _used_fallback_selection(position: FilledOptionPosition) -> bool:
        return position.intent.metadata.get("long_leg_selection_mode") == "closest_liquid_otm_fallback"

    @staticmethod
    def _collect_daily_returns(results: Sequence[BacktestResult]) -> list[float]:
        returns: list[float] = []
        for result in results:
            last_by_date: dict[object, float] = {}
            for point in result.equity_curve:
                last_by_date[point.timestamp.date()] = point.equity
            ordered = [equity for _, equity in sorted(last_by_date.items())]
            for previous, current in zip(ordered[:-1], ordered[1:], strict=True):
                if previous <= 0:
                    continue
                returns.append((current / previous) - 1.0)
        return returns


__all__ = [
    "PromotionCriteria",
    "PromotionEvaluation",
    "RollingWalkForwardRunner",
    "WalkForwardConfig",
    "WalkForwardReport",
    "WalkForwardRunner",
    "WalkForwardWindow",
    "WalkForwardWindowResult",
]
