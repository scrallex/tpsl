from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts.tools.stream_candles import (
    _latest_candle_age_seconds,
    _refresh_stale_candles,
    _stale_threshold_seconds,
    _target_latency_seconds,
    _window_count_for_instrument,
)


class FakeConnector:
    def __init__(self, responses: list[list[dict[str, object]]], *, read_only: bool = True) -> None:
        self._responses = list(responses)
        self.read_only = read_only
        self.closed = False
        self.session = SimpleNamespace(close=self._close)

    def _close(self) -> None:
        self.closed = True

    def get_candles(self, instrument: str, *, granularity: str, count: int) -> list[dict[str, object]]:
        assert instrument == "EUR_USD"
        assert granularity == "S5"
        assert count >= 5
        return self._responses.pop(0)


def test_latest_candle_age_seconds_returns_none_for_missing_time() -> None:
    assert _latest_candle_age_seconds([]) is None
    assert _latest_candle_age_seconds([{}]) is None


def test_refresh_stale_candles_rebuilds_connector_when_feed_is_old(monkeypatch) -> None:
    monkeypatch.delenv("CANDLE_STREAM_STALE_SECONDS", raising=False)
    stale = [{"time": "2026-03-11T20:59:00.000000000Z"}]
    fresh = [{"time": "2026-03-11T21:05:25.000000000Z"}]
    original = FakeConnector([stale])
    refreshed = FakeConnector([fresh])

    candles, connector = _refresh_stale_candles(
        original,
        instrument="EUR_USD",
        granularity="S5",
        recent_count=5,
        candles=stale,
        connector_factory=lambda **kwargs: refreshed,
    )

    assert original.closed is True
    assert connector is refreshed
    assert candles == fresh


def test_stale_threshold_defaults_scale_with_granularity(monkeypatch) -> None:
    monkeypatch.delenv("CANDLE_STREAM_STALE_SECONDS", raising=False)

    assert _stale_threshold_seconds("S5") == 90.0
    assert _stale_threshold_seconds("M1") == 360.0


def test_target_latency_defaults_scale_with_granularity(monkeypatch) -> None:
    monkeypatch.delenv("CANDLE_STREAM_TARGET_LATENCY_SECONDS", raising=False)

    assert _target_latency_seconds("S5") == 20.0
    assert _target_latency_seconds("M1") == 240.0


def test_window_count_bootstraps_then_uses_incremental_window() -> None:
    now = datetime.now(timezone.utc)
    recent_ts_ms = int((now - timedelta(seconds=10)).timestamp() * 1000)
    stale_ts_ms = int((now - timedelta(seconds=90)).timestamp() * 1000)

    assert (
        _window_count_for_instrument(
            last_written_ts_ms=None,
            granularity="S5",
            bootstrap_count=180,
            incremental_count=6,
        )
        == 180
    )
    assert (
        _window_count_for_instrument(
            last_written_ts_ms=recent_ts_ms,
            granularity="S5",
            bootstrap_count=180,
            incremental_count=6,
        )
        == 6
    )
    assert (
        _window_count_for_instrument(
            last_written_ts_ms=stale_ts_ms,
            granularity="S5",
            bootstrap_count=180,
            incremental_count=6,
        )
        == 180
    )
