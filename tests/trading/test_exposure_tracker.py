from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.trading import oanda as oanda_service
from scripts.trading.exposure_tracker import ExposureTracker
from scripts.trading.risk_limits import RiskLimits, RiskManager


def test_execute_delta_requires_confirmed_fill(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits())
    tracker = ExposureTracker(SimpleNamespace(), risk_manager)

    monkeypatch.setattr(oanda_service, "submit_market_order", lambda *args, **kwargs: None)

    assert tracker.execute_delta("EUR_USD", 100, 1.1) is False
    assert risk_manager.net_units("EUR_USD") == 0


def test_close_ticket_only_reduces_requested_slot(monkeypatch) -> None:
    risk_manager = RiskManager(RiskLimits())
    tracker = ExposureTracker(SimpleNamespace(), risk_manager)
    now = datetime.now(timezone.utc)

    tracker.open_position("EUR_USD", 100, 1.10, now)
    tracker.open_position("EUR_USD", 50, 1.11, now)
    risk_manager.record_fill("EUR_USD", 150, 1.11)

    monkeypatch.setattr(
        oanda_service,
        "close_position",
        lambda *args, **kwargs: {
            "longOrderFillTransaction": {"units": "-50", "price": "1.12"}
        },
    )

    result = tracker.close_ticket("EUR_USD", 1, 1.12, now, "time_exit", 1.12)

    assert result is not None
    assert tracker.net_units("EUR_USD") == 100
    assert risk_manager.net_units("EUR_USD") == 100
