"""CLI for exporting historical SEP-style gates from Market Data intraday bars."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from sep_historical_gates import HistoricalSEPParityGateExportConfig, HistoricalSEPParityGateExporter


logger = logging.getLogger("sep_historical_gates.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp, e.g. 2026-01-20T14:30:00+00:00")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, e.g. 2026-03-06T21:00:00+00:00")
    parser.add_argument("--resolution-minutes", type=int, default=1)
    parser.add_argument("--request-chunk-days", type=int, default=30)
    parser.add_argument("--adjust-splits", action="store_true")
    parser.add_argument("--extended-hours", action="store_true")
    parser.add_argument("--window-candles", type=int, default=64)
    parser.add_argument("--stride-candles", type=int, default=16)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--signature-retention-minutes", type=int, default=60)
    parser.add_argument("--hazard-percentile", type=float, default=0.8)
    parser.add_argument("--hazard-max", type=float, default=1.0)
    parser.add_argument("--admit-regimes", default="trend_bull,trend_bear")
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--lambda-scale", type=float, default=0.1)
    parser.add_argument("--bundle-config", default="config/bundle_strategy.yaml")
    parser.add_argument("--regime-mapping", default="config/regime_mapping.json")
    parser.add_argument("--output", default=None)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    config = HistoricalSEPParityGateExportConfig(
        symbol=args.symbol.upper(),
        start=datetime.fromisoformat(args.start),
        end=datetime.fromisoformat(args.end),
        resolution_minutes=args.resolution_minutes,
        request_chunk_days=args.request_chunk_days,
        adjust_splits=args.adjust_splits,
        extended_hours=args.extended_hours,
        window_candles=args.window_candles,
        stride_candles=args.stride_candles,
        atr_period=args.atr_period,
        signature_retention_minutes=args.signature_retention_minutes,
        hazard_percentile=args.hazard_percentile,
        hazard_max=args.hazard_max,
        admit_regimes=tuple(item.strip() for item in args.admit_regimes.split(",") if item.strip()),
        min_confidence=args.min_confidence,
        lambda_scale=args.lambda_scale,
        bundle_config=Path(args.bundle_config) if args.bundle_config else None,
        regime_mapping_path=Path(args.regime_mapping) if args.regime_mapping else None,
    )
    exporter = HistoricalSEPParityGateExporter()
    result = exporter.export(
        config=config,
        output_path=Path(args.output) if args.output else None,
    )
    logger.info(
        "exported gates symbol=%s bars=%d gates=%d admitted=%d output=%s",
        config.symbol,
        result.bar_count,
        result.gate_count,
        result.admitted_gate_count,
        result.output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
