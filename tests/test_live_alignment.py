from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.trading.exposure_tracker import ExposureTracker
from scripts.research.simulator.gate_cache import gate_cache_path_for
from scripts.research.simulator.gpu_parity_replay import (
    replay_gpu_parity,
    run_gpu_parity_replay,
)
from scripts.research.simulator.backtest_simulator import _structured_sources_for
from scripts.research.regime_manifold.encoder import MarketManifoldEncoder
from scripts.research.regime_manifold.types import Candle as ManifoldCandle, CanonicalFeatures, EncodedWindow
from scripts.tools.export_optimal_trades import _build_simulation_params
from scripts.research.simulator.models import OHLCCandle, TPSLSimulationParams
from scripts.trading.gate_loader import StrategyInstrument
from scripts.trading.oanda import OandaConnector
from scripts.trading.portfolio_manager import PortfolioConfig, PortfolioLoopCoordinator
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.regime_manifold_service import HazardCalibrator, RegimeManifoldService
from scripts.trading.risk_limits import RiskLimits, RiskManager
from scripts.trading.session_policy import SessionPolicy, SessionWindow
from scripts.trading.trade_stack import TradeStackProcessor
from scripts.trading.trade_state import ActiveTrade, TradeStateStore
from scripts.trading_service import TradingService
from scripts.tools.stream_candles import _window_count_for_instrument
from scripts.research.simulator import gate_cache as gate_cache_module


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


def test_mean_reversion_historical_cache_uses_rolling_stride(monkeypatch, tmp_path):
    captured: dict[str, int] = {}

    def _derive(*args, **kwargs):
        captured["window_candles"] = kwargs["window_candles"]
        captured["stride_candles"] = kwargs["stride_candles"]
        return [
            {
                "instrument": "EUR_USD",
                "ts_ms": 1,
                "source": "regime_manifold",
                "components": {
                    "codec_meta": {
                        "window_candles": kwargs["window_candles"],
                        "stride_candles": kwargs["stride_candles"],
                    }
                },
            }
        ]

    monkeypatch.setattr(gate_cache_module, "derive_regime_manifold_gates", _derive)

    cache_path = gate_cache_module.ensure_historical_gate_cache(
        "EUR_USD",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 3, 20, tzinfo=timezone.utc),
        signal_type="mean_reversion",
        base_dir=tmp_path,
        gate_cache_path=tmp_path / "EUR_USD.mean_reversion.gates.jsonl",
    )

    assert cache_path.exists()
    assert captured["window_candles"] == 64
    assert captured["stride_candles"] == 1


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


def test_gpu_parity_replay_respects_st_peak_mode():
    start = datetime(2026, 3, 18, 19, 40, tzinfo=timezone.utc)
    candles = [
        OHLCCandle(time=start, open=1.0000, high=1.0002, low=0.9998, close=1.0000),
        OHLCCandle(
            time=start.replace(second=5),
            open=1.0000,
            high=1.0002,
            low=0.9985,
            close=0.9990,
        ),
    ]
    gates = [
        {
            "ts_ms": int(start.timestamp() * 1000),
            "direction": "BUY",
            "hazard": 0.82,
            "repetitions": 2,
            "components": {
                "coherence": 0.12,
                "stability": 0.2,
                "entropy": 1.1,
            },
            "source": "regime_manifold",
        }
    ]

    disabled = replay_gpu_parity(
        instrument="EUR_USD",
        candles=candles,
        gates=gates,
        params=TPSLSimulationParams(
            signal_type="mean_reversion",
            hazard_min=0.80,
            min_repetitions=1,
            take_profit_pct=0.0005,
            exposure_scale=0.02,
            st_peak_mode=False,
        ),
        nav=100_000.0,
        nav_risk_pct=0.01,
        per_position_pct_cap=0.01,
        cost_bps=1.5,
    )
    enabled = replay_gpu_parity(
        instrument="EUR_USD",
        candles=candles,
        gates=gates,
        params=TPSLSimulationParams(
            signal_type="mean_reversion",
            hazard_min=0.80,
            min_repetitions=1,
            take_profit_pct=0.0005,
            exposure_scale=0.02,
            st_peak_mode=True,
        ),
        nav=100_000.0,
        nav_risk_pct=0.01,
        per_position_pct_cap=0.01,
        cost_bps=1.5,
    )

    assert len(disabled.trades) == 1
    assert len(enabled.trades) == 0


def test_run_gpu_parity_replay_passes_signal_type_to_gate_loader(monkeypatch):
    captured = {}
    start = datetime(2026, 3, 18, 19, 40, tzinfo=timezone.utc)

    class _Adapter:
        def __init__(self, redis_url=None, granularity="S5") -> None:
            self.redis_url = redis_url
            self.granularity = granularity

        def load_ohlc_candles(self, instrument, start_dt, end_dt):
            return [
                OHLCCandle(
                    time=start,
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                )
            ]

        def load_gate_events(self, instrument, start_dt, end_dt, signal_type=None):
            captured["signal_type"] = signal_type
            return []

    monkeypatch.setattr(
        "scripts.research.simulator.gpu_parity_replay.BacktestDataAdapter",
        _Adapter,
    )

    result = run_gpu_parity_replay(
        instrument="EUR_USD",
        start=start,
        end=start,
        params=TPSLSimulationParams(signal_type="mean_reversion"),
        nav=100_000.0,
        nav_risk_pct=0.01,
        per_position_pct_cap=0.01,
        cost_bps=1.5,
    )

    assert result is None
    assert captured["signal_type"] == "mean_reversion"


def test_backtest_structured_source_filter_accepts_regime_manifold():
    assert "regime_manifold" in _structured_sources_for("mean_reversion")
    assert "regime_manifold" in _structured_sources_for("trend_sniper")


def test_full_compose_regime_defaults_match_live_compose():
    live = Path("/sep/tpsl/docker-compose.live.yml").read_text(encoding="utf-8")
    full = Path("/sep/tpsl/docker-compose.full.yml").read_text(encoding="utf-8")

    for text in (live, full):
        assert 'REGIME_WINDOW_CANDLES: "${REGIME_WINDOW_CANDLES:-64}"' in text
        assert 'REGIME_STRIDE_CANDLES: "${REGIME_STRIDE_CANDLES:-16}"' in text
        assert 'BROKER_ACCOUNT_LEVERAGE: "${BROKER_ACCOUNT_LEVERAGE:-50}"' in text
        assert (
            'PORTFOLIO_MARGIN_UTILIZATION_CAP: "${PORTFOLIO_MARGIN_UTILIZATION_CAP:-0.70}"'
            in text
        )


def test_notional_caps_use_broker_buying_power(monkeypatch):
    monkeypatch.delenv("BROKER_ACCOUNT_LEVERAGE", raising=False)
    monkeypatch.delenv("OANDA_MAX_LEVERAGE", raising=False)
    monkeypatch.delenv("PORTFOLIO_MARGIN_UTILIZATION_CAP", raising=False)

    risk_sizer = RiskSizer(
        nav_risk_pct=0.0275,
        per_position_pct_cap=0.0275,
        alloc_top_k=32,
    )

    caps = risk_sizer.compute_notional_caps(572.6775, exposure_scale=1.0)

    assert caps.portfolio_cap == pytest.approx(572.6775 * 50.0 * 0.70)
    assert caps.per_position_cap == pytest.approx(572.6775 * 50.0 * 0.0275)


def test_portfolio_loop_uses_broker_notional_for_live_ticket_size(monkeypatch):
    monkeypatch.setenv("EXPOSURE_SCALE", "1.0")
    monkeypatch.setenv("BROKER_ACCOUNT_LEVERAGE", "50")
    monkeypatch.setenv("PORTFOLIO_MARGIN_UTILIZATION_CAP", "0.70")

    service = _FakeService()
    config = PortfolioConfig(
        profile_path=Path("/sep/tpsl/config/mean_reversion_strategy.yaml"),
        exit_buffer_minutes=5,
        nav_risk_pct=0.0275,
        per_pos_pct=0.0275,
        alloc_top_k=32,
        redis_url=None,
        hold_seconds=1800,
        loop_seconds=2.0,
        reconcile_seconds=0.0,
    )
    coordinator = PortfolioLoopCoordinator(service, config)

    monkeypatch.setattr(
        coordinator.gate_loader,
        "load",
        lambda instruments: {instrument: {} for instrument in instruments},
    )
    monkeypatch.setattr(
        coordinator.exposure_tracker,
        "fetch_prices",
        lambda instruments: {
            instrument: {"mid": 1.10}
            for instrument in instruments
        },
    )
    monkeypatch.setattr(coordinator.exposure_tracker, "nav_snapshot", lambda: 572.6775)
    monkeypatch.setattr(coordinator, "_publish_risk_snapshot", lambda: None)
    monkeypatch.setattr(coordinator, "enforce_time_exits", lambda prices, now_ts: None)

    captured: list[float] = []

    def _capture_process_instrument(
        instrument: str,
        gate_info: dict,
        price_data: dict,
        per_trade_exposure: float,
        nav_snapshot: float,
        price_cache: dict,
        tracker: object,
    ) -> None:
        captured.append(float(per_trade_exposure))

    monkeypatch.setattr(
        coordinator.trade_stack,
        "process_instrument",
        _capture_process_instrument,
    )

    coordinator.loop_once()

    expected_per_trade = 572.6775 * 50.0 * 0.0275
    assert captured
    assert captured == pytest.approx(
        [expected_per_trade] * len(coordinator.enabled_instruments)
    )
    assert coordinator.risk_manager.limits.max_total_exposure == pytest.approx(
        572.6775 * 50.0 * 0.70
    )


def test_trading_service_stop_does_not_force_kill_switch() -> None:
    service = TradingService.__new__(TradingService)
    service.running = True
    service._api_server = SimpleNamespace(shutdown=lambda: None)
    service.portfolio_manager = SimpleNamespace(stop=lambda: None)

    kill_switch_calls: list[bool] = []
    service.set_kill_switch = lambda enabled: kill_switch_calls.append(bool(enabled))

    TradingService.stop(service)

    assert service.running is False
    assert kill_switch_calls == []


def test_manifold_encoder_latest_window_can_ignore_stride_alignment(monkeypatch):
    monkeypatch.setattr(
        "scripts.research.regime_manifold.encoder.StructuralAnalyzer.analyze",
        staticmethod(lambda bit_bytes: ("sig", {"hazard": 0.5, "coherence": 0.2, "entropy": 1.0, "rupture": 0.1, "stability": 0.0})),
    )

    candles = [
        ManifoldCandle(
            timestamp_ms=1_000 + (idx * 5_000),
            open=1.0 + (idx * 0.0001),
            high=1.0002 + (idx * 0.0001),
            low=0.9998 + (idx * 0.0001),
            close=1.0 + (idx * 0.0001),
            volume=1.0,
            spread=0.00004,
        )
        for idx in range(19)
    ]

    encoder = MarketManifoldEncoder(window_candles=8, stride_candles=4, atr_period=3)
    aligned = encoder.encode(
        candles,
        instrument="EUR_USD",
        return_only_latest=True,
        align_latest_to_stride=True,
    )
    rolling = encoder.encode(
        candles,
        instrument="EUR_USD",
        return_only_latest=True,
        align_latest_to_stride=False,
    )

    assert aligned[-1].end_ms == candles[15].timestamp_ms
    assert rolling[-1].end_ms == candles[-1].timestamp_ms


def test_regime_service_emits_latest_rolling_window_once_per_candle(monkeypatch):
    service = RegimeManifoldService.__new__(RegimeManifoldService)
    service.cfg = SimpleNamespace(
        admit_regimes=("trend_bull", "trend_bear", "mean_revert", "neutral", "chaotic"),
        min_confidence=0.0,
        signature_retention_minutes=60,
        lambda_scale=0.1,
    )
    profile = StrategyInstrument(
        symbol="EUR_USD",
        hazard_max=1.0,
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
        session=None,
    )
    service.profile = _StrategyStub(profile)
    service._hazard_calibrators = {"EUR_USD": HazardCalibrator(percentile=0.8)}
    service._signature_history = {}
    service._last_emitted_ts_ms = {}
    service.metric_hazard = SimpleNamespace(labels=lambda **kwargs: SimpleNamespace(set=lambda value: None))
    service.metric_age = SimpleNamespace(labels=lambda **kwargs: SimpleNamespace(set=lambda value: None))
    service.metric_payloads = SimpleNamespace(labels=lambda **kwargs: SimpleNamespace(inc=lambda: None))
    service._load_recent_candles = lambda instrument: [
        SimpleNamespace(high=1.1010, low=1.0990)
        for _ in range(64)
    ]

    captured_kwargs: list[dict] = []
    window = EncodedWindow(
        instrument="EUR_USD",
        start_ms=1_000,
        end_ms=5_000,
        bits=b"\x00",
        bit_length=8,
        signature="sig-a",
        metrics={
            "hazard": 0.5,
            "coherence": 0.2,
            "entropy": 1.0,
            "rupture": 0.1,
            "stability": 0.0,
        },
        canonical=CanonicalFeatures(
            realized_vol=0.0,
            atr_mean=0.0,
            autocorr=0.2,
            trend_strength=2.0,
            volume_zscore=0.0,
            regime="trend_bull",
            regime_confidence=1.0,
        ),
        codec_meta={},
    )

    def _encode(candles, *, instrument, return_only_latest, align_latest_to_stride):
        captured_kwargs.append(
            {
                "instrument": instrument,
                "return_only_latest": return_only_latest,
                "align_latest_to_stride": align_latest_to_stride,
            }
        )
        return [window]

    writes: list[dict] = []
    service.codec = SimpleNamespace(window_candles=64, encode=_encode)
    service._write_gate = lambda instrument, payload: writes.append(dict(payload))

    monkeypatch.setattr(
        RegimeManifoldService,
        "_update_signature_history",
        lambda self, instrument, signature, ts_ms: 1,
    )

    service._process_instrument("EUR_USD")
    service._process_instrument("EUR_USD")

    assert captured_kwargs
    assert all(item["return_only_latest"] is True for item in captured_kwargs)
    assert all(item["align_latest_to_stride"] is False for item in captured_kwargs)
    assert len(writes) == 1
    assert writes[0]["ts_ms"] == 5_000
    assert writes[0]["repetitions"] == 1


def test_candle_stream_rehydrates_when_cache_depth_is_thin():
    recent_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000.0)

    assert _window_count_for_instrument(
        last_written_ts_ms=recent_ts_ms,
        granularity="S5",
        bootstrap_count=180,
        incremental_count=12,
        cached_count=37,
    ) == 180

    assert _window_count_for_instrument(
        last_written_ts_ms=recent_ts_ms,
        granularity="S5",
        bootstrap_count=180,
        incremental_count=12,
        cached_count=180,
    ) == 12
