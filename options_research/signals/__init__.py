"""Underlying-driven signal interfaces and placeholder implementations."""

from .base import SignalContext, UnderlyingSignalGenerator
from .placeholder import MovingAverageSignalConfig, MovingAverageSignalGenerator
from .sep_regime import SEPRegimeSignalConfig, SEPRegimeSignalGenerator

__all__ = [
    "MovingAverageSignalConfig",
    "MovingAverageSignalGenerator",
    "SEPRegimeSignalConfig",
    "SEPRegimeSignalGenerator",
    "SignalContext",
    "UnderlyingSignalGenerator",
]
