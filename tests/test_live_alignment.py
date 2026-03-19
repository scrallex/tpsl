from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.trading.exposure_tracker import ExposureTracker
from scripts.research.simulator.gate_cache import gate_cache_path_for
from scripts.tools.export_optimal_trades import _build_simulation_params
from scripts.trading.gate_loader import StrategyInstrument
from scripts.trading.oanda import OandaConnector
from scripts.trading.portfolio_manager import PortfolioConfig, PortfolioLoopCoordinator
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.risk_limits import RiskLimits, RiskManager
from scripts.trading.session_policy import SessionPolicy, SessionWindow
from scripts.trading.trade_stack import TradeStackProcessor
from scripts.trading.trade_state import ActiveTrade, TradeStateStore


class _StrategyStub:
    def __init__(self, profile: StrategyInstrument) -> None:
        self._profile = profile

    def get(self, symbol: str) -> StrategyInstrument:
        return self._profile


@dataclass
class _Decision:
    tradable: bool = True
    reason: str = "ok"


class _SessionPolicyStub:
    def evaluate(self, instrument: str, now, has_position: bool) -> _Decision:
        return _Decision()


class _FakeEngine:
    def __init__(self, trade_state: TradeStateStore) -> None:
        self.trade_state = trade_state
        self.calls = []

    def execute_allocation(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if not kwargs["gate_entry_ready"]:
            return
        inst = kwargs["instrument"]
        trades = self.trade_state.get_trades(inst)
        trades.append(
            ActiveTrade(
                direction=kwargs["requested_side"],
                units=max(1, int(kwargs["scaled_units_abs"])),
                entry_ts=float(kwargs["now_ts"]),
                hold_secs=int(kwargs["hold_secs"]),
                max_hold_secs=None,
                entry_price=float(kwargs["current_price"]),
            )
        )
        self.trade_state.replace_trades(inst, trades)


class _TrackerStub:
    def execute_delta(self, instrument: str, delta_units: int, mid_price: float, **kwargs):
        return True


class _FakeService:
    def __init__(self) -> None:
        self.enabled_pairs = ["AUD_USD", "EUR_USD", "GBP_USD"]
        self.risk_manager = RiskManager(
            RiskLimits(max_positions_per_pair=5, max_total_positions=32)
        )
        self.state_manager = SimpleNamespace(_valkey_client=None)

    def get_oanda_positions(self):
        return [
            {
                "instrument": "AUD_USD",
                "long": {"units": "399", "averagePrice": "0.70590"},
                "short": {"units": "0"},
            },
            {
                "instrument": "EUR_USD",
                "long": {"units": "246", "averagePrice": "1.14776"},
                "short": {"units": "0"},
            },
        ]

    def get_oanda_open_trades(self, instruments=None):
        return [
            {
                "id": "1",
                "instrument": "AUD_USD",
                "currentUnits": "210",
                "price": "0.70550",
                "openTime": "2026-03-18T19:40:00Z",
            },
            {
                "id": "2",
                "instrument": "AUD_USD",
                "currentUnits": "189",
                "price": "0.70634",
                "openTime": "2026-03-18T19:41:05Z",
            },
            {
                "id": "3",
                "instrument": "EUR_USD",
                "currentUnits": "246",
                "price": "1.14776",
                "openTime": "2026-03-18T19:42:10Z",
            },
        ]


def test_reconcile_portfolio_rebuilds_tracker_and_trade_state():
    service = _FakeService()
    config = PortfolioConfig(
        profile_path=Path("/sep/tpsl/config/mean_reversion_strategy.yaml"),
        exit_buffer_minutes=5,
        nav_risk_pct=0.01,
        per_pos_pct=0.01,
        alloc_top_k=32,
        redis_url=None,
        hold_seconds=1800,
        loop_seconds=2.0,
        reconcile_seconds=0.0,
    )
    coordinator = PortfolioLoopCoordinator(service, config)

    stale_ts = datetime(2026, 3, 18, 19, 35, tzinfo=timezone.utc)
    coordinator.exposure_tracker.open_position(
        "GBP_USD", 210, 1.33359, stale_ts, is_bundle=False
    )
    coordinator.risk_manager.record_fill("GBP_USD", 210, 1.33359)
    coordinator.trade_state.replace_trades(
        "GBP_USD",
        [
            ActiveTrade(
                direction=1,
                units=210,
                entry_ts=stale_ts.timestamp(),
                hold_secs=3600,
                max_hold_secs=None,
                entry_price=1.33359,
            )
        ],
    )
    coordinator.trade_stack.restore_entry_cooldown("GBP_USD", stale_ts.timestamp())

    coordinator.reconcile_portfolio()

    assert coordinator.risk_manager.positions() == {"AUD_USD": 399, "EUR_USD": 246}
    assert [ticket.units for ticket in coordinator.exposure_tracker.get_tickets("AUD_USD")] == [210, 189]
    assert [ticket.units for ticket in coordinator.exposure_tracker.get_tickets("EUR_USD")] == [246]
    assert coordinator.exposure_tracker.get_tickets("GBP_USD") == []

    assert coordinator.trade_state.trade_count("AUD_USD") == 2
    assert coordinator.trade_state.trade_count("EUR_USD") == 1
    assert coordinator.trade_state.trade_count("GBP_USD") == 0

    assert coordinator.trade_stack._last_entry_ts["AUD_USD"] == datetime(
        2026, 3, 18, 19, 41, 5, tzinfo=timezone.utc
    ).timestamp()


def test_trade_stack_enforces_backtest_entry_cooldown(monkeypatch):
    profile = StrategyInstrument(
        symbol="EUR_USD",
        hazard_max=None,
        hazard_min=0.0,
        min_repetitions=1,
        guards={key: None for key in (
            "min_coherence",
            "min_stability",
            "max_entropy",
            "max_coherence_tau_slope",
            "max_domain_wall_slope",
            "min_low_freq_share",
            "max_reynolds_ratio",
            "min_temporal_half_life",
            "min_spatial_corr_length",
            "min_pinned_alignment",
        )},
        session=SessionWindow.from_spec({"start": "00:00Z", "end": "23:59Z"}),
        allow_fallback=False,
        hold_minutes=10,
    )
    trade_state = TradeStateStore()
    risk_manager = RiskManager(RiskLimits(max_positions_per_pair=5, max_total_positions=32))
    risk_sizer = RiskSizer(nav_risk_pct=0.01, per_position_pct_cap=0.01, alloc_top_k=32)
    engine = _FakeEngine(trade_state)
    processor = TradeStackProcessor(
        _StrategyStub(profile),
        _SessionPolicyStub(),
        risk_manager,
        trade_state,
        risk_sizer,
        engine,
        hold_seconds=600,
    )

    timestamps = iter([1_000.0, 1_030.0])
    monkeypatch.setattr("scripts.trading.trade_stack.time.time", lambda: next(timestamps))

    gate_one = {"direction": "BUY", "hazard": 0.75, "repetitions": 2, "ts_ms": 1_000}
    gate_two = {"direction": "BUY", "hazard": 0.75, "repetitions": 2, "ts_ms": 6_000}
    price_data = {"mid": 1.10}
    tracker = _TrackerStub()

    processor.process_instrument(
        "EUR_USD",
        gate_one,
        price_data,
        per_trade_exposure=200.0,
        nav_snapshot=100_000.0,
        price_cache={"EUR_USD": {"mid": 1.10}},
        tracker=tracker,
    )
    assert trade_state.trade_count("EUR_USD") == 1

    processor.process_instrument(
        "EUR_USD",
        gate_two,
        price_data,
        per_trade_exposure=200.0,
        nav_snapshot=100_000.0,
        price_cache={"EUR_USD": {"mid": 1.10}},
        tracker=tracker,
    )

    assert trade_state.trade_count("EUR_USD") == 1
    assert "global_cooldown" in engine.calls[-1]["gate_reasons"]
    assert "cooldown_active" in engine.calls[-1]["gate_reasons"]


def test_oanda_bracket_orders_are_opt_in(monkeypatch):
    monkeypatch.delenv("OANDA_ATTACH_BRACKET_ORDERS", raising=False)
    monkeypatch.setenv("OANDA_API_KEY", "key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acct")

    connector = OandaConnector(read_only=False)
    captured = {}

    def _capture(method, path, *, params=None, json_body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {}

    monkeypatch.setattr(connector, "_request", _capture)

    connector.place_market_order(
        "EUR_USD",
        100,
        stop_loss=1.09000,
        take_profit=1.11000,
    )

    order = captured["body"]["order"]
    assert order["instrument"] == "EUR_USD"
    assert "stopLossOnFill" not in order
    assert "takeProfitOnFill" not in order


def test_mean_reversion_uses_dedicated_gate_cache():
    cache_path = gate_cache_path_for("eur_usd", "mean_reversion")
    assert cache_path.name == "EUR_USD.mean_reversion.gates.jsonl"


def test_export_replay_respects_require_st_peak_flag():
    payload = {
        "Haz": 0.82,
        "Reps": 2,
        "Hold": 60,
        "SL": 0.003,
        "TP": 0.006,
        "Trail": None,
        "HazEx": None,
        "Coh": 0.12,
        "Ent": 1.1,
        "Stab": 0.0,
        "BE": 0.002,
    }

    disabled = _build_simulation_params(
        payload,
        "mean_reversion",
        ml_primary_gate=False,
        exposure_scale=0.02,
        require_st_peak=False,
    )
    enabled = _build_simulation_params(
        payload,
        "mean_reversion",
        ml_primary_gate=False,
        exposure_scale=0.02,
        require_st_peak=True,
    )

    assert disabled.st_peak_mode is False
    assert enabled.st_peak_mode is True


def test_full_compose_regime_defaults_match_live_compose():
    live = Path("/sep/tpsl/docker-compose.live.yml").read_text(encoding="utf-8")
    full = Path("/sep/tpsl/docker-compose.full.yml").read_text(encoding="utf-8")

    for text in (live, full):
        assert 'REGIME_WINDOW_CANDLES: "${REGIME_WINDOW_CANDLES:-64}"' in text
        assert 'REGIME_STRIDE_CANDLES: "${REGIME_STRIDE_CANDLES:-16}"' in text
