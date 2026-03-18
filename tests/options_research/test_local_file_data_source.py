from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from options_research.data import (
    DataRequest,
    LocalFileDatasetConfig,
    LocalFileOptionsDataSource,
    NormalizedDataError,
)
from options_research.models import OptionRight


def build_source(tmp_path) -> LocalFileOptionsDataSource:
    root = tmp_path / "dataset"
    (root / "underlyings").mkdir(parents=True)
    (root / "options").mkdir(parents=True)
    (root / "corporate_actions").mkdir(parents=True)

    bars = pd.DataFrame(
        [
            {
                "time": "2024-01-02T14:30:00Z",
                "o": 470.0,
                "h": 470.7,
                "l": 469.8,
                "c": 470.4,
                "v": 1000,
                "adj_close": 470.4,
            },
            {
                "time": "2024-01-02T14:35:00Z",
                "o": 470.4,
                "h": 471.1,
                "l": 470.2,
                "c": 470.9,
                "v": 1100,
                "adj_close": 470.9,
            },
            {
                "time": "2024-01-02T14:40:00Z",
                "o": 470.9,
                "h": 471.3,
                "l": 470.6,
                "c": 471.0,
                "v": 1200,
                "adj_close": 471.0,
            },
        ]
    )
    bars.to_csv(root / "underlyings" / "SPY.csv", index=False)

    option_rows = pd.DataFrame(
        [
            {
                "as_of": "2024-01-02T14:30:00Z",
                "contract": "SPY240216C00470000",
                "expiry": "2024-02-16",
                "strike": 470.0,
                "right": "C",
                "bid": 4.90,
                "ask": 5.10,
                "last": 5.00,
                "iv": 0.18,
                "delta": 0.41,
                "gamma": 0.02,
                "theta": -0.03,
                "vega": 0.08,
                "volume": 250,
                "oi": 500,
                "spot": 470.4,
            },
            {
                "as_of": "2024-01-02T14:30:00Z",
                "contract": "SPY240216C00475000",
                "expiry": "2024-02-16",
                "strike": 475.0,
                "right": "C",
                "bid": 2.90,
                "ask": 3.10,
                "last": 3.00,
                "iv": 0.17,
                "delta": 0.28,
                "gamma": 0.02,
                "theta": -0.02,
                "vega": 0.07,
                "volume": 200,
                "oi": 450,
                "spot": 470.4,
            },
            {
                "as_of": "2024-01-02T14:35:00Z",
                "contract": "SPY240216C00470000",
                "expiry": "2024-02-16",
                "strike": 470.0,
                "right": "C",
                "bid": 5.10,
                "ask": 5.30,
                "last": 5.20,
                "iv": 0.19,
                "delta": 0.43,
                "gamma": 0.02,
                "theta": -0.03,
                "vega": 0.08,
                "volume": 260,
                "oi": 510,
                "spot": 470.9,
            },
            {
                "as_of": "2024-01-02T14:35:00Z",
                "contract": "SPY240216C00475000",
                "expiry": "2024-02-16",
                "strike": 475.0,
                "right": "C",
                "bid": 3.00,
                "ask": 3.20,
                "last": 3.10,
                "iv": 0.18,
                "delta": 0.29,
                "gamma": 0.02,
                "theta": -0.02,
                "vega": 0.07,
                "volume": 210,
                "oi": 455,
                "spot": 470.9,
            },
        ]
    )
    option_rows.to_parquet(root / "options" / "SPY.parquet", index=False)

    actions = [
        {
            "date": "2024-01-15",
            "type": "dividend",
            "value": 1.65,
            "description": "Quarterly dividend",
        }
    ]
    (root / "corporate_actions" / "SPY.json").write_text(json.dumps(actions), encoding="utf-8")

    config = LocalFileDatasetConfig(
        root=root,
        underlying_bars_pattern="underlyings/{symbol}.csv",
        option_chain_pattern="options/{symbol}.parquet",
        corporate_actions_pattern="corporate_actions/{symbol}.json",
    )
    return LocalFileOptionsDataSource(config)


def test_local_file_data_source_loads_and_filters_underlying_bars(tmp_path) -> None:
    source = build_source(tmp_path)
    request = DataRequest(
        underlying="SPY",
        start=datetime(2024, 1, 2, 14, 35, tzinfo=timezone.utc),
        end=datetime(2024, 1, 2, 14, 40, tzinfo=timezone.utc),
    )

    bars = source.load_underlying_bars(request)

    assert len(bars) == 2
    assert bars[0].close == pytest.approx(470.9)
    assert bars[1].adjusted_close == pytest.approx(471.0)


def test_local_file_data_source_groups_option_rows_into_snapshots_and_supports_lookup(tmp_path) -> None:
    source = build_source(tmp_path)
    request = DataRequest(
        underlying="SPY",
        start=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        end=datetime(2024, 1, 2, 14, 35, tzinfo=timezone.utc),
    )

    snapshots = source.iter_option_chain_snapshots(request)
    lookup = source.load_option_chain_snapshot(
        underlying="SPY",
        as_of=datetime(2024, 1, 2, 14, 34, tzinfo=timezone.utc),
    )

    assert len(snapshots) == 2
    assert snapshots[0].as_of == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    assert snapshots[1].as_of == datetime(2024, 1, 2, 14, 35, tzinfo=timezone.utc)
    assert len(snapshots[0].quotes) == 2
    assert snapshots[0].quotes[0].option_type is OptionRight.CALL
    assert snapshots[1].underlying_spot == pytest.approx(470.9)
    assert lookup is not None
    assert lookup.as_of == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def test_local_file_data_source_loads_corporate_actions(tmp_path) -> None:
    source = build_source(tmp_path)
    request = DataRequest(
        underlying="SPY",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 31, 0, 0, tzinfo=timezone.utc),
    )

    actions = source.load_corporate_actions(request)

    assert len(actions) == 1
    assert actions[0].action_type == "dividend"
    assert actions[0].value == pytest.approx(1.65)


def test_local_file_data_source_rejects_missing_required_option_columns(tmp_path) -> None:
    root = tmp_path / "dataset"
    (root / "options").mkdir(parents=True)
    bad_rows = pd.DataFrame(
        [
            {
                "as_of": "2024-01-02T14:30:00Z",
                "contract": "SPY240216C00470000",
                "expiry": "2024-02-16",
                "strike": 470.0,
                "right": "C",
                "ask": 5.10,
                "spot": 470.4,
            }
        ]
    )
    bad_rows.to_parquet(root / "options" / "SPY.parquet", index=False)
    source = LocalFileOptionsDataSource(
        LocalFileDatasetConfig(
            root=root,
            option_chain_pattern="options/{symbol}.parquet",
        )
    )

    with pytest.raises(NormalizedDataError, match="missing required columns: bid"):
        source.load_option_chain_snapshot(
            underlying="SPY",
            as_of=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        )
