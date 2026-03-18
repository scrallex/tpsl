from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import os

import pandas as pd
import pytest

from options_research.data import (
    AlphaVantageClient,
    AlphaVantageConfig,
    AlphaVantageDatasetBuilder,
    AlphaVantageIngestionConfig,
    AlphaVantagePremiumEndpointError,
    AlphaVantageRateLimitError,
)
from options_research.env import load_options_env


@dataclass
class FakeResponse:
    payload: dict

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls: list[dict] = []

    def get(self, url, params, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self.payloads:
            raise AssertionError("No fake payloads remaining")
        return FakeResponse(self.payloads.pop(0))


def test_load_options_env_populates_missing_variables(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ALPHA_VANTAGE_API_KEY=test-key\nOPTIONS_RESEARCH_DATA_ROOT=data/custom\n", encoding="utf-8")
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("OPTIONS_RESEARCH_DATA_ROOT", raising=False)

    load_options_env(env_path)

    assert os.environ["ALPHA_VANTAGE_API_KEY"] == "test-key"
    assert os.environ["OPTIONS_RESEARCH_DATA_ROOT"] == "data/custom"


def test_alpha_vantage_client_parses_daily_bars() -> None:
    session = FakeSession(
        [
            {
                "Meta Data": {"2. Symbol": "SPY"},
                "Time Series (Daily)": {
                    "2024-01-11": {
                        "1. open": "476.00",
                        "2. high": "478.00",
                        "3. low": "475.50",
                        "4. close": "477.25",
                        "5. volume": "1000000",
                    },
                    "2024-01-10": {
                        "1. open": "474.00",
                        "2. high": "476.50",
                        "3. low": "473.80",
                        "4. close": "476.00",
                        "5. volume": "900000",
                    },
                },
            }
        ]
    )
    client = AlphaVantageClient(
        AlphaVantageConfig(api_key="test-key", request_interval_seconds=0.0),
        session=session,
    )

    frame = client.fetch_daily_bars(symbol="SPY", outputsize="compact")

    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert list(frame["close"]) == [476.0, 477.25]
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2024-01-10T00:00:00Z")


def test_alpha_vantage_client_classifies_premium_and_rate_limit_messages() -> None:
    premium_client = AlphaVantageClient(
        AlphaVantageConfig(api_key="test-key", request_interval_seconds=0.0),
        session=FakeSession(
            [
                {
                    "Information": "Thank you for using Alpha Vantage! This is a premium endpoint.",
                }
            ]
        ),
    )
    rate_limited_client = AlphaVantageClient(
        AlphaVantageConfig(api_key="test-key", request_interval_seconds=0.0),
        session=FakeSession(
            [
                {
                    "Information": "Thank you for using Alpha Vantage! Please consider spreading out your free API requests more sparingly (1 request per second).",
                }
            ]
        ),
    )

    with pytest.raises(AlphaVantagePremiumEndpointError):
        premium_client.fetch_historical_options(symbol="SPY", trade_date=date(2024, 1, 10))
    with pytest.raises(AlphaVantageRateLimitError):
        rate_limited_client.fetch_dividends(symbol="SPY")


def test_alpha_vantage_client_parses_historical_options_with_fallback_spot() -> None:
    session = FakeSession(
        [
            {
                "data": [
                    {
                        "date": "2024-01-10",
                        "contractID": "SPY240209C00485000",
                        "expiration": "2024-02-09",
                        "strike": "485",
                        "type": "call",
                        "bid": "5.10",
                        "ask": "5.30",
                        "last": "5.20",
                        "implied_volatility": "0.18",
                        "delta": "0.40",
                        "gamma": "0.02",
                        "theta": "-0.03",
                        "vega": "0.08",
                        "volume": "250",
                        "open_interest": "500",
                    }
                ]
            }
        ]
    )
    client = AlphaVantageClient(
        AlphaVantageConfig(api_key="test-key", request_interval_seconds=0.0),
        session=session,
    )

    frame = client.fetch_historical_options(
        symbol="SPY",
        trade_date=date(2024, 1, 10),
        fallback_underlying_spot=476.0,
    )

    assert frame["contract_symbol"].iloc[0] == "SPY240209C00485000"
    assert frame["underlying_spot"].iloc[0] == pytest.approx(476.0)
    assert frame["option_type"].iloc[0] == "call"


def test_alpha_vantage_dataset_builder_writes_normalized_files(tmp_path) -> None:
    class StubClient:
        def fetch_daily_bars(self, *, symbol: str, outputsize: str = "full") -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2024-01-10T00:00:00Z"),
                        "open": 474.0,
                        "high": 476.5,
                        "low": 473.8,
                        "close": 476.0,
                        "volume": 900000,
                    }
                ]
            )

        def fetch_dividends(self, *, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "symbol": symbol,
                        "ex_date": date(2024, 1, 15),
                        "action_type": "dividend",
                        "value": 1.5,
                        "description": "",
                    }
                ]
            )

        def fetch_splits(self, *, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(columns=["symbol", "ex_date", "action_type", "value", "description"])

        def fetch_historical_options(
            self,
            *,
            symbol: str,
            trade_date: date,
            fallback_underlying_spot: float | None = None,
        ) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2024-01-10T00:00:00Z"),
                        "contract_symbol": "SPY240209C00485000",
                        "underlying": symbol,
                        "expiry": date(2024, 2, 9),
                        "strike": 485.0,
                        "option_type": "call",
                        "bid": 5.10,
                        "ask": 5.30,
                        "last": 5.20,
                        "implied_volatility": 0.18,
                        "delta": 0.40,
                        "gamma": 0.02,
                        "theta": -0.03,
                        "vega": 0.08,
                        "volume": 250,
                        "open_interest": 500,
                        "underlying_spot": fallback_underlying_spot or 476.0,
                        "multiplier": 100,
                    }
                ]
            )

    builder = AlphaVantageDatasetBuilder(
        client=StubClient(),
        config=AlphaVantageIngestionConfig(
            data_root=tmp_path / "dataset",
            output_format="parquet",
            include_options=True,
            include_corporate_actions=True,
            max_option_days=1,
        ),
    )

    outputs = builder.build_symbol_dataset(
        symbol="SPY",
        start=datetime(2024, 1, 10, 0, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 10, 23, 59, tzinfo=timezone.utc),
    )

    assert set(outputs) == {"underlyings", "corporate_actions", "options"}
    assert outputs["underlyings"].exists()
    assert outputs["corporate_actions"].exists()
    assert outputs["options"].exists()
