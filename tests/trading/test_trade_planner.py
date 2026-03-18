from scripts.trading.trade_planner import TradePlanner
from scripts.trading.trade_state import ActiveTrade, TradeStateStore


def _trade(direction: int = 1, units: int = 100, entry_ts: float = 1.0) -> ActiveTrade:
    return ActiveTrade(
        direction=direction,
        units=units,
        entry_ts=entry_ts,
        hold_secs=60,
        max_hold_secs=None,
        entry_price=1.1,
    )


def test_plan_allocation_allows_above_five_when_per_pair_limit_is_higher() -> None:
    store = TradeStateStore()
    store.replace_trades("EUR_USD", [_trade(entry_ts=float(i)) for i in range(5)])
    planner = TradePlanner(store)

    outcome = planner.plan_allocation(
        "EUR_USD",
        now_ts=10.0,
        current_units=500,
        gate_entry_ready=True,
        gate_reasons=[],
        direction="BUY",
        requested_side=1,
        scaled_units_abs=100,
        hold_secs=60,
        max_hold_limit=None,
        signal_key="sig-6",
        hard_blocks=[],
        max_positions_per_pair=6,
        max_total_positions=20,
    )

    assert outcome.gate_entry_ready is True
    assert len(store.get_trades("EUR_USD")) == 6
    assert outcome.target_units == 600


def test_plan_allocation_blocks_when_total_ticket_limit_is_reached() -> None:
    store = TradeStateStore()
    store.replace_trades("EUR_USD", [_trade(entry_ts=1.0)])
    store.replace_trades("GBP_USD", [_trade(entry_ts=2.0)])
    planner = TradePlanner(store)

    outcome = planner.plan_allocation(
        "USD_JPY",
        now_ts=10.0,
        current_units=0,
        gate_entry_ready=True,
        gate_reasons=[],
        direction="BUY",
        requested_side=1,
        scaled_units_abs=100,
        hold_secs=60,
        max_hold_limit=None,
        signal_key="sig-1",
        hard_blocks=[],
        max_positions_per_pair=6,
        max_total_positions=2,
    )

    assert outcome.gate_entry_ready is False
    assert "max_total_positions_reached" in outcome.gate_reasons
    assert store.get_trades("USD_JPY") == []
    assert outcome.target_units == 0
