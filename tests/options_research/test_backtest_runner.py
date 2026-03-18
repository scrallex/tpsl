from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence

import pytest

from options_research.backtest import BacktestConfig, SignalDrivenBacktestRunner
from options_research.data import DataRequest
from options_research.execution import FillPolicy, PackagedSpreadFillSimulator
from options_research.models import (
    CorporateAction,
    OptionChainSnapshot,
    OptionQuote,
    OptionRight,
    SignalDirection,
    SignalEvent,
    UnderlyingBar,
)
from options_research.portfolio import (
    ExitPolicy,
    PortfolioRiskLimits,
    SimplePortfolioEngine,
    SimplePortfolioRiskModel,
)
from options_research.selection import DebitSpreadSelectionConfig, VerticalDebitSpreadSelector
from options_research.signals import MovingAverageSignalConfig, MovingAverageSignalGenerator
from options_research.strategies import DirectionalExpressionConfig


@dataclass
class StubHistoricalOptionsDataSource:
    bars: tuple[UnderlyingBar, ...]
    snapshots: tuple[OptionChainSnapshot, ...]

    def load_underlying_bars(self, request: DataRequest) -> Sequence[UnderlyingBar]:
        return tuple(bar for bar in self.bars if request.start <= bar.timestamp <= request.end)

    def iter_option_chain_snapshots(self, request: DataRequest) -> Sequence[OptionChainSnapshot]:
        return tuple(snapshot for snapshot in self.snapshots if request.start <= snapshot.as_of <= request.end)

    def load_option_chain_snapshot(
        self,
        *,
        underlying: str,
        as_of: datetime,
    ) -> OptionChainSnapshot | None:
        eligible = [
            snapshot for snapshot in self.snapshots if snapshot.underlying == underlying and snapshot.as_of <= as_of
        ]
        if not eligible:
            return None
        return eligible[-1]

    def load_corporate_actions(self, request: DataRequest) -> Sequence[CorporateAction]:
        return ()


@dataclass
class StubSignalGenerator:
    signals: tuple[SignalEvent, ...]

    def generate(self, context) -> Sequence[SignalEvent]:  # noqa: ANN001
        return self.signals


def make_bar(*, timestamp: datetime, close: float) -> UnderlyingBar:
    return UnderlyingBar(
        symbol="SPY",
        timestamp=timestamp,
        open=close - 0.20,
        high=close + 0.30,
        low=close - 0.40,
        close=close,
        volume=1000,
    )


def make_quote(
    *,
    timestamp: datetime,
    contract_symbol: str,
    strike: float,
    bid: float,
    ask: float,
    delta: float,
) -> OptionQuote:
    return OptionQuote(
        as_of=timestamp,
        contract_symbol=contract_symbol,
        underlying="SPY",
        expiry=date(2024, 2, 9),
        strike=strike,
        option_type=OptionRight.CALL,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=0.02,
        theta=-0.03 if strike == 485.0 else -0.02,
        vega=0.08 if strike == 485.0 else 0.07,
        volume=500,
        open_interest=1000,
        underlying_spot=479.5,
    )


def make_snapshot(timestamp: datetime, long_bid: float, long_ask: float, short_bid: float, short_ask: float) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="SPY",
        as_of=timestamp,
        underlying_spot=479.5,
        quotes=(
            make_quote(
                timestamp=timestamp,
                contract_symbol="SPY240209C00485000",
                strike=485.0,
                bid=long_bid,
                ask=long_ask,
                delta=0.39 if long_bid < 6.0 else 0.47,
            ),
            make_quote(
                timestamp=timestamp,
                contract_symbol="SPY240209C00490000",
                strike=490.0,
                bid=short_bid,
                ask=short_ask,
                delta=0.24 if short_bid < 3.0 else 0.29,
            ),
        ),
    )


def test_signal_driven_backtest_runner_executes_end_to_end_fixture() -> None:
    start = datetime(2024, 1, 10, 14, 30, tzinfo=timezone.utc)
    bars = (
        make_bar(timestamp=start, close=100.0),
        make_bar(timestamp=datetime(2024, 1, 10, 14, 35, tzinfo=timezone.utc), close=100.1),
        make_bar(timestamp=datetime(2024, 1, 10, 14, 40, tzinfo=timezone.utc), close=100.2),
        make_bar(timestamp=datetime(2024, 1, 10, 14, 45, tzinfo=timezone.utc), close=101.0),
        make_bar(timestamp=datetime(2024, 1, 10, 14, 50, tzinfo=timezone.utc), close=101.5),
    )
    snapshots = (
        make_snapshot(
            datetime(2024, 1, 10, 14, 45, tzinfo=timezone.utc),
            long_bid=5.00,
            long_ask=5.20,
            short_bid=2.40,
            short_ask=2.60,
        ),
        make_snapshot(
            datetime(2024, 1, 10, 14, 50, tzinfo=timezone.utc),
            long_bid=5.00,
            long_ask=5.20,
            short_bid=2.40,
            short_ask=2.60,
        ),
        make_snapshot(
            datetime(2024, 1, 10, 14, 55, tzinfo=timezone.utc),
            long_bid=7.10,
            long_ask=7.30,
            short_bid=3.40,
            short_ask=3.60,
        ),
    )
    runner = SignalDrivenBacktestRunner(
        data_source=StubHistoricalOptionsDataSource(bars=bars, snapshots=snapshots),
        signal_generator=MovingAverageSignalGenerator(
            MovingAverageSignalConfig(short_window=2, long_window=3, min_gap_pct=0.001)
        ),
        selector=VerticalDebitSpreadSelector(
            DebitSpreadSelectionConfig(
                min_dte=21,
                max_dte=45,
                target_dte=30,
                long_delta_min=0.30,
                long_delta_max=0.45,
                spread_width=5.0,
                short_delta_offset=0.15,
                max_relative_spread=0.10,
                min_open_interest=100,
                min_volume=10,
            ),
            expression_config=DirectionalExpressionConfig(
                contracts=1,
                take_profit_pct=0.25,
                stop_loss_pct=0.50,
                close_before_expiry_days=1,
            ),
        ),
        portfolio_engine=SimplePortfolioEngine(
            fill_simulator=PackagedSpreadFillSimulator(
                FillPolicy(
                    price_reference="mid",
                    open_penalty_half_spreads=0.50,
                    close_penalty_half_spreads=0.50,
                    per_contract_commission=0.50,
                    per_contract_fee=0.10,
                )
            ),
            risk_model=SimplePortfolioRiskModel(
                PortfolioRiskLimits(
                    max_open_positions=5,
                    max_portfolio_max_loss=0.50,
                    max_underlying_allocation=0.50,
                    max_new_position_max_loss=0.10,
                )
            ),
            exit_policy=ExitPolicy(),
        ),
    )
    config = BacktestConfig(
        underlying="SPY",
        start=start,
        end=datetime(2024, 1, 10, 14, 55, tzinfo=timezone.utc),
        initial_capital=10000.0,
        signal_activation_policy="immediate",
    )

    result = runner.run(config)

    assert result.total_trades == 1
    assert result.rejected_entries == 0
    assert len(result.positions) == 1
    assert result.positions[0].exit_reason.value == "profit_target"
    assert result.positions[0].realized_pnl == pytest.approx(87.60)
    assert result.metrics["total_return"] == pytest.approx(0.00876, rel=1e-3)
    assert result.metrics["win_rate"] == pytest.approx(1.0)
    assert result.metrics["profit_factor"] == float("inf")
    assert result.metrics["fill_rejection_rate"] == pytest.approx(0.0)
    assert result.metrics["attempted_entries"] == pytest.approx(1.0)
    assert result.strategy_summaries["long_call_debit_spread"]["total_pnl"] == pytest.approx(87.60)
    assert result.underlying_summaries["SPY"]["trades"] == pytest.approx(1.0)
    assert result.config["signals_generated"] == 1
    assert result.config["snapshots_loaded"] == 3
    assert result.notes == ()


def test_signal_driven_backtest_runner_forbids_same_snapshot_fill_by_default() -> None:
    signal_time = datetime(2024, 1, 10, 21, 0, tzinfo=timezone.utc)
    next_snapshot_time = datetime(2024, 1, 11, 21, 0, tzinfo=timezone.utc)
    runner = SignalDrivenBacktestRunner(
        data_source=StubHistoricalOptionsDataSource(
            bars=(
                make_bar(timestamp=datetime(2024, 1, 10, 5, 0, tzinfo=timezone.utc), close=470.0),
                make_bar(timestamp=datetime(2024, 1, 11, 5, 0, tzinfo=timezone.utc), close=471.0),
            ),
            snapshots=(
                make_snapshot(signal_time, long_bid=5.00, long_ask=5.20, short_bid=2.40, short_ask=2.60),
                make_snapshot(next_snapshot_time, long_bid=5.10, long_ask=5.30, short_bid=2.50, short_ask=2.70),
            ),
        ),
        signal_generator=StubSignalGenerator(
            signals=(
                SignalEvent(
                    underlying="SPY",
                    occurred_at=signal_time,
                    direction=SignalDirection.BULLISH,
                    signal_name="sep_regime_test",
                    strength=0.9,
                ),
            )
        ),
        selector=VerticalDebitSpreadSelector(
            DebitSpreadSelectionConfig(
                min_dte=21,
                max_dte=45,
                target_dte=30,
                long_delta_min=0.30,
                long_delta_max=0.45,
                spread_width=5.0,
                short_delta_offset=0.15,
                max_relative_spread=0.10,
                min_open_interest=100,
                min_volume=10,
            ),
            expression_config=DirectionalExpressionConfig(
                contracts=1,
                take_profit_pct=0.25,
                stop_loss_pct=0.50,
                close_before_expiry_days=1,
            ),
        ),
        portfolio_engine=SimplePortfolioEngine(
            fill_simulator=PackagedSpreadFillSimulator(
                FillPolicy(
                    price_reference="mid",
                    open_penalty_half_spreads=0.50,
                    close_penalty_half_spreads=0.50,
                    per_contract_commission=0.50,
                    per_contract_fee=0.10,
                )
            ),
            risk_model=SimplePortfolioRiskModel(
                PortfolioRiskLimits(
                    max_open_positions=5,
                    max_portfolio_max_loss=0.50,
                    max_underlying_allocation=0.50,
                    max_new_position_max_loss=0.10,
                )
            ),
            exit_policy=ExitPolicy(),
        ),
    )

    result = runner.run(
        BacktestConfig(
            underlying="SPY",
            start=datetime(2024, 1, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 1, 11, 23, 59, tzinfo=timezone.utc),
            initial_capital=10000.0,
        )
    )

    assert result.total_trades == 1
    assert result.positions[0].opened_at == next_snapshot_time
    assert result.config["signal_activation_policy"] == "next_snapshot"
    assert result.config["tradable_signals"] == 1


def test_signal_driven_backtest_runner_forbids_same_day_daily_bar_entry() -> None:
    same_day_signal_time = datetime(2024, 1, 10, 5, 0, tzinfo=timezone.utc)
    same_day_snapshot_time = datetime(2024, 1, 10, 21, 0, tzinfo=timezone.utc)
    next_day_snapshot_time = datetime(2024, 1, 11, 21, 0, tzinfo=timezone.utc)
    runner = SignalDrivenBacktestRunner(
        data_source=StubHistoricalOptionsDataSource(
            bars=(
                make_bar(timestamp=datetime(2024, 1, 10, 5, 0, tzinfo=timezone.utc), close=470.0),
                make_bar(timestamp=datetime(2024, 1, 11, 5, 0, tzinfo=timezone.utc), close=471.0),
                make_bar(timestamp=datetime(2024, 1, 12, 5, 0, tzinfo=timezone.utc), close=472.0),
            ),
            snapshots=(
                make_snapshot(same_day_snapshot_time, long_bid=5.00, long_ask=5.20, short_bid=2.40, short_ask=2.60),
                make_snapshot(next_day_snapshot_time, long_bid=5.10, long_ask=5.30, short_bid=2.50, short_ask=2.70),
            ),
        ),
        signal_generator=StubSignalGenerator(
            signals=(
                SignalEvent(
                    underlying="SPY",
                    occurred_at=same_day_signal_time,
                    direction=SignalDirection.BULLISH,
                    signal_name="daily_bar_signal",
                    strength=0.9,
                ),
            )
        ),
        selector=VerticalDebitSpreadSelector(
            DebitSpreadSelectionConfig(
                min_dte=21,
                max_dte=45,
                target_dte=30,
                long_delta_min=0.30,
                long_delta_max=0.45,
                spread_width=5.0,
                short_delta_offset=0.15,
                max_relative_spread=0.10,
                min_open_interest=100,
                min_volume=10,
            ),
            expression_config=DirectionalExpressionConfig(
                contracts=1,
                take_profit_pct=0.25,
                stop_loss_pct=0.50,
                close_before_expiry_days=1,
            ),
        ),
        portfolio_engine=SimplePortfolioEngine(
            fill_simulator=PackagedSpreadFillSimulator(
                FillPolicy(
                    price_reference="mid",
                    open_penalty_half_spreads=0.50,
                    close_penalty_half_spreads=0.50,
                    per_contract_commission=0.50,
                    per_contract_fee=0.10,
                )
            ),
            risk_model=SimplePortfolioRiskModel(
                PortfolioRiskLimits(
                    max_open_positions=5,
                    max_portfolio_max_loss=0.50,
                    max_underlying_allocation=0.50,
                    max_new_position_max_loss=0.10,
                )
            ),
            exit_policy=ExitPolicy(),
        ),
    )

    result = runner.run(
        BacktestConfig(
            underlying="SPY",
            start=datetime(2024, 1, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 1, 11, 23, 59, tzinfo=timezone.utc),
            initial_capital=10000.0,
        )
    )

    assert result.total_trades == 1
    assert result.positions[0].opened_at == next_day_snapshot_time
