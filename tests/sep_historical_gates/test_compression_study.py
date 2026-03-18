from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from sep_historical_gates import GateCompressionStudyConfig, GateCompressionStudyRunner


def _compression_price_frame() -> pd.DataFrame:
    rows = []
    closes_by_day = {
        datetime(2026, 3, 2, 20, 59, tzinfo=timezone.utc): 100.0,
        datetime(2026, 3, 3, 20, 59, tzinfo=timezone.utc): 95.0,
        datetime(2026, 3, 4, 20, 59, tzinfo=timezone.utc): 105.0,
        datetime(2026, 3, 5, 20, 59, tzinfo=timezone.utc): 110.0,
    }
    for close_time, close_price in closes_by_day.items():
        for offset, price in enumerate((close_price - 1.0, close_price - 0.5, close_price)):
            timestamp = close_time - timedelta(minutes=2 - offset)
            rows.append(
                {
                    "timestamp": pd.Timestamp(timestamp),
                    "open": price - 0.1,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "close": price,
                    "volume": 1000 + offset,
                }
            )
    return pd.DataFrame(rows)


def test_gate_compression_study_runner_evaluates_daily_rules(tmp_path: Path) -> None:
    gate_path = tmp_path / "SPY.gates.jsonl"
    gate_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 14, 31, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "BUY",
                        "source": "regime_manifold",
                        "hazard": 0.20,
                        "regime": {"label": "trend_bull", "trend_strength": 1.0},
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 16, 0, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "SELL",
                        "source": "regime_manifold",
                        "hazard": 0.30,
                        "regime": {"label": "trend_bear", "trend_strength": -2.0},
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 18, 0, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "SELL",
                        "source": "regime_manifold",
                        "hazard": 0.10,
                        "regime": {"label": "trend_bear", "trend_strength": -1.5},
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 3, 14, 35, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "BUY",
                        "source": "regime_manifold",
                        "hazard": 0.25,
                        "regime": {"label": "trend_bull", "trend_strength": 0.5},
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 3, 18, 0, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "SELL",
                        "source": "regime_manifold",
                        "hazard": 0.05,
                        "regime": {"label": "trend_bear", "trend_strength": -0.4},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    class StubClient:
        def fetch_intraday_bars(self, **kwargs):  # noqa: ANN003
            return _compression_price_frame()

    payload = GateCompressionStudyRunner(client=StubClient()).run(
        GateCompressionStudyConfig(
            symbol="SPY",
            gate_path=gate_path,
            output_path=tmp_path / "compression.json",
            trading_day_horizons=(1,),
        )
    )

    assert payload["decisioning_policy"]["non_decisioning_fields"] == ["bundle_hits", "confidence"]
    assert payload["rules"]["first_admitted"]["decision_count"] == 2
    assert payload["rules"]["last_admitted"]["decision_count"] == 2
    assert payload["rules"]["strongest_admitted"]["decision_count"] == 2
    assert payload["rules"]["majority_direction"]["decision_count"] == 1
    assert payload["rules"]["majority_direction"]["skip_reason_breakdown"] == {"tied_direction": 1}

    first_day_first = payload["rules"]["first_admitted"]["daily_decisions"][0]
    first_day_last = payload["rules"]["last_admitted"]["daily_decisions"][0]
    first_day_strongest = payload["rules"]["strongest_admitted"]["daily_decisions"][0]
    first_day_majority = payload["rules"]["majority_direction"]["daily_decisions"][0]

    assert first_day_first["direction"] == "BUY"
    assert first_day_last["direction"] == "SELL"
    assert first_day_strongest["direction"] == "SELL"
    assert first_day_majority["direction"] == "SELL"
    assert first_day_majority["returns"]["1d"] == pytest.approx(-0.05)
    assert first_day_majority["directional_returns"]["1d"] == pytest.approx(0.05)
    assert first_day_majority["decision_time"].endswith("20:59:00+00:00")
    assert first_day_majority["representative_gate_time"].endswith("18:00:00+00:00")
