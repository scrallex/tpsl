#!/usr/bin/env python3
"""Shared pricing, commission, and metrics utilities for backtesting simulators.

This module provides common financial calculations used across different
simulator implementations, eliminating code duplication and ensuring
consistent pricing logic.
"""
from __future__ import annotations


import math
from datetime import date, datetime
from typing import Dict, List, Sequence, Tuple


def calculate_commission(
    instrument: str, price: float, units: int, cost_bps: float
) -> float:
    """Calculate trading commission in USD.

    Args:
        instrument: Trading instrument (e.g., "EUR_USD")
        price: Execution price
        units: Number of units traded (absolute value)
        cost_bps: Commission cost in basis points

    Returns:
        Commission amount in USD
    """
    if cost_bps <= 0 or units <= 0:
        return 0.0
    notional = abs(float(price) * units)
    raw = notional * (cost_bps / 10_000.0)
    return convert_to_usd(instrument, raw, price)


def convert_to_usd(instrument: str, raw: float, price: float) -> float:
    """Convert instrument-denominated P&L to USD.

    Handles currency conversion based on instrument naming convention:
    - *_USD instruments: Direct USD (no conversion)
    - USD_* instruments: Divide by price
    - Other: Pass through as-is

    Args:
        instrument: Trading instrument
        raw: Raw P&L in instrument currency
        price: Current price for conversion

    Returns:
        P&L amount in USD
    """
    inst = instrument.upper()
    if inst.endswith("_USD"):
        return raw
    if inst.startswith("USD_"):
        if math.isclose(price, 0.0):
            return 0.0
        return raw / price
    return raw


def compute_sharpe(equity_curve: Sequence[Tuple[datetime, float]]) -> float:
    """Compute annualized Sharpe ratio from an equity curve.

    Groups equity values by calendar date, computes daily returns,
    and annualizes using sqrt(252).

    Args:
        equity_curve: Sequence of (timestamp, equity_value) pairs.

    Returns:
        Annualized Sharpe ratio, or 0.0 if insufficient data.
    """
    if len(equity_curve) < 2:
        return 0.0
    daily_equity: Dict[date, float] = {}
    start_equity = equity_curve[0][1]
    for ts, value in equity_curve:
        daily_equity[ts.date()] = value
    returns: List[float] = []
    prev = start_equity
    for day in sorted(daily_equity.keys()):
        value = daily_equity[day]
        if math.isclose(prev, 0.0):
            prev = value
            continue
        returns.append((value - prev) / prev)
        prev = value
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance)
    if math.isclose(std, 0.0):
        return 0.0
    return (mean / std) * math.sqrt(252)


def compute_drawdown(equity_curve: Sequence[Tuple[datetime, float]]) -> float:
    """Compute maximum drawdown from an equity curve.

    Args:
        equity_curve: Sequence of (timestamp, equity_value) pairs.

    Returns:
        Maximum drawdown as an absolute value.
    """
    peak = -float("inf")
    max_dd = 0.0
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd
