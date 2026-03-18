from datetime import datetime, timezone

from scripts.trading.execution_engine import ExecutionEngine
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.risk_limits import RiskLimits, RiskManager
from scripts.trading.trade_planner import TradePlanner
from scripts.trading.trade_state import TradeStateStore


class DummyTracker:
    def __init__(self) -> None:
        self.synced = []

    def has_position(self, instrument: str) -> bool:
        return False

    def sync_to_net_position(
        self,
        instrument: str,
        target_units: int,
        price: float,
        timestamp: datetime,
        *,
        is_bundle: bool = False,
    ) -> None:
        self.synced.append((instrument, target_units, price, is_bundle))


def _build_engine() -> tuple[ExecutionEngine, RiskManager, TradeStateStore]:
    risk_manager = RiskManager(RiskLimits())
    trade_state = TradeStateStore()
    engine = ExecutionEngine(
        risk_manager=risk_manager,
        trade_state=trade_state,
        risk_sizer=RiskSizer(
            nav_risk_pct=0.01,
            per_position_pct_cap=0.01,
            alloc_top_k=3,
        ),
        trade_planner=TradePlanner(trade_state),
    )
    return engine, risk_manager, trade_state


def test_execute_allocation_restores_trade_state_on_failed_execution() -> None:
    engine, risk_manager, trade_state = _build_engine()
    tracker = DummyTracker()

    engine.execute_allocation(
        instrument="EUR_USD",
        now_ts=1.0,
        gate_entry_ready=True,
        gate_reasons=[],
        direction="BUY",
        requested_side=1,
        scaled_units_abs=100,
        hold_secs=60,
        signal_key="sig-1",
        hard_blocks=[],
        current_price=1.1,
        timestamp=datetime.now(timezone.utc),
        is_bundle_entry=False,
        execute_callback=lambda *args, **kwargs: False,
        tracker=tracker,
    )

    assert risk_manager.net_units("EUR_USD") == 0
    assert trade_state.get_trades("EUR_USD") == []
    assert tracker.synced == []


def test_execute_allocation_syncs_tracker_after_confirmed_fill() -> None:
    engine, risk_manager, trade_state = _build_engine()
    tracker = DummyTracker()

    def execute_callback(instrument: str, delta_units: int, current_price: float, **_: float) -> bool:
        risk_manager.record_fill(instrument, delta_units, current_price)
        return True

    engine.execute_allocation(
        instrument="EUR_USD",
        now_ts=1.0,
        gate_entry_ready=True,
        gate_reasons=[],
        direction="BUY",
        requested_side=1,
        scaled_units_abs=100,
        hold_secs=60,
        signal_key="sig-1",
        hard_blocks=[],
        current_price=1.1,
        timestamp=datetime.now(timezone.utc),
        is_bundle_entry=True,
        execute_callback=execute_callback,
        tracker=tracker,
    )

    assert risk_manager.net_units("EUR_USD") == 100
    assert len(trade_state.get_trades("EUR_USD")) == 1
    assert tracker.synced == [("EUR_USD", 100, 1.1, True)]
