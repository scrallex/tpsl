from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from options_research.execution import FillPolicy, PackagedSpreadFillSimulator
from options_research.models import (
    ExitReason,
    LegAction,
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionRight,
    OptionStrategyIntent,
    SignalDirection,
    SignalEvent,
    StrategyFamily,
)
from options_research.portfolio import (
    ExitPolicy,
    PortfolioRiskLimits,
    PortfolioState,
    SimplePortfolioEngine,
    SimplePortfolioRiskModel,
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
    gamma: float,
    theta: float,
    vega: float,
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
        gamma=gamma,
        theta=theta,
        vega=vega,
        volume=500,
        open_interest=1000,
        underlying_spot=479.5,
    )


def make_snapshot(*quotes: OptionQuote) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="SPY",
        as_of=quotes[0].as_of,
        underlying_spot=quotes[0].underlying_spot or 479.5,
        quotes=quotes,
    )


def make_intent(open_time: datetime) -> OptionStrategyIntent:
    signal = make_signal()
    return OptionStrategyIntent(
        intent_id="intent-portfolio-engine",
        created_at=signal.occurred_at,
        underlying="SPY",
        strategy_family=StrategyFamily.LONG_CALL_DEBIT_SPREAD,
        signal_event=signal,
        entry_snapshot_time=open_time,
        legs=(
            OptionLeg(
                action=LegAction.BUY,
                quantity=1,
                quote=make_quote(
                    contract_symbol="SPY240209C00485000",
                    as_of=open_time,
                    strike=485.0,
                    bid=5.00,
                    ask=5.20,
                    delta=0.39,
                    gamma=0.02,
                    theta=-0.03,
                    vega=0.08,
                ),
            ),
            OptionLeg(
                action=LegAction.SELL,
                quantity=1,
                quote=make_quote(
                    contract_symbol="SPY240209C00490000",
                    as_of=open_time,
                    strike=490.0,
                    bid=2.40,
                    ask=2.60,
                    delta=0.24,
                    gamma=0.02,
                    theta=-0.02,
                    vega=0.07,
                ),
            ),
        ),
        contracts=1,
        max_loss=2.60,
        profit_target=1.00,
        stop_loss=1.00,
        force_exit_at=open_time + timedelta(days=20),
    )


def build_engine(*, max_holding_period: timedelta | None = None) -> SimplePortfolioEngine:
    return SimplePortfolioEngine(
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
        exit_policy=ExitPolicy(
            max_holding_period=max_holding_period,
            exit_on_signal_reversal=True,
            enforce_force_exit=True,
            exit_at_expiry=True,
        ),
    )


def test_simple_portfolio_engine_opens_marks_and_closes_position() -> None:
    open_time = datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc)
    close_time = datetime(2024, 1, 15, 15, 35, tzinfo=timezone.utc)
    state = PortfolioState(as_of=open_time, cash=10000.0, equity=10000.0)
    intent = make_intent(open_time)
    engine = build_engine()
    open_snapshot = make_snapshot(*(leg.quote for leg in intent.legs))
    close_snapshot = make_snapshot(
        make_quote(
            contract_symbol="SPY240209C00485000",
            as_of=close_time,
            strike=485.0,
            bid=7.10,
            ask=7.30,
            delta=0.47,
            gamma=0.02,
            theta=-0.03,
            vega=0.09,
        ),
        make_quote(
            contract_symbol="SPY240209C00490000",
            as_of=close_time,
            strike=490.0,
            bid=3.40,
            ask=3.60,
            delta=0.29,
            gamma=0.02,
            theta=-0.02,
            vega=0.08,
        ),
    )

    opened_state, position, open_rejection = engine.open_position(
        state=state,
        intent=intent,
        snapshot=open_snapshot,
    )
    exit_eval = engine.evaluate_exit(position=position or pytest.fail("position missing"), snapshot=close_snapshot)
    closed_state, closed_position, close_rejection = engine.close_position(
        state=opened_state,
        position_id=position.position_id if position is not None else "missing",
        snapshot=close_snapshot,
        reason=exit_eval.reason or pytest.fail("expected profit target"),
    )

    assert open_rejection is None
    assert position is not None
    assert opened_state.cash == pytest.approx(9728.80)
    assert opened_state.equity == pytest.approx(9988.80)
    assert opened_state.metadata["unrealized_pnl"] == pytest.approx(-11.20)
    assert opened_state.metadata["portfolio_max_loss"] == pytest.approx(260.0)
    assert opened_state.metadata["underlying_exposure"] == {"SPY": pytest.approx(260.0)}
    assert opened_state.metadata["aggregate_greeks_available"] is True
    assert opened_state.metadata["aggregate_greeks"]["delta"] == pytest.approx(15.0)

    assert exit_eval.reason is ExitReason.PROFIT_TARGET
    assert exit_eval.mark_price == pytest.approx(3.70)

    assert close_rejection is None
    assert closed_position is not None
    assert closed_position.realized_pnl == pytest.approx(87.60)
    assert closed_state.cash == pytest.approx(10087.60)
    assert closed_state.equity == pytest.approx(10087.60)
    assert closed_state.metadata["realized_pnl"] == pytest.approx(87.60)
    assert len(closed_state.open_positions) == 0
    assert len(closed_state.closed_positions) == 1


def test_simple_portfolio_engine_exit_priority_supports_time_stop_and_signal_reversal() -> None:
    open_time = datetime(2024, 1, 10, 15, 35, tzinfo=timezone.utc)
    review_time = datetime(2024, 1, 17, 15, 35, tzinfo=timezone.utc)
    engine = build_engine(max_holding_period=timedelta(days=5))
    state = PortfolioState(as_of=open_time, cash=10000.0, equity=10000.0)
    intent = make_intent(open_time)
    open_snapshot = make_snapshot(*(leg.quote for leg in intent.legs))
    opened_state, position, rejection = engine.open_position(
        state=state,
        intent=intent,
        snapshot=open_snapshot,
    )
    review_snapshot = make_snapshot(
        make_quote(
            contract_symbol="SPY240209C00485000",
            as_of=review_time,
            strike=485.0,
            bid=5.10,
            ask=5.30,
            delta=0.40,
            gamma=0.02,
            theta=-0.03,
            vega=0.08,
        ),
        make_quote(
            contract_symbol="SPY240209C00490000",
            as_of=review_time,
            strike=490.0,
            bid=2.50,
            ask=2.70,
            delta=0.25,
            gamma=0.02,
            theta=-0.02,
            vega=0.07,
        ),
    )
    reversal_signal = make_signal(direction=SignalDirection.BEARISH)

    exit_eval = engine.evaluate_exit(
        position=position or pytest.fail("position missing"),
        snapshot=review_snapshot,
        current_signal=reversal_signal,
    )
    marked_state = engine.mark_to_market(
        state=opened_state,
        snapshots={review_snapshot.underlying: review_snapshot},
        as_of=review_time,
    )

    assert rejection is None
    assert exit_eval.reason is ExitReason.TIME_STOP
    assert exit_eval.mark_price == pytest.approx(2.60)
    assert marked_state.metadata["unrealized_pnl"] == pytest.approx(-11.20)
