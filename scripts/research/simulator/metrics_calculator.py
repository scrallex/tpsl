"""Simulation Results Metrics Calculator."""

import logging
import statistics
from datetime import datetime
from typing import Sequence, Tuple

from scripts.research.simulator.models import TPSLTradeRecord
from scripts.research.simulator.metrics import (
    TPSLSimulationMetrics,
    CoreMetrics,
    ExecutionMetrics,
    ExitMetrics,
    BundleMetrics,
)
from scripts.trading.tpsl import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING_STOP,
    EXIT_BREAKEVEN_STOP,
)

try:
    from .pricing_utils import compute_drawdown, compute_sharpe
except ImportError:
    from scripts.research.simulator.pricing_utils import (
        compute_drawdown,
        compute_sharpe,
    )


def compute_tpsl_metrics(
    equity_curve: Sequence[Tuple[datetime, float]],
    trades: Sequence[TPSLTradeRecord],
    nav: float,
) -> TPSLSimulationMetrics:
    total_pnl = (equity_curve[-1][1] - nav) if equity_curve else 0.0
    return_pct = (total_pnl / nav) if nav else 0.0
    sharpe = compute_sharpe(equity_curve)
    max_dd = compute_drawdown(equity_curve)
    num = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = (len(wins) / num) if num else 0.0
    avg_mae = statistics.mean(abs(t.mae) for t in trades) if trades else 0.0
    avg_mfe = statistics.mean(max(0.0, t.mfe) for t in trades) if trades else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (
        (gross_win / gross_loss)
        if gross_loss > 0
        else float("inf") if gross_win > 0 else 0.0
    )
    avg_hold = 0.0
    if trades:
        durations = [
            (t.exit_time - t.entry_time).total_seconds() / 60.0 for t in trades
        ]
        avg_hold = statistics.mean(durations)
    avg_win = statistics.mean(t.pnl for t in wins) if wins else 0.0
    avg_loss = statistics.mean(t.pnl for t in losses) if losses else 0.0

    # Bundle Metrics
    bundle_trades = [t for t in trades if getattr(t, "is_bundle_trade", False)]
    bundle_count = len(bundle_trades)
    bundle_pnl = sum(t.pnl for t in bundle_trades)
    bundle_wins = [t for t in bundle_trades if t.pnl > 0]
    bundle_win_rate = (len(bundle_wins) / bundle_count) if bundle_count else 0.0

    # Log audit warning
    if trades and (bundle_count / len(trades) > 0.3):
        logging.warning(
            f"AUDIT ALERT: High bundle trade ratio ({bundle_count}/{len(trades)} = {bundle_count/len(trades):.2%})"
        )

    tp_exits = sum(
        1 for t in trades if getattr(t, "exit_reason", "") == EXIT_TAKE_PROFIT
    )
    sl_exits = sum(1 for t in trades if getattr(t, "exit_reason", "") == EXIT_STOP_LOSS)
    trail_exits = sum(
        1 for t in trades if getattr(t, "exit_reason", "") == EXIT_TRAILING_STOP
    )
    be_exits = sum(
        1 for t in trades if getattr(t, "exit_reason", "") == EXIT_BREAKEVEN_STOP
    )
    hazard_exits = sum(
        1 for t in trades if getattr(t, "exit_reason", "") == "hazard_exit"
    )
    time_exits = num - tp_exits - sl_exits - trail_exits - be_exits - hazard_exits

    core = CoreMetrics(
        pnl=total_pnl,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_dd,
        trades=num,
        win_rate=win_rate,
        profit_factor=profit_factor,
    )
    execution = ExecutionMetrics(
        avg_mae=avg_mae,
        avg_mfe=avg_mfe,
        avg_hold_minutes=avg_hold,
        avg_win_pnl=avg_win,
        avg_loss_pnl=avg_loss,
    )
    exits = ExitMetrics(
        tp_exits=tp_exits,
        sl_exits=sl_exits,
        trail_exits=trail_exits,
        be_exits=be_exits,
        hazard_exits=hazard_exits,
        time_exits=time_exits,
        tp_hit_rate=(tp_exits / num) if num else 0.0,
        sl_hit_rate=(sl_exits / num) if num else 0.0,
    )
    bundle = BundleMetrics(
        bundle_trade_count=bundle_count,
        bundle_win_rate=bundle_win_rate,
        bundle_pnl=bundle_pnl,
    )

    return TPSLSimulationMetrics(
        core=core,
        execution=execution,
        exits=exits,
        bundle=bundle,
    )
