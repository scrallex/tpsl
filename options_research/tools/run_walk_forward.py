"""Run rolling walk-forward evaluation for the isolated options research package."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from options_research.backtest import PromotionCriteria, RollingWalkForwardRunner, WalkForwardConfig
from options_research.env import load_options_env
from options_research.tools.run_backtest import _build_runner, _safe_number


logger = logging.getLogger("options_research.run_walk_forward")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlyings", nargs="+", default=["SPY", "QQQ", "IWM"])
    parser.add_argument("--start", required=True, help="UTC ISO timestamp, e.g. 2025-01-01T00:00:00+00:00")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, e.g. 2026-03-01T00:00:00+00:00")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--min-oos-trades", type=int, default=100)
    parser.add_argument("--min-profit-factor", type=float, default=1.2)
    parser.add_argument("--min-sharpe", type=float, default=1.0)
    parser.add_argument("--min-positive-window-ratio", type=float, default=0.60)
    parser.add_argument("--max-fallback-selection-rate", type=float, default=0.25)
    parser.add_argument("--report-path", default="data/options_research/results/walk_forward.json")

    backtest_parser = _shared_backtest_parser()
    for action in backtest_parser._actions:
        if not action.option_strings:
            continue
        if any(option in {"-h", "--help", "--underlying", "--start", "--end", "--report-path"} for option in action.option_strings):
            continue
        parser._add_action(action)
    return parser


def _shared_backtest_parser() -> argparse.ArgumentParser:
    from options_research.tools.run_backtest import build_parser as build_backtest_parser

    return build_backtest_parser()


def _serialize_report(report) -> dict:  # noqa: ANN001
    return {
        "oos_metrics": {key: _safe_number(value) for key, value in report.oos_metrics.items()},
        "underlying_metrics": {
            key: {metric: _safe_number(value) for metric, value in metrics.items()}
            for key, metrics in report.underlying_metrics.items()
        },
        "promotion": {
            "promotable": report.promotion.promotable,
            "failed_checks": list(report.promotion.failed_checks),
            "summary": {key: _safe_number(value) for key, value in report.promotion.summary.items()},
        },
        "metadata": report.metadata,
        "windows": [
            {
                "underlying": item.underlying,
                "train_start": item.window.train_start.isoformat(),
                "train_end": item.window.train_end.isoformat(),
                "test_start": item.window.test_start.isoformat(),
                "test_end": item.window.test_end.isoformat(),
                "train_metrics": {key: _safe_number(value) for key, value in item.train_result.metrics.items()},
                "test_metrics": {key: _safe_number(value) for key, value in item.test_result.metrics.items()},
                "test_trades": item.test_result.total_trades,
                "test_rejected_entries": item.test_result.rejected_entries,
            }
            for item in report.windows
        ],
    }


def main() -> int:
    load_options_env()
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    runner = RollingWalkForwardRunner(
        runner_factory=lambda underlying: _build_runner(args),
        initial_capital=args.initial_capital,
        promotion_criteria=PromotionCriteria(
            min_oos_trades=args.min_oos_trades,
            min_profit_factor=args.min_profit_factor,
            min_sharpe=args.min_sharpe,
            min_positive_window_ratio=args.min_positive_window_ratio,
            max_fallback_selection_rate=args.max_fallback_selection_rate,
        ),
    )
    report = runner.run(
        underlyings=tuple(symbol.upper() for symbol in args.underlyings),
        start=start,
        end=end,
        config=WalkForwardConfig(
            train_span=timedelta(days=args.train_days),
            test_span=timedelta(days=args.test_days),
            step_span=timedelta(days=args.step_days),
        ),
        backtest_config_overrides={
            "signal_lookback_days": args.signal_lookback_days,
            "signal_activation_policy": args.signal_activation_policy,
        },
    )

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(_serialize_report(report), indent=2, sort_keys=True), encoding="utf-8")

    logger.info(
        "completed walk-forward windows=%s promotable=%s report=%s",
        len(report.windows),
        report.promotion.promotable,
        report_path,
    )
    logger.info(
        "oos_trades=%.0f oos_pf=%s oos_sharpe=%.4f fallback_rate=%.4f",
        report.oos_metrics.get("oos_trades", 0.0),
        report.oos_metrics.get("oos_profit_factor"),
        report.oos_metrics.get("oos_sharpe", 0.0),
        report.oos_metrics.get("fallback_selection_rate", 0.0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
