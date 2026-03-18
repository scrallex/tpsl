"""Run the isolated options backtest against a normalized local dataset."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from options_research.backtest import BacktestConfig, SignalDrivenBacktestRunner
from options_research.data import LocalFileDatasetConfig, LocalFileOptionsDataSource
from options_research.env import load_options_env
from options_research.execution import FillPolicy, PackagedSpreadFillSimulator
from options_research.models import BacktestResult, FilledOptionPosition
from options_research.portfolio import ExitPolicy, PortfolioRiskLimits, SimplePortfolioEngine, SimplePortfolioRiskModel
from options_research.selection import DebitSpreadSelectionConfig, VerticalDebitSpreadSelector
from options_research.signals import (
    MovingAverageSignalConfig,
    MovingAverageSignalGenerator,
    SEPRegimeSignalConfig,
    SEPRegimeSignalGenerator,
)
from options_research.strategies import DirectionalExpressionConfig


logger = logging.getLogger("options_research.run_backtest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="SPY")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp, e.g. 2026-01-20T00:00:00+00:00")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, e.g. 2026-03-06T23:59:00+00:00")
    parser.add_argument(
        "--data-root",
        default=os.getenv("OPTIONS_RESEARCH_DATA_ROOT", "data/options_research/marketdata"),
    )
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--signal-source", choices=("sep", "ma"), default="sep")
    parser.add_argument("--signal-lookback-days", type=int, default=365)
    parser.add_argument("--signal-activation-policy", choices=("next_snapshot", "immediate"), default="next_snapshot")
    parser.add_argument("--signal-short-window", type=int, default=3)
    parser.add_argument("--signal-long-window", type=int, default=7)
    parser.add_argument("--signal-min-gap-pct", type=float, default=0.001)
    parser.add_argument("--sep-min-regime-confidence", type=float, default=0.0)
    parser.add_argument("--gate-root", default=os.getenv("OPTIONS_RESEARCH_GATE_ROOT", "data/options_research/gates"))
    parser.add_argument("--gate-file-pattern", default="{underlying}.gates.jsonl")
    parser.add_argument(
        "--sep-allowed-source",
        action="append",
        default=[],
        help="Repeat to keep only specific SEP gate sources.",
    )
    parser.add_argument("--min-dte", type=int, default=21)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--target-dte", type=int, default=30)
    parser.add_argument("--long-delta-min", type=float, default=0.30)
    parser.add_argument("--long-delta-max", type=float, default=0.45)
    parser.add_argument("--spread-width", type=float, default=5.0)
    parser.add_argument("--short-delta-offset", type=float, default=0.15)
    parser.add_argument("--max-relative-spread", type=float, default=0.25)
    parser.add_argument("--min-open-interest", type=int, default=100)
    parser.add_argument("--min-volume", type=int, default=10)
    parser.add_argument("--strict-delta-selection", action="store_true")
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--take-profit-pct", type=float, default=0.25)
    parser.add_argument("--stop-loss-pct", type=float, default=0.50)
    parser.add_argument("--close-before-expiry-days", type=int, default=1)
    parser.add_argument("--open-penalty-half-spreads", type=float, default=0.50)
    parser.add_argument("--close-penalty-half-spreads", type=float, default=0.50)
    parser.add_argument("--per-contract-commission", type=float, default=0.50)
    parser.add_argument("--per-contract-fee", type=float, default=0.10)
    parser.add_argument("--report-path", default=None)
    return parser


def _report_path(*, underlying: str, start: datetime, end: datetime, value: str | None) -> Path:
    if value is not None:
        return Path(value)
    results_dir = Path("data/options_research/results")
    timestamp = f"{start:%Y%m%d}_{end:%Y%m%d}"
    return results_dir / f"{underlying.upper()}_{timestamp}_backtest.json"


def _serialize_position(position: FilledOptionPosition) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": position.closed_at.isoformat() if position.closed_at is not None else None,
        "status": position.status.value,
        "strategy_family": position.intent.strategy_family.value,
        "signal_direction": position.intent.signal_event.direction.value,
        "entry_net_price": _safe_number(position.entry_fill.net_price),
        "exit_net_price": _safe_number(position.exit_fill.net_price) if position.exit_fill is not None else None,
        "realized_pnl": _safe_number(position.realized_pnl),
        "exit_reason": position.exit_reason.value if position.exit_reason is not None else None,
        "contracts": position.intent.contracts,
        "max_loss": _safe_number(position.intent.max_loss),
        "profit_target": _safe_number(position.intent.profit_target),
        "stop_loss": _safe_number(position.intent.stop_loss),
        "selection_mode": position.intent.metadata.get("long_leg_selection_mode"),
        "legs": [
            {
                "action": leg.action.value,
                "contract_symbol": leg.quote.contract_symbol,
                "expiry": leg.quote.expiry.isoformat(),
                "strike": _safe_number(leg.quote.strike),
                "option_type": leg.quote.option_type.value,
            }
            for leg in position.intent.legs
        ],
    }


def _safe_number(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isfinite(numeric):
        return numeric
    return None


def _serialize_result(result: BacktestResult) -> dict[str, Any]:
    selection_mode_breakdown: dict[str, float] = {}
    exit_reason_breakdown: dict[str, float] = {}
    for position in result.positions:
        selection_mode = str(position.intent.metadata.get("long_leg_selection_mode") or "unknown")
        selection_mode_breakdown[selection_mode] = selection_mode_breakdown.get(selection_mode, 0.0) + 1.0
        if position.exit_reason is not None:
            key = position.exit_reason.value
            exit_reason_breakdown[key] = exit_reason_breakdown.get(key, 0.0) + 1.0
    return {
        "strategy_name": result.strategy_name,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "total_trades": result.total_trades,
        "rejected_entries": result.rejected_entries,
        "metrics": {key: _safe_number(value) for key, value in result.metrics.items()},
        "config": result.config,
        "notes": list(result.notes),
        "selection_mode_breakdown": selection_mode_breakdown,
        "exit_reason_breakdown": exit_reason_breakdown,
        "strategy_summaries": {
            key: {metric: _safe_number(value) for metric, value in metrics.items()}
            for key, metrics in result.strategy_summaries.items()
        },
        "underlying_summaries": {
            key: {metric: _safe_number(value) for metric, value in metrics.items()}
            for key, metrics in result.underlying_summaries.items()
        },
        "positions": [_serialize_position(position) for position in result.positions],
        "equity_curve": [
            {
                "timestamp": point.timestamp.isoformat(),
                "equity": _safe_number(point.equity),
                "cash": _safe_number(point.cash),
                "drawdown": _safe_number(point.drawdown),
            }
            for point in result.equity_curve
        ],
    }


def _build_runner(args: argparse.Namespace) -> SignalDrivenBacktestRunner:
    data_source = LocalFileOptionsDataSource(LocalFileDatasetConfig(root=Path(args.data_root)))

    if args.signal_source == "sep":
        signal_generator = SEPRegimeSignalGenerator(
            SEPRegimeSignalConfig(
                min_regime_confidence=args.sep_min_regime_confidence,
                allowed_sources=tuple(args.sep_allowed_source),
                gate_root=Path(args.gate_root),
                gate_file_pattern=args.gate_file_pattern,
            )
        )
    else:
        signal_generator = MovingAverageSignalGenerator(
            MovingAverageSignalConfig(
                short_window=args.signal_short_window,
                long_window=args.signal_long_window,
                min_gap_pct=args.signal_min_gap_pct,
            )
        )
    selector = VerticalDebitSpreadSelector(
        DebitSpreadSelectionConfig(
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            target_dte=args.target_dte,
            long_delta_min=args.long_delta_min,
            long_delta_max=args.long_delta_max,
            spread_width=args.spread_width,
            short_delta_offset=args.short_delta_offset,
            max_relative_spread=args.max_relative_spread,
            min_open_interest=args.min_open_interest,
            min_volume=args.min_volume,
            allow_moneyness_fallback=not args.strict_delta_selection,
        ),
        expression_config=DirectionalExpressionConfig(
            contracts=args.contracts,
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            close_before_expiry_days=args.close_before_expiry_days,
        ),
    )
    fill_simulator = PackagedSpreadFillSimulator(
        FillPolicy(
            price_reference="mid",
            open_penalty_half_spreads=args.open_penalty_half_spreads,
            close_penalty_half_spreads=args.close_penalty_half_spreads,
            per_contract_commission=args.per_contract_commission,
            per_contract_fee=args.per_contract_fee,
        )
    )
    portfolio_engine = SimplePortfolioEngine(
        fill_simulator=fill_simulator,
        risk_model=SimplePortfolioRiskModel(
            PortfolioRiskLimits(
                max_open_positions=5,
                max_portfolio_max_loss=0.50,
                max_underlying_allocation=0.50,
                max_new_position_max_loss=0.10,
            )
        ),
        exit_policy=ExitPolicy(),
    )
    return SignalDrivenBacktestRunner(
        data_source=data_source,
        signal_generator=signal_generator,
        selector=selector,
        portfolio_engine=portfolio_engine,
    )


def main() -> int:
    load_options_env()
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    report_path = _report_path(underlying=args.underlying, start=start, end=end, value=args.report_path)

    runner = _build_runner(args)
    result = runner.run(
        BacktestConfig(
            underlying=args.underlying.upper(),
            start=start,
            end=end,
            initial_capital=args.initial_capital,
            signal_lookback_days=args.signal_lookback_days,
            signal_activation_policy=args.signal_activation_policy,
        )
    )
    payload = _serialize_result(result)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    logger.info(
        "completed backtest underlying=%s trades=%s rejected=%s total_return=%.6f report=%s",
        args.underlying.upper(),
        result.total_trades,
        result.rejected_entries,
        result.metrics.get("total_return", 0.0),
        report_path,
    )
    logger.info(
        "metrics win_rate=%.4f profit_factor=%s max_drawdown=%.6f",
        result.metrics.get("win_rate", 0.0),
        result.metrics.get("profit_factor"),
        result.metrics.get("max_drawdown", 0.0),
    )
    exit_breakdown = payload.get("config", {}).get("rejection_breakdown", {})
    if exit_breakdown:
        logger.info("rejection_breakdown=%s", exit_breakdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
