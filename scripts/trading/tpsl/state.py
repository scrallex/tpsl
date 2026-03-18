#!/usr/bin/env python3
from dataclasses import dataclass


@dataclass
class TPSLTradeState:
    """Mutable per-trade state used by TPSLChecker for trailing / BE logic."""

    entry_price: float = 0.0
    direction: int = 0  # +1 long, -1 short
    peak_price: float = 0.0  # best price seen since entry (for trailing)
    breakeven_activated: bool = False

    def reset(self, entry_price: float, direction: int) -> None:
        self.entry_price = float(entry_price)
        self.direction = int(direction)
        self.peak_price = float(entry_price)
        self.breakeven_activated = False

    def update_peak(self, current_price: float) -> None:
        if self.direction > 0:
            self.peak_price = max(self.peak_price, current_price)
        elif self.direction < 0:
            self.peak_price = min(self.peak_price, current_price)
