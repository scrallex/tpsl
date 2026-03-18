"""CLI for SEP gate outcome studies on underlying returns."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sep_historical_gates.study import GateOutcomeStudyConfig, GateOutcomeStudyRunner


logger = logging.getLogger("sep_historical_gates.study_cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--gate-path", default="data/options_research/gates/SPY.gates.jsonl")
    parser.add_argument("--output", default="data/options_research/results/spy_gate_outcome_study.json")
    parser.add_argument("--intraday-resolution-minutes", type=int, default=1)
    parser.add_argument("--request-chunk-days", type=int, default=30)
    parser.add_argument("--intraday-horizons", nargs="+", type=int, default=[5, 15, 30, 60])
    parser.add_argument("--trading-day-horizons", nargs="+", type=int, default=[1, 3])
    parser.add_argument("--include-all-gates", action="store_true")
    parser.add_argument("--extended-hours", action="store_true")
    parser.add_argument("--adjust-splits", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    config = GateOutcomeStudyConfig(
        symbol=args.symbol.upper(),
        gate_path=Path(args.gate_path),
        output_path=Path(args.output),
        intraday_resolution_minutes=args.intraday_resolution_minutes,
        request_chunk_days=args.request_chunk_days,
        intraday_horizons_minutes=tuple(args.intraday_horizons),
        trading_day_horizons=tuple(args.trading_day_horizons),
        include_only_admitted=not args.include_all_gates,
        extended_hours=args.extended_hours,
        adjust_splits=args.adjust_splits,
    )
    study = GateOutcomeStudyRunner().run(config)
    logger.info(
        "completed gate outcome study symbol=%s gates=%s observations=%s output=%s",
        config.symbol,
        study["eligible_gate_count"],
        study["observation_count"],
        config.output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
