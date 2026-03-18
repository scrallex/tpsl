"""Backtest and walk-forward runner interfaces."""

from .runner import BacktestConfig, BacktestRunner, SignalDrivenBacktestRunner
from .walk_forward import (
    PromotionCriteria,
    PromotionEvaluation,
    RollingWalkForwardRunner,
    WalkForwardConfig,
    WalkForwardReport,
    WalkForwardRunner,
    WalkForwardWindow,
    WalkForwardWindowResult,
)

__all__ = [
    "BacktestConfig",
    "BacktestRunner",
    "PromotionCriteria",
    "PromotionEvaluation",
    "RollingWalkForwardRunner",
    "SignalDrivenBacktestRunner",
    "WalkForwardConfig",
    "WalkForwardReport",
    "WalkForwardRunner",
    "WalkForwardWindow",
    "WalkForwardWindowResult",
]
