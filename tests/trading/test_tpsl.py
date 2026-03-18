import pytest
from scripts.trading.tpsl import (
    pip_scale,
    TPSLChecker,
    TPSLConfig,
    TPSLTradeState,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING_STOP,
    EXIT_BREAKEVEN_STOP,
)


def test_pip_scale_jpy():
    assert pip_scale("USD_JPY") == 0.01
    assert pip_scale("EUR_JPY") == 0.01
    assert pip_scale("GBP_JPY") == 0.01
    assert pip_scale("AUD_JPY") == 0.01
    assert pip_scale("CAD_JPY") == 0.01


def test_pip_scale_non_jpy():
    assert pip_scale("EUR_USD") == 0.0001
    assert pip_scale("GBP_CAD") == 0.0001
    assert pip_scale("UNKNOWN_PAIR") == 0.0001
    assert pip_scale("AUD_USD") == 0.0001


def test_pip_scale_case_insensitive():
    assert pip_scale("usd_jpy") == 0.01
    assert pip_scale("eur_usd") == 0.0001


def test_tpsl_bracket_long():
    config = TPSLConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
    state = TPSLTradeState(entry_price=100.0, direction=1)

    # Within bounds (No exit)
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 100.5, state, config)
    assert not should_exit

    # Hit Stop Loss
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 98.5, state, config)
    assert should_exit
    assert reason == EXIT_STOP_LOSS
    assert estimate == pytest.approx(99.0)  # 1% below entry

    # Hit Take Profit
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 102.5, state, config)
    assert should_exit
    assert reason == EXIT_TAKE_PROFIT
    assert estimate == pytest.approx(102.0)  # 2% above entry


def test_tpsl_bracket_short():
    config = TPSLConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
    state = TPSLTradeState(entry_price=100.0, direction=-1)

    # Hit Stop Loss (price goes UP)
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 101.5, state, config)
    assert should_exit
    assert reason == EXIT_STOP_LOSS
    assert estimate == pytest.approx(101.0)

    # Hit Take Profit (price goes DOWN)
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 97.5, state, config)
    assert should_exit
    assert reason == EXIT_TAKE_PROFIT
    assert estimate == pytest.approx(98.0)


def test_tpsl_pips():
    # 50 pips = 0.005 for non-jpy, 0.5 for jpy
    config = TPSLConfig(stop_loss_pips=50, take_profit_pips=100)

    state = TPSLTradeState(entry_price=1.1000, direction=1)
    # 50 pips down = 1.0950
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 1.0940, state, config)
    assert should_exit
    assert reason == EXIT_STOP_LOSS
    assert estimate == pytest.approx(1.0950)

    # 100 pips up = 1.1100
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 1.1110, state, config)
    assert should_exit
    assert reason == EXIT_TAKE_PROFIT
    assert estimate == pytest.approx(1.1100)

    state_jpy = TPSLTradeState(entry_price=150.00, direction=1)
    # 50 pips down = 149.50
    should_exit, reason, estimate = TPSLChecker.check(
        "USD_JPY", 149.40, state_jpy, config
    )
    assert should_exit
    assert reason == EXIT_STOP_LOSS
    assert estimate == pytest.approx(149.50)


def test_tpsl_trailing_stop():
    config = TPSLConfig(trailing_stop_pct=0.01)
    state = TPSLTradeState(entry_price=100.0, direction=1)

    # New peak
    TPSLChecker.check("EUR_USD", 105.0, state, config)
    assert state.peak_price == 105.0

    # Within trailing stop (105 * 0.99 = 103.95)
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 104.0, state, config)
    assert not should_exit

    # Hit trailing stop
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 103.0, state, config)
    assert should_exit
    assert reason == EXIT_TRAILING_STOP
    assert estimate == pytest.approx(103.95)


def test_tpsl_breakeven():
    config = TPSLConfig(breakeven_trigger_pct=0.02)
    state = TPSLTradeState(entry_price=100.0, direction=1)

    # Activate BE
    TPSLChecker.check("EUR_USD", 102.5, state, config)
    assert state.breakeven_activated

    # Drop to entry
    should_exit, reason, estimate = TPSLChecker.check("EUR_USD", 100.0, state, config)
    assert should_exit
    assert reason == EXIT_BREAKEVEN_STOP
    assert estimate == pytest.approx(100.0)


def test_tpsl_intra_candle():
    config = TPSLConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
    state = TPSLTradeState(entry_price=100.0, direction=1)

    # Exact hit on low matching SL (zero spread simulation)
    should_exit, reason, estimate = TPSLChecker.check_intra_candle(
        "EUR_USD", 101.0, 99.0, state, config
    )
    assert should_exit
    assert reason == EXIT_STOP_LOSS
    assert estimate == pytest.approx(99.0)

    # Exact hit on TP, SL not hit
    should_exit, reason, estimate = TPSLChecker.check_intra_candle(
        "EUR_USD", 102.0, 99.5, state, config
    )
    assert should_exit
    assert reason == EXIT_TAKE_PROFIT
    assert estimate == pytest.approx(102.0)
