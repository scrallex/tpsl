from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from options_research.data import (
    MarketDataAuthError,
    MarketDataClient,
    MarketDataConfig,
    MarketDataDatasetBuilder,
    MarketDataIngestionConfig,
    MarketDataPlanError,
    MarketDataRateLimitError,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: dict
    text: str = ""

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def get(self, url, params, headers, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError("No fake responses remaining")
        return self.responses.pop(0)


def test_marketdata_client_parses_daily_bars() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status_code=200,
                payload={
                    "s": "ok",
                    "t": [1704862800, 1704949200],
                    "o": [474.0, 476.0],
                    "h": [476.5, 478.0],
                    "l": [473.8, 475.5],
                    "c": [476.0, 477.25],
                    "v": [900000, 1000000],
                },
            )
        ]
    )
    client = MarketDataClient(
        MarketDataConfig(token="test-token"),
        session=session,
    )

    frame = client.fetch_daily_bars(
        symbol="SPY",
        start=datetime(2024, 1, 10, 0, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 11, 0, 0, tzinfo=timezone.utc),
    )

    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert list(frame["close"]) == [476.0, 477.25]
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2024-01-10T05:00:00Z")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert "token" not in session.calls[0]["params"]


def test_marketdata_client_parses_intraday_bars() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status_code=200,
                payload={
                    "s": "ok",
                    "t": [1772479800, 1772479860],
                    "o": [688.21, 688.36],
                    "h": [688.55, 688.62],
                    "l": [688.13, 688.24],
                    "c": [688.36, 688.25],
                    "v": [934430, 253850],
                },
            )
        ]
    )
    client = MarketDataClient(
        MarketDataConfig(token="test-token"),
        session=session,
    )

    frame = client.fetch_intraday_bars(
        symbol="SPY",
        resolution_minutes=1,
        start=datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc),
        end=datetime(2026, 3, 2, 14, 31, tzinfo=timezone.utc),
    )

    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2026-03-02T19:30:00Z")
    assert frame["close"].iloc[1] == 688.25
    assert session.calls[0]["url"].endswith("/stocks/candles/1/SPY/")


def test_marketdata_client_parses_historical_options_with_fallback_spot() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status_code=203,
                payload={
                    "s": "ok",
                    "optionSymbol": ["AAPL260327C00260000"],
                    "underlying": ["AAPL"],
                    "expiration": [int(pd.Timestamp("2026-03-27T00:00:00Z").timestamp())],
                    "side": ["call"],
                    "strike": [260],
                    "bid": [4.8],
                    "ask": [5.1],
                    "last": [5.0],
                    "iv": [0.24],
                    "delta": [0.41],
                    "gamma": [0.02],
                    "theta": [-0.03],
                    "vega": [0.08],
                    "volume": [1250],
                    "openInterest": [4200],
                    "underlyingPrice": [None],
                    "updated": [int(pd.Timestamp("2026-03-06T20:59:00Z").timestamp())],
                },
            )
        ]
    )
    client = MarketDataClient(
        MarketDataConfig(token="test-token"),
        session=session,
    )

    frame = client.fetch_historical_options(
        symbol="AAPL",
        trade_date=date(2026, 3, 6),
        min_expiration=date(2026, 3, 27),
        max_expiration=date(2026, 4, 20),
        fallback_underlying_spot=257.46,
    )

    assert frame["contract_symbol"].iloc[0] == "AAPL260327C00260000"
    assert frame["underlying_spot"].iloc[0] == pytest.approx(257.46)
    assert frame["option_type"].iloc[0] == "call"
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2026-03-06T20:59:00Z")


@pytest.mark.parametrize(
    ("status_code", "exception_type"),
    [
        (401, MarketDataAuthError),
        (402, MarketDataPlanError),
        (429, MarketDataRateLimitError),
    ],
)
def test_marketdata_client_classifies_http_errors(status_code: int, exception_type: type[Exception]) -> None:
    session = FakeSession(
        [
            FakeResponse(
                status_code=status_code,
                payload={"s": "error", "errmsg": "request rejected"},
                text='{"s":"error","errmsg":"request rejected"}',
            )
        ]
    )
    client = MarketDataClient(
        MarketDataConfig(token="test-token"),
        session=session,
    )

    with pytest.raises(exception_type):
        client.fetch_daily_bars(
            symbol="SPY",
            start=datetime(2024, 1, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 1, 11, 0, 0, tzinfo=timezone.utc),
        )


def test_marketdata_dataset_builder_writes_normalized_files(tmp_path) -> None:
    class StubClient:
        def fetch_daily_bars(
            self,
            *,
            symbol: str,
            start: datetime,
            end: datetime,
            adjust_splits: bool = True,
        ) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-03-06T05:00:00Z"),
                        "open": 258.63,
                        "high": 258.77,
                        "low": 254.37,
                        "close": 257.46,
                        "volume": 40527830,
                    }
                ]
            )

        def fetch_historical_options(
            self,
            *,
            symbol: str,
            trade_date: date,
            min_expiration: date,
            max_expiration: date,
            fallback_underlying_spot: float | None = None,
        ) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-03-06T20:59:00Z"),
                        "contract_symbol": "AAPL260327C00260000",
                        "underlying": symbol,
                        "expiry": date(2026, 3, 27),
                        "strike": 260.0,
                        "option_type": "call",
                        "bid": 4.8,
                        "ask": 5.1,
                        "last": 5.0,
                        "implied_volatility": 0.24,
                        "delta": 0.41,
                        "gamma": 0.02,
                        "theta": -0.03,
                        "vega": 0.08,
                        "volume": 1250,
                        "open_interest": 4200,
                        "underlying_spot": fallback_underlying_spot,
                        "multiplier": 100,
                    }
                ]
            )

    builder = MarketDataDatasetBuilder(
        client=StubClient(),
        config=MarketDataIngestionConfig(
            data_root=tmp_path / "dataset",
            output_format="parquet",
            include_options=True,
            include_corporate_actions=True,
            option_min_dte=21,
            option_max_dte=45,
            max_option_days=1,
        ),
    )

    outputs = builder.build_symbol_dataset(
        symbol="AAPL",
        start=datetime(2026, 3, 6, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 3, 6, 23, 59, tzinfo=timezone.utc),
    )

    options_frame = pd.read_parquet(outputs["options"])

    assert set(outputs) == {"underlyings", "corporate_actions", "options"}
    assert outputs["underlyings"].exists()
    assert outputs["corporate_actions"].exists()
    assert outputs["options"].exists()
    assert options_frame["underlying_spot"].iloc[0] == pytest.approx(257.46)
