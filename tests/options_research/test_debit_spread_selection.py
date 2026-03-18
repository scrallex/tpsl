from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from options_research.models import OptionChainSnapshot, OptionQuote, OptionRight, SignalDirection, SignalEvent
from options_research.selection import DebitSpreadSelectionConfig, VerticalDebitSpreadSelector
from options_research.strategies import DirectionalExpressionConfig


def make_signal(direction: SignalDirection) -> SignalEvent:
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
    expiry: date,
    strike: float,
    option_type: OptionRight,
    bid: float,
    ask: float,
    delta: float | None,
    volume: int = 500,
    open_interest: int = 1000,
) -> OptionQuote:
    return OptionQuote(
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        contract_symbol=contract_symbol,
        underlying="SPY",
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        delta=delta,
        volume=volume,
        open_interest=open_interest,
        underlying_spot=479.5,
    )


def test_vertical_debit_spread_selector_builds_bullish_call_spread_by_width() -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        underlying_spot=479.5,
        quotes=(
            make_quote(
                contract_symbol="SPY240209C00485000",
                expiry=date(2024, 2, 9),
                strike=485.0,
                option_type=OptionRight.CALL,
                bid=4.90,
                ask=5.10,
                delta=0.39,
            ),
            make_quote(
                contract_symbol="SPY240209C00490000",
                expiry=date(2024, 2, 9),
                strike=490.0,
                option_type=OptionRight.CALL,
                bid=2.40,
                ask=2.60,
                delta=0.24,
            ),
            make_quote(
                contract_symbol="SPY240209C00495000",
                expiry=date(2024, 2, 9),
                strike=495.0,
                option_type=OptionRight.CALL,
                bid=1.25,
                ask=1.45,
                delta=0.15,
            ),
            make_quote(
                contract_symbol="SPY240216C00485000",
                expiry=date(2024, 2, 16),
                strike=485.0,
                option_type=OptionRight.CALL,
                bid=5.40,
                ask=5.80,
                delta=0.40,
            ),
            make_quote(
                contract_symbol="SPY240216C00490000",
                expiry=date(2024, 2, 16),
                strike=490.0,
                option_type=OptionRight.CALL,
                bid=3.10,
                ask=3.40,
                delta=0.27,
            ),
        ),
    )
    selector = VerticalDebitSpreadSelector(
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
            take_profit_pct=0.50,
            stop_loss_pct=0.50,
            close_before_expiry_days=1,
        ),
    )

    outcome = selector.select(
        signal=make_signal(SignalDirection.BULLISH),
        snapshot=snapshot,
    )

    assert outcome.accepted is True
    assert outcome.intent is not None
    assert outcome.intent.strategy_family.value == "long_call_debit_spread"
    assert outcome.intent.legs[0].quote.contract_symbol == "SPY240209C00485000"
    assert outcome.intent.legs[1].quote.contract_symbol == "SPY240209C00490000"
    assert outcome.intent.max_loss == pytest.approx(2.50)
    assert outcome.intent.metadata["selected_width"] == pytest.approx(5.0)
    assert outcome.intent.force_exit_at == datetime(2024, 2, 8, 15, 35, tzinfo=timezone.utc)


def test_vertical_debit_spread_selector_builds_bearish_put_spread_by_delta_offset() -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        underlying_spot=479.5,
        quotes=(
            make_quote(
                contract_symbol="SPY240209P00485000",
                expiry=date(2024, 2, 9),
                strike=485.0,
                option_type=OptionRight.PUT,
                bid=5.10,
                ask=5.30,
                delta=-0.40,
            ),
            make_quote(
                contract_symbol="SPY240209P00480000",
                expiry=date(2024, 2, 9),
                strike=480.0,
                option_type=OptionRight.PUT,
                bid=2.80,
                ask=3.00,
                delta=-0.24,
            ),
            make_quote(
                contract_symbol="SPY240209P00475000",
                expiry=date(2024, 2, 9),
                strike=475.0,
                option_type=OptionRight.PUT,
                bid=1.40,
                ask=1.60,
                delta=-0.12,
            ),
        ),
    )
    selector = VerticalDebitSpreadSelector(
        DebitSpreadSelectionConfig(
            min_dte=21,
            max_dte=45,
            target_dte=30,
            long_delta_min=0.30,
            long_delta_max=0.45,
            spread_width=None,
            short_delta_offset=0.15,
            max_relative_spread=0.10,
            min_open_interest=100,
            min_volume=10,
        )
    )

    outcome = selector.select(
        signal=make_signal(SignalDirection.BEARISH),
        snapshot=snapshot,
    )

    assert outcome.accepted is True
    assert outcome.intent is not None
    assert outcome.intent.strategy_family.value == "long_put_debit_spread"
    assert outcome.intent.legs[0].quote.strike == pytest.approx(485.0)
    assert outcome.intent.legs[1].quote.strike == pytest.approx(480.0)
    assert outcome.intent.metadata["selection_basis"] == "delta_offset"
    assert outcome.intent.metadata["selected_short_delta"] == pytest.approx(0.24)


def test_vertical_debit_spread_selector_rejects_illiquid_or_wide_markets() -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        underlying_spot=479.5,
        quotes=(
            make_quote(
                contract_symbol="SPY240209C00485000",
                expiry=date(2024, 2, 9),
                strike=485.0,
                option_type=OptionRight.CALL,
                bid=4.00,
                ask=6.00,
                delta=0.39,
                volume=3,
                open_interest=40,
            ),
            make_quote(
                contract_symbol="SPY240209C00490000",
                expiry=date(2024, 2, 9),
                strike=490.0,
                option_type=OptionRight.CALL,
                bid=2.00,
                ask=3.50,
                delta=0.24,
                volume=3,
                open_interest=40,
            ),
        ),
    )
    selector = VerticalDebitSpreadSelector(
        DebitSpreadSelectionConfig(
            min_dte=21,
            max_dte=45,
            spread_width=5.0,
            max_relative_spread=0.15,
            min_open_interest=100,
            min_volume=10,
        )
    )

    outcome = selector.select(
        signal=make_signal(SignalDirection.BULLISH),
        snapshot=snapshot,
    )

    assert outcome.accepted is False
    assert outcome.rejection_reason == "no_liquid_long_leg_candidates"


def test_vertical_debit_spread_selector_falls_back_to_closest_liquid_otm_when_delta_is_missing() -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        underlying_spot=479.5,
        quotes=(
            make_quote(
                contract_symbol="SPY240209C00475000",
                expiry=date(2024, 2, 9),
                strike=475.0,
                option_type=OptionRight.CALL,
                bid=6.90,
                ask=7.10,
                delta=None,
            ),
            make_quote(
                contract_symbol="SPY240209C00480000",
                expiry=date(2024, 2, 9),
                strike=480.0,
                option_type=OptionRight.CALL,
                bid=4.90,
                ask=5.10,
                delta=None,
            ),
            make_quote(
                contract_symbol="SPY240209C00485000",
                expiry=date(2024, 2, 9),
                strike=485.0,
                option_type=OptionRight.CALL,
                bid=2.40,
                ask=2.60,
                delta=None,
            ),
        ),
    )
    selector = VerticalDebitSpreadSelector(
        DebitSpreadSelectionConfig(
            min_dte=21,
            max_dte=45,
            target_dte=30,
            spread_width=5.0,
            max_relative_spread=0.10,
            min_open_interest=100,
            min_volume=10,
            allow_moneyness_fallback=True,
        )
    )

    outcome = selector.select(
        signal=make_signal(SignalDirection.BULLISH),
        snapshot=snapshot,
    )

    assert outcome.accepted is True
    assert outcome.intent is not None
    assert outcome.intent.legs[0].quote.contract_symbol == "SPY240209C00480000"
    assert outcome.intent.legs[1].quote.contract_symbol == "SPY240209C00485000"
    assert outcome.intent.metadata["long_leg_selection_mode"] == "closest_liquid_otm_fallback"


def test_vertical_debit_spread_selector_can_require_delta_and_reject_missing_greeks() -> None:
    snapshot = OptionChainSnapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc),
        underlying_spot=479.5,
        quotes=(
            make_quote(
                contract_symbol="SPY240209C00480000",
                expiry=date(2024, 2, 9),
                strike=480.0,
                option_type=OptionRight.CALL,
                bid=4.90,
                ask=5.10,
                delta=None,
            ),
            make_quote(
                contract_symbol="SPY240209C00485000",
                expiry=date(2024, 2, 9),
                strike=485.0,
                option_type=OptionRight.CALL,
                bid=2.40,
                ask=2.60,
                delta=None,
            ),
        ),
    )
    selector = VerticalDebitSpreadSelector(
        DebitSpreadSelectionConfig(
            min_dte=21,
            max_dte=45,
            target_dte=30,
            spread_width=5.0,
            max_relative_spread=0.10,
            min_open_interest=100,
            min_volume=10,
            allow_moneyness_fallback=False,
        )
    )

    outcome = selector.select(
        signal=make_signal(SignalDirection.BULLISH),
        snapshot=snapshot,
    )

    assert outcome.accepted is False
    assert outcome.rejection_reason == "no_delta_long_leg_candidates"
