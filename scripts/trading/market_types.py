"""Shared runtime market data types."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    """Minimal candle representation used by the live trading path."""

    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: Optional[float] = None
