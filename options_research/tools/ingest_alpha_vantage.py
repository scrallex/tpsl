"""Fetch Alpha Vantage market data into the normalized local options dataset."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from options_research.data import (
    AlphaVantageClient,
    AlphaVantageConfig,
    AlphaVantageDatasetBuilder,
    AlphaVantageIngestionConfig,
)
from options_research.env import load_options_env


logger = logging.getLogger("options_research.ingest_alpha_vantage")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp, e.g. 2024-01-01T00:00:00+00:00")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, e.g. 2024-03-01T00:00:00+00:00")
    parser.add_argument(
        "--data-root",
        default=os.getenv("ALPHA_VANTAGE_DATA_ROOT", os.getenv("OPTIONS_RESEARCH_DATA_ROOT", "data/options_research/alpha_vantage")),
    )
    parser.add_argument("--output-format", choices=("parquet", "csv"), default="parquet")
    parser.add_argument("--daily-outputsize", choices=("compact", "full"), default="compact")
    parser.add_argument("--skip-options", action="store_true")
    parser.add_argument("--skip-actions", action="store_true")
    parser.add_argument("--max-option-days", type=int, default=None)
    parser.add_argument("--request-interval", type=float, default=1.1)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main() -> int:
    load_options_env()
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    client = AlphaVantageClient(
        AlphaVantageConfig(
            timeout_seconds=args.timeout,
            request_interval_seconds=args.request_interval,
        )
    )
    builder = AlphaVantageDatasetBuilder(
        client=client,
        config=AlphaVantageIngestionConfig(
            data_root=Path(args.data_root),
            output_format=args.output_format,
            daily_outputsize=args.daily_outputsize,
            include_options=not args.skip_options,
            include_corporate_actions=not args.skip_actions,
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
