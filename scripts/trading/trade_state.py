#!/usr/bin/env python3
"""Execution state and active trade tracking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ActiveTrade:
    direction: int
    units: int
    entry_ts: float
    hold_secs: int
    max_hold_secs: Optional[int]
    elapsed_secs: int = 0
    entry_price: float = 0.0
    peak_favorable_price: float = 0.0
    breakeven_activated: bool = False

    def net_units(self) -> int:
        return self.direction * self.units

    def extend_hold(self, additional_secs: int, max_limit_secs: Optional[int]) -> bool:
        new_target = self.hold_secs + additional_secs
        if max_limit_secs is not None and new_target > max_limit_secs:
            new_target = max_limit_secs
        if new_target > self.hold_secs:
            self.hold_secs = new_target
            return True
        return False


@dataclass
class TradeStateSnapshot:
    trades: List[ActiveTrade]
    pending_close: bool
    last_signal: Optional[str]


class TradeStateStore:
    def __init__(self) -> None:
        self._trades: Dict[str, List[ActiveTrade]] = {}
        self._pending_close: set[str] = set()
        self._last_entry_signal: Dict[str, str] = {}

    def trade_count(self, instrument: str) -> int:
        """Return the number of active trades for one instrument."""
        return len(self._trades.get(instrument.upper(), []))

    def total_trade_count(self) -> int:
        """Return the total number of active trades across the portfolio."""
        return sum(len(trades) for trades in self._trades.values())

    def get_trades(self, instrument: str) -> List[ActiveTrade]:
        """Get a copy of all active trades for a specific instrument.

        Args:
            instrument: The instrument symbol.

        Returns:
            A list of active trades.
        """
        trades = self._trades.get(instrument.upper(), [])
        return [
            ActiveTrade(
                t.direction,
                t.units,
                t.entry_ts,
                t.hold_secs,
                t.max_hold_secs,
                t.elapsed_secs,
                t.entry_price,
                getattr(t, "peak_favorable_price", t.entry_price),
                getattr(t, "breakeven_activated", False),
            )
            for t in trades
        ]

    def replace_trades(self, instrument: str, trades: List[ActiveTrade]) -> None:
        """Replace the list of active trades for an instrument.

        Args:
            instrument: The instrument symbol.
            trades: The new list of active trades.
        """
        inst = instrument.upper()
        if trades:
            self._trades[inst] = trades
        else:
            self._trades.pop(inst, None)

    def remove_trades(self, instrument: str) -> None:
        """Remove all active trades and pending states for an instrument.

        Args:
            instrument: The instrument symbol.
        """
        inst = instrument.upper()
        self._trades.pop(inst, None)
        self._pending_close.discard(inst)
        self._last_entry_signal.pop(inst, None)

    def has_trades(self, instrument: str) -> bool:
        """Check if an instrument has any active trades.

        Args:
            instrument: The instrument symbol.

        Returns:
            True if trades exist, False otherwise.
        """
        return bool(self._trades.get(instrument.upper()))

    def mark_pending_close(self, instrument: str) -> None:
        """Mark an instrument's trades as pending closure.

        Args:
            instrument: The instrument symbol.
        """
        self._pending_close.add(instrument.upper())

    def clear_pending_close(self, instrument: str) -> None:
        """Clear the pending closure flag for an instrument.

        Args:
            instrument: The instrument symbol.
        """
        self._pending_close.discard(instrument.upper())

    def is_pending_close(self, instrument: str) -> bool:
        """Check if an instrument is marked for pending closure.

        Args:
            instrument: The instrument symbol.

        Returns:
            True if pending close, False otherwise.
        """
        return instrument.upper() in self._pending_close

    def set_last_signal(self, instrument: str, signal_key: str) -> None:
        """Record the key of the last entry signal processed for an instrument.

        Args:
            instrument: The instrument symbol.
            signal_key: The unique signal identifier.
        """
        if signal_key:
            self._last_entry_signal[instrument.upper()] = signal_key

    def clear_last_signal(self, instrument: str) -> None:
        """Clear the last recorded signal key for an instrument.

        Args:
            instrument: The instrument symbol.
        """
        self._last_entry_signal.pop(instrument.upper(), None)

    def get_last_signal(self, instrument: str) -> Optional[str]:
        """Retrieve the key of the last entry signal for an instrument.

        Args:
            instrument: The instrument symbol.

        Returns:
            The signal key if recorded, None otherwise.
        """
        return self._last_entry_signal.get(instrument.upper())

    def snapshot(self, instrument: str) -> TradeStateSnapshot:
        """Capture the mutable trade state for a single instrument."""
        inst = instrument.upper()
        return TradeStateSnapshot(
            trades=self.get_trades(inst),
            pending_close=inst in self._pending_close,
            last_signal=self._last_entry_signal.get(inst),
        )

    def restore(self, instrument: str, snapshot: TradeStateSnapshot) -> None:
        """Restore a previously captured instrument state snapshot."""
        inst = instrument.upper()
        self.replace_trades(inst, snapshot.trades)
        if snapshot.pending_close:
            self._pending_close.add(inst)
        else:
            self._pending_close.discard(inst)
        if snapshot.last_signal:
            self._last_entry_signal[inst] = snapshot.last_signal
        else:
            self._last_entry_signal.pop(inst, None)
