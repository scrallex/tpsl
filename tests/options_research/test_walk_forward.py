from __future__ import annotations

from datetime import datetime, timedelta, timezone

from options_research.backtest import (
    PromotionCriteria,
    RollingWalkForwardRunner,
    WalkForwardConfig,
)
from options_research.models import (
    BacktestResult,
    EquityPoint,
    ExitReason,
    FilledOptionPosition,
    LegAction,
    OptionLeg,
    OptionQuote,
    OptionRight,
    OptionStrategyIntent,
    PackageFill,
    PositionStatus,
    SignalDirection,
    SignalEvent,
    StrategyFamily,
)


def _make_position(*, opened_at: datetime, closed_at: datetime, pnl: float, selection_mode: str) -> FilledOptionPosition:
    expiry = opened_at.date() + timedelta(days=30)
    signal = SignalEvent(
        underlying="SPY",
        occurred_at=opened_at,
        direction=SignalDirection.BULLISH,
        signal_name="wf_test",
        strength=0.8,
    )
    long_quote = OptionQuote(
        as_of=opened_at,
        contract_symbol="SPY260327C00650000",
        underlying="SPY",
        expiry=expiry,
        strike=650.0,
        option_type=OptionRight.CALL,
        bid=5.0,
        ask=5.2,
        delta=0.35,
        volume=500,
        open_interest=1000,
        underlying_spot=640.0,
    )
    short_quote = OptionQuote(
        as_of=opened_at,
        contract_symbol="SPY260327C00655000",
        underlying="SPY",
        expiry=expiry,
        strike=655.0,
        option_type=OptionRight.CALL,
        bid=2.4,
        ask=2.6,
        delta=0.20,
        volume=500,
        open_interest=1000,
        underlying_spot=640.0,
    )
    intent = OptionStrategyIntent(
        intent_id=f"wf-{opened_at.isoformat()}-{selection_mode}",
        created_at=opened_at,
        underlying="SPY",
        strategy_family=StrategyFamily.LONG_CALL_DEBIT_SPREAD,
        signal_event=signal,
        entry_snapshot_time=opened_at,
        legs=(
            OptionLeg(action=LegAction.BUY, quantity=1, quote=long_quote),
            OptionLeg(action=LegAction.SELL, quantity=1, quote=short_quote),
        ),
        contracts=1,
        max_loss=2.6,
        profit_target=1.0,
        stop_loss=1.0,
        metadata={"long_leg_selection_mode": selection_mode},
    )
    entry_fill = PackageFill(
        filled_at=opened_at,
        net_price=2.7,
        leg_prices=(5.15, 2.45),
    )
    exit_fill = PackageFill(
        filled_at=closed_at,
        net_price=2.7 + (pnl / 100.0),
        leg_prices=(max(0.0, 5.15 + (pnl / 100.0)), 2.45),
    )
    return FilledOptionPosition(
        position_id=f"pos-{opened_at.isoformat()}",
        intent=intent,
        opened_at=opened_at,
        entry_fill=entry_fill,
        closed_at=closed_at,
        exit_fill=exit_fill,
        exit_reason=ExitReason.FORCED_EXIT,
        status=PositionStatus.CLOSED,
    )


def _make_result(*, start: datetime, end: datetime, pnls: list[float], total_return: float, sharpe: float) -> BacktestResult:
    positions = tuple(
        _make_position(
            opened_at=start + timedelta(hours=index),
            closed_at=start + timedelta(hours=index + 4),
            pnl=pnl,
            selection_mode="delta_band" if index % 2 == 0 else "closest_liquid_otm_fallback",
        )
        for index, pnl in enumerate(pnls)
    )
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
    return BacktestResult(
        strategy_name="wf_test",
        started_at=start,
        finished_at=end,
        positions=positions,
        equity_curve=(
            EquityPoint(timestamp=start, equity=100000.0, cash=100000.0, drawdown=0.0),
            EquityPoint(timestamp=end, equity=100000.0 * (1.0 + total_return), cash=100000.0, drawdown=0.0),
        ),
        metrics={
            "total_return": total_return,
            "sharpe": sharpe,
            "profit_factor": profit_factor,
            "fallback_selection_rate": 0.5 if positions else 0.0,
        },
        config={},
    )


class StubBacktestRunner:
    def __init__(self) -> None:
        self.configs = []

    def run(self, config) -> BacktestResult:  # noqa: ANN001
        self.configs.append(config)
        phase = str(config.metadata.get("walk_forward_phase"))
        if phase == "train":
            return _make_result(start=config.start, end=config.end, pnls=[20.0, 10.0], total_return=0.01, sharpe=1.2)
        return _make_result(start=config.start, end=config.end, pnls=[30.0, 25.0, -10.0], total_return=0.02, sharpe=1.4)


def test_rolling_walk_forward_runner_builds_rolling_windows() -> None:
    runner = RollingWalkForwardRunner(StubBacktestRunner())

    windows = runner.build_windows(
        start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
        config=WalkForwardConfig(
            train_span=timedelta(days=60),
            test_span=timedelta(days=30),
            step_span=timedelta(days=30),
        ),
    )

    assert len(windows) == 4
    assert windows[0].train_start == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert windows[0].test_start == datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)


def test_rolling_walk_forward_runner_aggregates_oos_metrics_and_promotion() -> None:
    runner = RollingWalkForwardRunner(
        StubBacktestRunner(),
        promotion_criteria=PromotionCriteria(
            min_oos_trades=6,
            min_profit_factor=1.5,
            min_sharpe=0.0,
            min_positive_window_ratio=0.5,
            max_fallback_selection_rate=0.6,
        ),
    )

    report = runner.run(
        underlyings=("SPY",),
        start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc),
        config=WalkForwardConfig(
            train_span=timedelta(days=60),
            test_span=timedelta(days=30),
            step_span=timedelta(days=30),
        ),
    )

    assert len(report.windows) == 3
    assert report.oos_metrics["oos_trades"] == 9.0
    assert report.oos_metrics["oos_profit_factor"] > 1.0
    assert report.oos_metrics["positive_window_ratio"] == 1.0
    assert report.promotion.promotable is True
    assert report.underlying_metrics["SPY"]["oos_trades"] == 9.0


def test_rolling_walk_forward_runner_blocks_promotion_when_sharpe_is_too_low() -> None:
    runner = RollingWalkForwardRunner(
        StubBacktestRunner(),
        promotion_criteria=PromotionCriteria(
            min_oos_trades=6,
            min_profit_factor=1.5,
            min_sharpe=1.0,
            min_positive_window_ratio=0.5,
            max_fallback_selection_rate=0.6,
        ),
    )

    report = runner.run(
        underlyings=("SPY",),
        start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc),
        config=WalkForwardConfig(
            train_span=timedelta(days=60),
            test_span=timedelta(days=30),
            step_span=timedelta(days=30),
        ),
    )

    assert report.promotion.promotable is False
    assert "sharpe" in report.promotion.failed_checks


def test_rolling_walk_forward_runner_propagates_backtest_config_overrides() -> None:
    stub_runner = StubBacktestRunner()
    runner = RollingWalkForwardRunner(stub_runner)

    runner.run(
        underlyings=("SPY",),
        start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        config=WalkForwardConfig(
            train_span=timedelta(days=60),
            test_span=timedelta(days=30),
            step_span=timedelta(days=30),
        ),
        backtest_config_overrides={
            "signal_lookback_days": 120,
            "signal_activation_policy": "immediate",
        },
    )

    assert stub_runner.configs
    assert all(config.signal_lookback_days == 120 for config in stub_runner.configs)
    assert all(config.signal_activation_policy == "immediate" for config in stub_runner.configs)
