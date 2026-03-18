import json
from pathlib import Path
from types import SimpleNamespace

from scripts.trading.pricing_cache import PriceHistoryCache
from scripts.trading.portfolio_manager import StrategyProfile
from scripts.trading_service import TradingService


class DummyRedis:
    def __init__(self, rows):
        self._rows = rows

    def zrange(self, key: str, start: int, end: int):
        return self._rows


def test_price_history_prefers_valkey_stream() -> None:
    rows = [
        json.dumps({"time": "2026-03-08T00:00:00Z", "t": 1, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1}),
        json.dumps({"time": "2026-03-08T00:00:05Z", "t": 2, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2}),
    ]
    service = TradingService.__new__(TradingService)
    service.state_manager = SimpleNamespace(_valkey_client=DummyRedis(rows))
    service.price_history_cache = PriceHistoryCache(None)
    service.oanda = SimpleNamespace(
        get_candles=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("OANDA should not be called"))
    )

    payload = TradingService.price_history(service, "EUR_USD", granularity="S5", count=2)

    assert payload["source"] == "valkey"
    assert payload["points"][-1]["close"] == 1.2


def test_update_strategy_bounds_persists_to_disk(tmp_path: Path) -> None:
    profile_path = tmp_path / "strategy.yaml"
    profile_path.write_text(
        """
global:
  min_repetitions: 1
instruments:
  EUR_USD:
    hazard_max: 0.6
    min_repetitions: 1
    guards: {}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    service = TradingService.__new__(TradingService)
    service.strategy_profile_path = profile_path
    service.portfolio_manager = SimpleNamespace(
        strategy=StrategyProfile.load(profile_path)
    )

    assert TradingService.update_strategy_bounds(
        service,
        "EUR_USD",
        {"hazard_max": 0.42, "hold_minutes": 30, "guards": {"min_coherence": 0.3}},
    )

    persisted = profile_path.read_text(encoding="utf-8")
    assert "hazard_max: 0.42" in persisted
    assert "max_hold_minutes: 30" in persisted
    assert "min_coherence: 0.3" in persisted


def test_update_strategy_bounds_maps_raw_gpu_mean_reversion_payload(tmp_path: Path) -> None:
    profile_path = tmp_path / "strategy.yaml"
    profile_path.write_text(
        """
global:
  min_repetitions: 1
instruments:
  EUR_USD:
    invert_bundles: true
    hazard_min: 0.6
    hazard_max: null
    min_repetitions: 1
    guards:
      min_coherence: 0.0
      min_stability: 0.0
      max_entropy: 3.0
    exit:
      max_hold_minutes: 120
""".strip()
        + "\n",
        encoding="utf-8",
    )

    service = TradingService.__new__(TradingService)
    service.strategy_profile_path = profile_path
    service.portfolio_manager = SimpleNamespace(
        strategy=StrategyProfile.load(profile_path)
    )

    assert TradingService.update_strategy_bounds(
        service,
        "EUR_USD",
        {
            "Haz": 0.8611,
            "Reps": 1,
            "Coh": 0.17017,
            "Ent": 1.63495,
            "Stab": 0.0,
            "Hold": 1521,
            "SL": 0.00493,
            "TP": 0.00667,
            "Trail": None,
            "BE": 0.00169,
        },
    )

    live = service.portfolio_manager.strategy.get("EUR_USD")
    assert live.hazard_min == 0.8611
    assert live.min_repetitions == 1
    assert live.stop_loss_pct == 0.00493
    assert live.take_profit_pct == 0.00667
    assert live.trailing_stop_pct is None
    assert live.breakeven_trigger_pct == 0.00169
    assert live.hold_minutes == 1521
    assert live.guards["min_coherence"] == 0.17017
    assert live.guards["max_entropy"] == 1.63495
    assert live.guards["min_stability"] == 0.0

    persisted = profile_path.read_text(encoding="utf-8")
    assert "hazard_min: 0.8611" in persisted
    assert "max_hold_minutes: 1521" in persisted
    assert "min_coherence: 0.17017" in persisted
    assert "max_entropy: 1.63495" in persisted


def test_update_strategy_bounds_does_not_mutate_live_profile_when_persist_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "strategy.yaml"
    profile_path.write_text(
        """
global:
  min_repetitions: 1
instruments:
  EUR_USD:
    invert_bundles: true
    hazard_min: 0.6
    hazard_max: null
    min_repetitions: 1
    guards:
      min_coherence: 0.1
      min_stability: 0.0
      max_entropy: 2.0
    exit:
      max_hold_minutes: 120
""".strip()
        + "\n",
        encoding="utf-8",
    )

    service = TradingService.__new__(TradingService)
    service.strategy_profile_path = profile_path
    service.portfolio_manager = SimpleNamespace(
        strategy=StrategyProfile.load(profile_path)
    )

    def _boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(service, "_persist_strategy_bounds", _boom)

    live = service.portfolio_manager.strategy.get("EUR_USD")
    assert TradingService.update_strategy_bounds(
        service,
        "EUR_USD",
        {"Haz": 0.9, "Coh": 0.3, "Hold": 999},
    ) is False
    assert live.hazard_min == 0.6
    assert live.guards["min_coherence"] == 0.1
    assert live.hold_minutes == 120
