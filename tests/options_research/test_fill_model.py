from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from options_research.execution import FillPolicy, PackagedSpreadFillSimulator
from options_research.models import (
    ExitReason,
    FilledOptionPosition,
    LegAction,
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionRight,
    OptionStrategyIntent,
    PositionStatus,
    SignalDirection,
    SignalEvent,
    StrategyFamily,
)


def make_signal(direction: SignalDirection = SignalDirection.BULLISH) -> SignalEvent:
    return SignalEvent(
        underlying="SPY",
        occurred_at=datetime(2024, 1, 10, 15, 30, tzinfo=timezone.utc),
        direction=direction,
        signal_name="test_signal",
        strength=0.8,
    )


def make_quote(
    *,
    contract_symbol: str,
    as_of: datetime,
    strike: float,
    bid: float,
    ask: float,
    delta: float,
    option_type: OptionRight = OptionRight.CALL,
) -> OptionQuote:
    return OptionQuote(
        as_of=as_of,
        contract_symbol=contract_symbol,
        underlying="SPY",
        expiry=date(2024, 2, 9),
        strike=strike,
        option_type=option_type,
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


def make_intent(open_time: datetime) -> OptionStrategyIntent:
    signal = make_signal()
    long_quote = make_quote(
        contract_symbol="SPY240209C00485000",
        as_of=open_time,
        strike=485.0,
        bid=5.00,
        ask=5.20,
        delta=0.39,
    )
    short_quote = make_quote(
        contract_symbol="SPY240209C00490000",
        as_of=open_time,
        strike=490.0,
        bid=2.40,
        ask=2.60,
        delta=0.24,
    )
    return OptionStrategyIntent(
        intent_id="intent-fill-model",
        created_at=signal.occurred_at,
        underlying="SPY",
        strategy_family=StrategyFamily.LONG_CALL_DEBIT_SPREAD,
        signal_event=signal,
        entry_snapshot_time=open_time,
        legs=(
            OptionLeg(action=LegAction.BUY, quantity=1, quote=long_quote),
            OptionLeg(action=LegAction.SELL, quantity=1, quote=short_quote),
        ),
        contracts=1,
        max_loss=2.60,
        profit_target=1.00,
        stop_loss=1.00,
    )


def make_snapshot(*quotes: OptionQuote) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="SPY",
        as_of=quotes[0].as_of,
        underlying_spot=quotes[0].underlying_spot or 479.5,
        quotes=quotes,
    )


def test_packaged_fill_simulator_prices_spread_open_close_and_mark() -> None:
    open_time = datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc)
    close_time = datetime(2024, 1, 15, 15, 35, tzinfo=timezone.utc)
    intent = make_intent(open_time)
    open_snapshot = make_snapshot(*(leg.quote for leg in intent.legs))
    close_snapshot = make_snapshot(
        make_quote(
            contract_symbol="SPY240209C00485000",
            as_of=close_time,
            strike=485.0,
            bid=7.10,
            ask=7.30,
            delta=0.47,
        ),
        make_quote(
            contract_symbol="SPY240209C00490000",
            as_of=close_time,
            strike=490.0,
            bid=3.40,
            ask=3.60,
            delta=0.29,
        ),
    )
    simulator = PackagedSpreadFillSimulator(
        FillPolicy(
            price_reference="mid",
            open_penalty_half_spreads=0.50,
            close_penalty_half_spreads=0.50,
            per_contract_commission=0.50,
            per_contract_fee=0.10,
        )
    )

    entry_fill = simulator.fill_open(intent=intent, snapshot=open_snapshot)
    position = FilledOptionPosition(
        position_id="pos-fill-model",
        intent=intent,
        opened_at=open_time,
        entry_fill=entry_fill or pytest.fail("entry fill should succeed"),
    )
    mark_price = simulator.mark_position(position=position, snapshot=close_snapshot)
    exit_fill = simulator.fill_close(
        position=position,
        snapshot=close_snapshot,
        reason=ExitReason.PROFIT_TARGET,
    )

    assert entry_fill is not None
    assert entry_fill.net_price == pytest.approx(2.70)
    assert entry_fill.leg_prices == pytest.approx((5.15, 2.45))
    assert entry_fill.commission == pytest.approx(1.00)
    assert entry_fill.fees == pytest.approx(0.20)
    assert entry_fill.slippage == pytest.approx(0.10)

    assert mark_price == pytest.approx(3.70)

    assert exit_fill is not None
    assert exit_fill.net_price == pytest.approx(3.60)
    assert exit_fill.leg_prices == pytest.approx((7.15, 3.55))
    assert exit_fill.slippage == pytest.approx(0.10)
    assert exit_fill.metadata["exit_reason"] == "profit_target"


def test_packaged_fill_simulator_allows_small_debit_to_close() -> None:
    open_time = datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc)
    close_time = datetime(2024, 1, 11, 15, 35, tzinfo=timezone.utc)
    intent = make_intent(open_time)
    simulator = PackagedSpreadFillSimulator(FillPolicy(price_reference="mid"))
    entry_fill = simulator.fill_open(
        intent=intent,
        snapshot=make_snapshot(*(leg.quote for leg in intent.legs)),
    )
    position = FilledOptionPosition(
        position_id="pos-impossible-close",
        intent=intent,
        opened_at=open_time,
        entry_fill=entry_fill or pytest.fail("entry fill should succeed"),
        status=PositionStatus.OPEN,
    )
    bad_close_snapshot = make_snapshot(
        make_quote(
            contract_symbol="SPY240209C00485000",
            as_of=close_time,
            strike=485.0,
            bid=1.00,
            ask=1.20,
            delta=0.10,
        ),
        make_quote(
            contract_symbol="SPY240209C00490000",
            as_of=close_time,
            strike=490.0,
            bid=1.50,
            ask=1.70,
            delta=0.18,
        ),
    )

    exit_fill = simulator.fill_close(
        position=position,
        snapshot=bad_close_snapshot,
        reason=ExitReason.STOP_LOSS,
    )

    assert exit_fill is not None
    assert exit_fill.net_price == pytest.approx(-0.55)
    assert exit_fill.metadata["exit_reason"] == "stop_loss"
