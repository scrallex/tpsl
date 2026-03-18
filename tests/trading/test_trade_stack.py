import math
from datetime import datetime, timezone

from scripts.trading.execution_engine import ExecutionEngine
from scripts.trading.gate_loader import StrategyInstrument, StrategyProfile
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.risk_limits import RiskLimits, RiskManager
from scripts.trading.session_policy import SessionWindow
from scripts.trading.session_policy import SessionPolicy
from scripts.trading.trade_planner import TradePlanner
from scripts.trading.trade_stack import TradeStackProcessor
from scripts.trading.trade_state import TradeStateStore


class DummyTracker:
    price_cache: dict[str, dict[str, float]] = {}

    def __init__(self) -> None:
        self.executed: list[tuple[str, int, float]] = []
        self.synced: list[tuple[str, int, float, bool]] = []

    def execute_delta(self, instrument: str, delta_units: int, current_price: float, **_: float) -> bool:
        self.executed.append((instrument, delta_units, current_price))
        return True

    def has_position(self, instrument: str) -> bool:
        return False

    def get_tickets(self, instrument: str) -> list[object]:
        return []

    def close_ticket(self, *args: object, **kwargs: object) -> bool:
        return False

    def net_units(self, instrument: str) -> int:
        return 0

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


def _build_stack(allow_fallback: bool = False) -> tuple[TradeStackProcessor, TradeStateStore, RiskManager]:
    instrument = StrategyInstrument(
        symbol="EUR_USD",
        hazard_max=None,
        hazard_min=0.8,
        min_repetitions=1,
        guards={
            "min_coherence": 0.0,
            "min_stability": 0.0,
            "max_entropy": 5.0,
            "max_coherence_tau_slope": None,
            "max_domain_wall_slope": None,
            "min_low_freq_share": None,
            "max_reynolds_ratio": None,
            "min_temporal_half_life": None,
            "min_spatial_corr_length": None,
            "min_pinned_alignment": None,
        },
        session=SessionWindow.from_spec({"start": "00:00Z", "end": "23:59Z"}),
        invert_bundles=True,
        ml_primary_gate=True,
        allow_fallback=allow_fallback,
        disable_bundle_overrides=True,
        stop_loss_pct=0.003,
        take_profit_pct=0.006,
    )
    strategy = StrategyProfile(
        instruments={"EUR_USD": instrument},
        global_defaults={},
        bundle_defaults={},
    )
    trade_state = TradeStateStore()
    risk_manager = RiskManager(RiskLimits())
    risk_manager.set_nav(1_000.0)
    execution_engine = ExecutionEngine(
        risk_manager=risk_manager,
        trade_state=trade_state,
        risk_sizer=RiskSizer(
            nav_risk_pct=0.05,
            per_position_pct_cap=0.05,
            alloc_top_k=3,
        ),
        trade_planner=TradePlanner(trade_state),
    )
    stack = TradeStackProcessor(
        strategy=strategy,
        session_policy=SessionPolicy({"EUR_USD": instrument.session}, exit_buffer_minutes=5),
        risk_manager=risk_manager,
        trade_state=trade_state,
        risk_sizer=RiskSizer(
            nav_risk_pct=0.05,
            per_position_pct_cap=0.05,
            alloc_top_k=3,
        ),
        execution_engine=execution_engine,
        hold_seconds=1800,
    )
    return stack, trade_state, risk_manager


def test_directional_non_bundle_gate_trades_when_fallback_disabled() -> None:
    stack, trade_state, risk_manager = _build_stack(allow_fallback=False)
    tracker = DummyTracker()

    gate_info = {
        "instrument": "EUR_USD",
        "ts_ms": 1,
        "admit": 1,
        "direction": "BUY",
        "hazard": 0.9,
        "repetitions": 1,
        "structure": {"coherence": 0.3, "entropy": 1.0, "stability": 0.0},
        "st_peak": True,
        "reasons": [],
    }

    stack.process_instrument(
        instrument="EUR_USD",
        gate_info=gate_info,
        price_data={"mid": 1.1, "bid": 1.0999, "ask": 1.1001},
        per_trade_exposure=50.0,
        nav_snapshot=1_000.0,
        price_cache={},
        tracker=tracker,
    )

    trades = trade_state.get_trades("EUR_USD")
    assert len(trades) == 1
    assert trades[0].direction == -1
    assert tracker.executed
    assert risk_manager.net_units("EUR_USD") == 0


def test_flat_non_bundle_gate_does_not_trade_when_fallback_disabled() -> None:
    stack, trade_state, _ = _build_stack(allow_fallback=False)
    tracker = DummyTracker()

    gate_info = {
        "instrument": "EUR_USD",
        "ts_ms": 1,
        "admit": 1,
        "direction": "FLAT",
        "hazard": 0.9,
        "repetitions": 1,
        "structure": {"coherence": 0.3, "entropy": 1.0, "stability": 0.0},
        "st_peak": True,
        "reasons": [],
    }

    stack.process_instrument(
        instrument="EUR_USD",
        gate_info=gate_info,
        price_data={"mid": 1.1, "bid": 1.0999, "ask": 1.1001},
        per_trade_exposure=50.0,
        nav_snapshot=1_000.0,
        price_cache={},
        tracker=tracker,
    )

    assert trade_state.get_trades("EUR_USD") == []
    assert tracker.executed == []


def test_fixed_stop_profile_uses_scalar_sizing_to_match_backtest() -> None:
    stack, trade_state, _ = _build_stack(allow_fallback=False)
    tracker = DummyTracker()

    gate_info = {
        "instrument": "EUR_USD",
        "ts_ms": 2,
        "admit": 1,
        "direction": "BUY",
        "hazard": 0.91,
        "repetitions": 2,
        "structure": {"coherence": 0.4, "entropy": 1.0, "stability": 0.0},
        "st_peak": True,
        "reasons": [],
    }

    stack.process_instrument(
        instrument="EUR_USD",
        gate_info=gate_info,
        price_data={"mid": 1.1, "bid": 1.0999, "ask": 1.1001},
        per_trade_exposure=50.0,
        nav_snapshot=1_000.0,
        price_cache={},
        tracker=tracker,
    )

    trades = trade_state.get_trades("EUR_USD")
    assert len(trades) == 1
    expected_units = int(50.0 / (1.1 * 0.02))
    assert trades[0].units == expected_units
    assert tracker.executed[0][1] == -expected_units
    stop_loss_price = 1.1 * (1.0 - (0.003 * -1))
    r_sized_units, _ = stack.risk_sizer.target_position_size_for_r(
        instrument="EUR_USD",
        nav_snapshot=1_000.0,
        entry_price=1.1,
        stop_loss_price=stop_loss_price,
        auxiliary_prices={},
    )
    assert not math.isclose(trades[0].units, r_sized_units)
