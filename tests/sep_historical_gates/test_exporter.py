from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from sep_historical_gates import HistoricalSEPParityGateExportConfig, HistoricalSEPParityGateExporter


def _bars_frame(*, start: datetime, count: int) -> pd.DataFrame:
    rows = []
    for idx in range(count):
        timestamp = start + timedelta(minutes=idx)
        base = 600.0 + idx
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp),
                "open": base,
                "high": base + 0.5,
                "low": base - 0.5,
                "close": base + 0.25,
                "volume": 1000 + idx,
            }
        )
    return pd.DataFrame(rows)


def test_historical_sep_exporter_fetches_intraday_bars_in_chunks() -> None:
    class StubClient:
        def __init__(self) -> None:
            self.calls = []

        def fetch_intraday_bars(self, **kwargs):  # noqa: ANN003
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return pd.DataFrame(
                    [
                        {
                            "timestamp": pd.Timestamp("2026-03-02T14:30:00Z"),
                            "open": 600.0,
                            "high": 600.5,
                            "low": 599.5,
                            "close": 600.25,
                            "volume": 1000,
                        }
                    ]
                )
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-03-03T14:30:00Z"),
                        "open": 601.0,
                        "high": 601.5,
                        "low": 600.5,
                        "close": 601.25,
                        "volume": 1010,
                    }
                ]
            )

    exporter = HistoricalSEPParityGateExporter(client=StubClient())
    config = HistoricalSEPParityGateExportConfig(
        symbol="SPY",
        start=datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc),
        end=datetime(2026, 3, 3, 20, 0, tzinfo=timezone.utc),
        request_chunk_days=1,
    )

    frame = exporter.fetch_intraday_bars(config=config)

    assert len(frame) == 2
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2026-03-02T14:30:00Z")
    assert frame["timestamp"].iloc[1] == pd.Timestamp("2026-03-03T14:30:00Z")


def test_historical_sep_exporter_builds_sep_style_gate_payloads(monkeypatch) -> None:
    @dataclass
    class StubCanonical:
        regime: str
        regime_confidence: float
        realized_vol: float = 0.1
        atr_mean: float = 0.02
        autocorr: float = 0.3
        trend_strength: float = 1.2
        volume_zscore: float = 0.4

    @dataclass
    class StubWindow:
        end_ms: int
        signature: str
        metrics: dict[str, float]
        canonical: StubCanonical
        codec_meta: dict[str, float]

        def bits_b64(self) -> str:
            return "ZmFrZQ=="

    class StubEncoder:
        def __init__(self, *, window_candles: int, stride_candles: int, atr_period: int) -> None:
            self.window_candles = window_candles
            self.stride_candles = stride_candles
            self.atr_period = atr_period

        def encode(self, candles, *, instrument: str, return_only_latest: bool = False):  # noqa: ANN001
            assert instrument == "SPY"
            return [
                StubWindow(
                    end_ms=int(datetime(2026, 3, 2, 15, 33, tzinfo=timezone.utc).timestamp() * 1000),
                    signature="sig-a",
                    metrics={"hazard": 0.10, "coherence": 0.6, "stability": 0.5},
                    canonical=StubCanonical(regime="trend_bull", regime_confidence=0.90),
                    codec_meta={"volume_split": 10.0},
                ),
                StubWindow(
                    end_ms=int(datetime(2026, 3, 2, 15, 49, tzinfo=timezone.utc).timestamp() * 1000),
                    signature="sig-a",
                    metrics={"hazard": 0.25, "coherence": 0.4, "stability": 0.3},
                    canonical=StubCanonical(regime="neutral", regime_confidence=0.40),
                    codec_meta={"volume_split": 10.0},
                ),
            ]

    monkeypatch.setattr("sep_historical_gates.exporter.MarketManifoldEncoder", StubEncoder)
    monkeypatch.setattr("sep_historical_gates.exporter.apply_semantic_tags", lambda payload: dict(payload))
    monkeypatch.setattr(
        "sep_historical_gates.exporter.get_bundle_hits",
        lambda payload, catalog, bundle_config: (
            [{"id": "MB003", "score": 1.0}, {"id": "OTHER", "score": 0.5}],
            ["CB002", "OTHER"],
            {"MB003": {"id": "MB003", "ready": True}, "OTHER": {"id": "OTHER", "ready": True}},
        ),
    )

    exporter = HistoricalSEPParityGateExporter(client=None)
    config = HistoricalSEPParityGateExportConfig(
        symbol="SPY",
        start=datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc),
        end=datetime(2026, 3, 2, 16, 0, tzinfo=timezone.utc),
        window_candles=4,
        stride_candles=2,
        atr_period=2,
    )

    records = exporter.build_records(
        config=config,
        bars_frame=_bars_frame(start=config.start, count=10),
    )

    assert len(records) == 2
    assert records[0]["instrument"] == "SPY"
    assert records[0]["direction"] == "BUY"
    assert records[0]["admit"] is True
    assert records[0]["action"] == "ARMED"
    assert records[0]["regime"]["label"] == "trend_bull"
    assert records[0]["regime"]["confidence"] == 0.90
    assert records[0]["repetitions"] == 1
    assert records[0]["bundle_hits"] == [{"id": "MB003", "score": 1.0}]
    assert records[0]["bundle_blocks"] == ["CB002"]
    assert records[1]["admit"] is False
    assert records[1]["direction"] == "FLAT"
    assert records[1]["repetitions"] == 2
    assert "regime_filtered" in records[1]["reasons"]
    assert "regime_confidence_low" in records[1]["reasons"]
