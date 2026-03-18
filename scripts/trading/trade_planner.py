#!/usr/bin/env python3
"""Logic for evaluating constraints and executing trade plans."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from scripts.trading.trade_state import ActiveTrade, TradeStateStore


@dataclass
class TradePlanOutcome:
    target_units: int
    gate_entry_ready: bool
    gate_reasons: List[str]
    direction: Optional[str]
    requested_side: int
    state_changed: bool


class TradePlanner:
    def __init__(self, store: TradeStateStore) -> None:
        self._store = store

    def _evaluate_hard_blocks(
        self, inst: str, trades: List[ActiveTrade], state_changed: bool
    ) -> Tuple[bool, bool, int]:
        if trades:
            self._store.remove_trades(inst)
            self._store.mark_pending_close(inst)
            self._store.clear_last_signal(inst)
        else:
            self._store.remove_trades(inst)
        return False, bool(trades) or state_changed, 0

    def plan_allocation(
        self,
        instrument: str,
        *,
        now_ts: float,
        current_units: int,
        gate_entry_ready: bool,
        gate_reasons: List[str],
        direction: Optional[str],
        requested_side: int,
        scaled_units_abs: int,
        hold_secs: int,
        max_hold_limit: Optional[float],
        signal_key: str,
        hard_blocks: List[str],
        current_price: float = 0.0,
        disable_stacking: bool = False,
        max_positions_per_pair: Optional[int] = 5,
        max_total_positions: Optional[int] = None,
    ) -> TradePlanOutcome:
        """Evaluate trade constraints to determine the final allocation and state changes.

        Validates hard blocks, stacking limits, and duplicate signals. Updates the
        trade state store if the allocation proceeds.

        Args:
            instrument: The instrument symbol.
            now_ts: Current timestamp in seconds.
            current_units: Existing net units for the instrument.
            gate_entry_ready: Boolean indicating if the gate allows entry.
            gate_reasons: List of reasons from gate evaluation.
            direction: Requested direction (e.g. 'BUY' or 'SELL').
            requested_side: Numeric side mapped from direction (1 or -1).
            scaled_units_abs: The absolute calculated trade size.
            hold_secs: Target hold duration in seconds.
            max_hold_limit: Global override for max holding time.
            signal_key: Unique identifier for the trade signal.
            hard_blocks: List of external blocking constraints.
            current_price: Execution price entry point.
            disable_stacking: Whether stacking multiple trades is prevented.

        Returns:
            TradePlanOutcome detailing the final decision and state changes.
        """
        inst = instrument.upper()
        trades = self._store.get_trades(inst)
        state_changed = False

        net_from_trades = sum(t.net_units() for t in trades)
        if self._store.is_pending_close(inst) and net_from_trades == current_units:
            self._store.clear_pending_close(inst)
            state_changed = True

        if hard_blocks:
            gate_ready, state_changed, target = self._evaluate_hard_blocks(
                inst, trades, state_changed
            )
            return TradePlanOutcome(
                target_units=target,
                gate_entry_ready=gate_ready,
                gate_reasons=gate_reasons + hard_blocks,
                direction=direction,
                requested_side=requested_side,
                state_changed=state_changed,
            )

        if disable_stacking and net_from_trades != 0 and gate_entry_ready:
            gate_entry_ready = False
            gate_reasons.append("stacking_disabled")

        current_side = 1 if net_from_trades > 0 else (-1 if net_from_trades < 0 else 0)
        if current_side and requested_side and requested_side != current_side:
            gate_entry_ready = False
            gate_reasons.append("opposite_side_blocked")

        if gate_entry_ready and requested_side != 0 and scaled_units_abs > 0:
            last_signal = self._store.get_last_signal(inst)
            if signal_key and signal_key == last_signal:
                gate_entry_ready = False
                gate_reasons.append("duplicate_signal")
            elif (
                max_positions_per_pair is not None
                and max_positions_per_pair > 0
                and len(trades) >= max_positions_per_pair
            ):
                gate_entry_ready = False
                gate_reasons.append("max_stack_limit_reached")
            elif (
                max_total_positions is not None
                and max_total_positions > 0
                and self._store.total_trade_count() >= max_total_positions
            ):
                gate_entry_ready = False
                gate_reasons.append("max_total_positions_reached")
            else:
                trades.append(
                    ActiveTrade(
                        direction=requested_side,
                        units=int(scaled_units_abs),
                        entry_ts=now_ts,
                        hold_secs=hold_secs,
                        max_hold_secs=int(max_hold_limit) if max_hold_limit else None,
                        elapsed_secs=0,
                        entry_price=current_price,
                    )
                )
                self._store.set_last_signal(inst, signal_key)
                state_changed = True

        if trades:
            self._store.replace_trades(inst, trades)
        else:
            self._store.remove_trades(inst)

        return TradePlanOutcome(
            target_units=sum(t.net_units() for t in trades),
            gate_entry_ready=gate_entry_ready,
            gate_reasons=gate_reasons,
            direction=direction,
            requested_side=requested_side,
            state_changed=state_changed,
        )
