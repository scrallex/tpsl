"""Backtest simulator package reusing live trading primitives."""

__all__ = [
    "TPSLBacktestSimulator",
    "TPSLSimulationParams",
    "TPSLSimulationResult",
    "derive_signals",
]


def __getattr__(name: str):
    if name in {
        "TPSLBacktestSimulator",
        "TPSLSimulationParams",
        "TPSLSimulationResult",
    }:
        from .backtest_simulator import (
            TPSLBacktestSimulator,
            TPSLSimulationParams,
            TPSLSimulationResult,
        )

        return {
            "TPSLBacktestSimulator": TPSLBacktestSimulator,
            "TPSLSimulationParams": TPSLSimulationParams,
            "TPSLSimulationResult": TPSLSimulationResult,
        }[name]
    if name == "derive_signals":
        from .signal_deriver import derive_signals

        return derive_signals
    raise AttributeError(name)
