from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from options_research.models import (
    ExitReason,
    FilledOptionPosition,
    LegAction,
    OptionChainSnapshot,
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


def make_quote(
    *,
    contract_symbol: str,
    option_type: OptionRight,
    strike: float,
    bid: float,
    ask: float,
    as_of: datetime | None = None,
    expiry: date | None = None,
    underlying: str = "SPY",
    delta: float | None = None,
) -> OptionQuote:
    quote_time = as_of or datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
    return OptionQuote(
        as_of=quote_time,
        contract_symbol=contract_symbol,
        underlying=underlying,
        expiry=expiry or date(2024, 2, 16),
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        delta=delta,
        open_interest=500,
        volume=50,
        underlying_spot=470.0,
    )


def make_signal() -> SignalEvent:
    return SignalEvent(
        underlying="SPY",
        occurred_at=datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc),
        direction=SignalDirection.BULLISH,
        signal_name="test_signal",
        strength=0.75,
    )


def test_option_quote_rejects_crossed_market() -> None:
    with pytest.raises(ValueError, match="ask must be greater than or equal to bid"):
        make_quote(
            contract_symbol="SPY240216C00470000",
            option_type=OptionRight.CALL,
            strike=470.0,
            bid=3.10,
            ask=3.00,
        )


def test_chain_snapshot_rejects_mixed_underlying() -> None:
    spy_quote = make_quote(
        contract_symbol="SPY240216C00470000",
        option_type=OptionRight.CALL,
        strike=470.0,
        bid=3.00,
        ask=3.20,
    )
    qqq_quote = make_quote(
        contract_symbol="QQQ240216C00400000",
        option_type=OptionRight.CALL,
        strike=400.0,
        bid=2.00,
        ask=2.20,
        underlying="QQQ",
    )

    with pytest.raises(ValueError, match="same underlying"):
        OptionChainSnapshot(
            underlying="SPY",
            as_of=datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc),
            underlying_spot=470.0,
            quotes=(spy_quote, qqq_quote),
        )


def test_option_strategy_intent_estimates_debit_for_vertical_spread() -> None:
    signal = make_signal()
    long_leg = OptionLeg(
        action=LegAction.BUY,
        quantity=1,
        quote=make_quote(
            contract_symbol="SPY240216C00470000",
            option_type=OptionRight.CALL,
            strike=470.0,
            bid=4.90,
            ask=5.10,
        ),
    )
    short_leg = OptionLeg(
        action=LegAction.SELL,
        quantity=1,
        quote=make_quote(
            contract_symbol="SPY240216C00475000",
            option_type=OptionRight.CALL,
            strike=475.0,
            bid=2.90,
            ask=3.10,
        ),
    )

    intent = OptionStrategyIntent(
        intent_id="intent-1",
        created_at=signal.occurred_at,
        underlying="SPY",
        strategy_family=StrategyFamily.LONG_CALL_DEBIT_SPREAD,
        signal_event=signal,
        entry_snapshot_time=signal.occurred_at,
        legs=(long_leg, short_leg),
        contracts=2,
        max_loss=2.0,
        profit_target=1.0,
        stop_loss=1.0,
    )

    assert intent.estimated_entry_debit == pytest.approx(2.0)
    assert intent.contract_multiplier == 100
    assert intent.expiry == date(2024, 2, 16)


def test_filled_position_realized_pnl_uses_package_prices_and_costs() -> None:
    signal = make_signal()
    long_leg = OptionLeg(
        action=LegAction.BUY,
        quantity=1,
        quote=make_quote(
            contract_symbol="SPY240216P00470000",
            option_type=OptionRight.PUT,
            strike=470.0,
            bid=5.90,
            ask=6.10,
        ),
    )
    short_leg = OptionLeg(
        action=LegAction.SELL,
        quantity=1,
        quote=make_quote(
            contract_symbol="SPY240216P00465000",
            option_type=OptionRight.PUT,
            strike=465.0,
            bid=3.90,
            ask=4.10,
        ),
    )
    intent = OptionStrategyIntent(
        intent_id="intent-2",
        created_at=signal.occurred_at,
        underlying="SPY",
        strategy_family=StrategyFamily.LONG_PUT_DEBIT_SPREAD,
        signal_event=SignalEvent(
            underlying="SPY",
            occurred_at=signal.occurred_at,
            direction=SignalDirection.BEARISH,
            signal_name="test_signal",
            strength=0.75,
        ),
        entry_snapshot_time=signal.occurred_at,
        legs=(long_leg, short_leg),
        max_loss=2.0,
    )
    entry_fill = PackageFill(
        filled_at=signal.occurred_at,
        net_price=2.0,
        leg_prices=(6.0, 4.0),
        commission=1.0,
        fees=0.0,
    )
    exit_fill = PackageFill(
        filled_at=signal.occurred_at + timedelta(days=3),
        net_price=3.5,
        leg_prices=(7.0, 3.5),
        commission=1.0,
        fees=0.0,
    )

    position = FilledOptionPosition(
        position_id="pos-1",
        intent=intent,
        opened_at=signal.occurred_at,
        entry_fill=entry_fill,
        status=PositionStatus.CLOSED,
        closed_at=signal.occurred_at + timedelta(days=3),
        exit_fill=exit_fill,
        exit_reason=ExitReason.PROFIT_TARGET,
    )

    assert position.entry_cash_spent == pytest.approx(201.0)
    assert position.exit_cash_received == pytest.approx(349.0)
    assert position.realized_pnl == pytest.approx(148.0)
    assert position.holding_period == timedelta(days=3)
