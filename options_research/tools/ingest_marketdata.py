"""Fetch Market Data candles and option chains into the normalized local options dataset."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from options_research.data import (
    MarketDataClient,
    MarketDataConfig,
    MarketDataDatasetBuilder,
    MarketDataIngestionConfig,
)
from options_research.env import load_options_env


logger = logging.getLogger("options_research.ingest_marketdata")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp, e.g. 2024-01-01T00:00:00+00:00")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, e.g. 2024-03-01T00:00:00+00:00")
    parser.add_argument(
        "--data-root",
        default=os.getenv("MARKETDATA_DATA_ROOT", "data/options_research/marketdata"),
    )
    parser.add_argument("--output-format", choices=("parquet", "csv"), default="parquet")
    parser.add_argument("--skip-options", action="store_true")
    parser.add_argument("--skip-actions", action="store_true")
    parser.add_argument("--option-min-dte", type=int, default=1)
    parser.add_argument("--option-max-dte", type=int, default=60)
    parser.add_argument("--max-option-days", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--token", default=None, help="Override MARKETDATA_TOKEN from env")
    parser.add_argument(
        "--use-url-token",
        action="store_true",
        help="Pass the token as a URL parameter instead of an Authorization header",
    )
    return parser


def main() -> int:
    load_options_env()
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    client = MarketDataClient(
        MarketDataConfig(
            token=args.token,
            timeout_seconds=args.timeout,
            use_header_auth=not args.use_url_token,
        )
    )
    builder = MarketDataDatasetBuilder(
        client=client,
        config=MarketDataIngestionConfig(
            data_root=Path(args.data_root),
            output_format=args.output_format,
            include_options=not args.skip_options,
            include_corporate_actions=not args.skip_actions,
            option_min_dte=args.option_min_dte,
            option_max_dte=args.option_max_dte,
            max_option_days=args.max_option_days,
        ),
    )
    outputs = builder.build_symbol_dataset(
        symbol=args.symbol,
        start=start,
        end=end,
    )
    for name, path in outputs.items():
        logger.info("%s -> %s", name, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
