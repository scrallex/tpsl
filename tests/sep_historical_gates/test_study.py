from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from sep_historical_gates import GateOutcomeStudyConfig, GateOutcomeStudyRunner


def _price_frame() -> pd.DataFrame:
    rows = []
    start_day_one = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    start_day_two = datetime(2026, 3, 3, 14, 30, tzinfo=timezone.utc)
    for index in range(10):
        timestamp = start_day_one + timedelta(minutes=index)
        close = 100.0 + index
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp),
                "open": close - 0.2,
                "high": close + 0.3,
                "low": close - 0.4,
                "close": close,
                "volume": 1000 + index,
            }
        )
    for index in range(10):
        timestamp = start_day_two + timedelta(minutes=index)
        close = 110.0 + index
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp),
                "open": close - 0.2,
                "high": close + 0.3,
                "low": close - 0.4,
                "close": close,
                "volume": 2000 + index,
            }
        )
    return pd.DataFrame(rows)


def test_gate_outcome_study_runner_computes_forward_returns_and_breakdowns(tmp_path: Path) -> None:
    gate_path = tmp_path / "SPY.gates.jsonl"
    gate_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "BUY",
                        "source": "regime_manifold",
                        "hazard": 0.27,
                        "regime": {"label": "trend_bull", "confidence": 0.82},
                        "bundle_hits": [{"id": "MB003"}],
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 14, 31, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 1,
                        "direction": "SELL",
                        "source": "regime_manifold",
                        "hazard": 0.41,
                        "regime": {"label": "trend_bear", "confidence": 0.68},
                        "bundle_hits": [],
                    }
                ),
                json.dumps(
                    {
                        "ts_ms": int(datetime(2026, 3, 2, 14, 32, tzinfo=timezone.utc).timestamp() * 1000),
                        "admit": 0,
                        "direction": "BUY",
                        "source": "regime_manifold",
                        "hazard": 0.19,
                        "regime": {"label": "trend_bull", "confidence": 0.91},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    class StubClient:
        def fetch_intraday_bars(self, **kwargs):  # noqa: ANN003
            return _price_frame()

    output_path = tmp_path / "study.json"
    study = GateOutcomeStudyRunner(client=StubClient()).run(
        GateOutcomeStudyConfig(
            symbol="SPY",
            gate_path=gate_path,
            output_path=output_path,
            intraday_horizons_minutes=(5,),
            trading_day_horizons=(1,),
        )
    )

    assert output_path.exists()
    assert study["gate_count"] == 3
    assert study["eligible_gate_count"] == 2
    assert study["observation_count"] == 2
    assert study["overall"]["horizons"]["5m"]["count"] == 2
    assert study["overall"]["horizons"]["5m"]["directional_win_rate"] == 0.5
    assert study["overall"]["horizons"]["close"]["count"] == 2
    assert study["overall"]["horizons"]["1d"]["count"] == 2
    assert study["breakdowns"]["source"]["regime_manifold"]["count"] == 2
    assert study["breakdowns"]["regime"]["trend_bull"]["count"] == 1
    assert study["breakdowns"]["regime"]["trend_bear"]["count"] == 1
    assert study["breakdowns"]["bundle_hit"]["MB003"]["count"] == 1
    assert study["breakdowns"]["bundle_hit"]["none"]["count"] == 1
    assert "09:30-10:00 ET" in study["breakdowns"]["time_of_day"]


def test_gate_outcome_study_runner_can_include_non_admitted_gates(tmp_path: Path) -> None:
    gate_path = tmp_path / "SPY.gates.jsonl"
    gate_path.write_text(
        json.dumps(
            {
                "ts_ms": int(datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc).timestamp() * 1000),
                "admit": 0,
                "direction": "BUY",
                "source": "regime_manifold",
                "hazard": 0.27,
                "regime": {"label": "trend_bull", "confidence": 0.82},
            }
        ),
        encoding="utf-8",
    )

    class StubClient:
        def fetch_intraday_bars(self, **kwargs):  # noqa: ANN003
            return _price_frame()

    study = GateOutcomeStudyRunner(client=StubClient()).run(
        GateOutcomeStudyConfig(
            symbol="SPY",
            gate_path=gate_path,
            output_path=tmp_path / "study.json",
            intraday_horizons_minutes=(5,),
            trading_day_horizons=(1,),
            include_only_admitted=False,
        )
    )

    assert study["eligible_gate_count"] == 1
    assert study["observation_count"] == 1
