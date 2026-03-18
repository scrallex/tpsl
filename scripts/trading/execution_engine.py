#!/usr/bin/env python3
"""Shared execution engine for live trading and backtesting.

Unifies TP/SL enforcement, position sizing, and trade planning.
"""
from __future__ import annotations


import logging
from typing import Any, List, Optional

from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.risk_limits import RiskManager
from scripts.trading.trade_planner import TradePlanner
from scripts.trading.trade_state import TradeStateStore
from scripts.trading.tpsl import TPSLChecker, TPSLConfig

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Core execution engine handling sizing, planning, and TP/SL enforcement."""

    def __init__(
        self,
        risk_manager: RiskManager,
        trade_state: TradeStateStore,
        risk_sizer: RiskSizer,
        trade_planner: TradePlanner,
        cost_bps: float = 1.5,
    ) -> None:
        self.risk_manager = risk_manager
        self.trade_state = trade_state
        self.risk_sizer = risk_sizer
        self.trade_planner = trade_planner
        self.cost_bps = cost_bps

    def check_tpsl_exit_intra_candle(
        self,
        instrument: str,
        high: float,
        low: float,
        timestamp: Any,
        tpsl_config: TPSLConfig,
        tracker: Any,
    ) -> bool:
        """Check TP/SL within a candle using high/low."""
        if not tracker.has_position(instrument) or not tpsl_config.active:
            return False

        tickets = tracker.get_tickets(instrument)
        to_close = []
        for i, ticket in enumerate(tickets):
            should_exit, reason, trigger_price = TPSLChecker.check_intra_candle(
                instrument, high, low, ticket.tpsl_state, tpsl_config
            )
            if should_exit:
                to_close.append((i, reason, trigger_price))

        if to_close:
            trades = self.trade_state.get_trades(instrument)
            closed_any = False
            for i, reason, trigger_price in sorted(to_close, reverse=True):
                if i >= len(trades):
                    continue
                trade = trades[i]
                close_result = tracker.close_ticket(
                    instrument, i, trigger_price, timestamp, reason, trigger_price
                )
                if not close_result:
                    logger.warning(
                        "Failed to close %s[%d] for %s; leaving trade state intact",
                        instrument,
                        i,
                        reason,
                    )
                    continue
                closed_any = True
                from scripts.trading.structural_circuit_breaker import (
                    StructuralCircuitBreaker,
                )

                breaker = StructuralCircuitBreaker.get_instance()

                # Calculate PL and duration assuming trigger price is fill
                pnl = (
                    (trigger_price - trade.entry_price) / trade.entry_price
                ) * trade.direction
                duration_secs = trade.elapsed_secs
                breaker.record_closed_trade(instrument, pnl, duration_secs)

                if i < len(trades):
                    trades.pop(i)

            if not closed_any:
                return False

            if trades:
                self.trade_state.replace_trades(instrument, trades)
            else:
                self.trade_state.remove_trades(instrument)

            self._sync_risk_after_tracker_exit(
                instrument,
                tracker,
                float(trigger_price),
                float(tickets[0].entry_price) if tickets else float(trigger_price),
            )
            return True
        return False

    def check_time_expiry(
        self,
        instrument: str,
        now_ts: float,
        current_price: float,
        timestamp: Any,
        tracker: Any,
        tick_elapsed_secs: int,
    ) -> bool:
        """Explicitly enforce hold limits and forcefully close expired trades.

        Phase 9 Enforcer Logic:
        1. 15-Minute TID (Time-In-Drawdown): If bars_held == 15 AND unrealized_pnl < 0, force exit.
        2. 60-Minute Harvest: If bars_held == 60, force exit.
        """
        if self.risk_manager.net_units(instrument) == 0:
            return False

        trades = self.trade_state.get_trades(instrument)
        if not trades:
            return False

        to_close = []
        for i, trade in enumerate(trades):
            trade.elapsed_secs += tick_elapsed_secs

            max_limit = trade.max_hold_secs
            is_expired = (
                max_limit is not None and trade.elapsed_secs >= max_limit
            ) or trade.elapsed_secs >= trade.hold_secs

            if is_expired:
                to_close.append(i)

        if not to_close:
            self.trade_state.replace_trades(instrument, trades)

        if to_close:
            closed_any = False
            for i in sorted(to_close, reverse=True):
                if i >= len(trades):
                    continue
                trade = trades[i]
                close_result = tracker.close_ticket(
                    instrument,
                    i,
                    current_price,
                    timestamp,
                    "hold_expiry",
                    current_price,
                )
                if not close_result:
                    logger.warning(
                        "Failed to close expired %s[%d]; leaving trade state intact",
                        instrument,
                        i,
                    )
                    continue
                closed_any = True
                from scripts.trading.structural_circuit_breaker import (
                    StructuralCircuitBreaker,
                )

                breaker = StructuralCircuitBreaker.get_instance()
                pnl = (
                    (current_price - trade.entry_price) / trade.entry_price
                ) * trade.direction
                breaker.record_closed_trade(instrument, pnl, trade.elapsed_secs)

                trades.pop(i)

            if not closed_any:
                return False

            if trades:
                self.trade_state.replace_trades(instrument, trades)
            else:
                self.trade_state.remove_trades(instrument)

            self._sync_risk_after_tracker_exit(
                instrument,
                tracker,
                current_price,
                current_price,
            )
            return True
        return False

    def execute_allocation(
        self,
        instrument: str,
        now_ts: float,
        gate_entry_ready: bool,
        gate_reasons: List[str],
        direction: Optional[str],
        requested_side: int,
        scaled_units_abs: int,
        hold_secs: int,
        signal_key: str,
        hard_blocks: List[str],
        current_price: float,
        timestamp: Any,
        is_bundle_entry: bool,
        execute_callback: Any,
        tracker: Optional[Any] = None,
        disable_stacking: bool = False,
        tick_elapsed_secs: int = 5,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> None:
        """Plan and execute a position change."""

        from scripts.trading.structural_circuit_breaker import StructuralCircuitBreaker

        breaker = StructuralCircuitBreaker.get_instance()
        if breaker.is_blocked:
            gate_entry_ready = False
            gate_reasons.append("structural_circuit_breaker_active")

        # 1. Evaluate explicit time-expiry on all active tracker slots
        if tracker:
            self.check_time_expiry(
                instrument, now_ts, current_price, timestamp, tracker, tick_elapsed_secs
            )

        # 2. Re-read net units after potential time-expiry closures
        current_units = self.risk_manager.net_units(instrument)
        prior_state = self.trade_state.snapshot(instrument)

        outcome = self.trade_planner.plan_allocation(
            instrument,
            now_ts=now_ts,
            current_units=current_units,
            gate_entry_ready=gate_entry_ready,
            gate_reasons=gate_reasons,
            direction=direction,
            requested_side=requested_side,
            scaled_units_abs=abs(scaled_units_abs),
            hold_secs=hold_secs,
            max_hold_limit=None,
            signal_key=signal_key,
            hard_blocks=hard_blocks,
            current_price=current_price,
            disable_stacking=disable_stacking,
            max_positions_per_pair=self.risk_manager.limits.max_positions_per_pair,
            max_total_positions=self.risk_manager.limits.max_total_positions,
        )

        delta_units = outcome.target_units - current_units
        if delta_units == 0:
            return

        if delta_units and not self.risk_manager.can_add(
            instrument,
            delta_units,
            price=current_price,
        ):
            logger.warning(
                "Risk limits rejected %s delta=%s target=%s exposure=%.2f/%.2f",
                instrument,
                delta_units,
                outcome.target_units,
                self.risk_manager.exposure(),
                self.risk_manager.limits.max_total_exposure,
            )
            self.trade_state.restore(instrument, prior_state)
            return

        # Pipe the Trap Door SL through to the executor
        kwargs = {}
        if stop_loss_price is not None and stop_loss_price > 0:
            kwargs["stop_loss_price"] = stop_loss_price
        if take_profit_price is not None and take_profit_price > 0:
            kwargs["take_profit_price"] = take_profit_price

        executed = bool(
            execute_callback(instrument, delta_units, current_price, **kwargs)
        )
        if not executed:
            logger.warning(
                "Execution callback failed for %s delta=%s; restoring trade state",
                instrument,
                delta_units,
            )
            self.trade_state.restore(instrument, prior_state)
            return

        if tracker and hasattr(tracker, "sync_to_net_position"):
            tracker.sync_to_net_position(
                instrument,
                outcome.target_units,
                current_price,
                timestamp,
                is_bundle=is_bundle_entry,
            )

    def _sync_risk_after_tracker_exit(
        self,
        instrument: str,
        tracker: Any,
        current_price: float,
        fallback_price: float,
    ) -> None:
        if hasattr(tracker, "sync_risk_manager"):
            tracker.sync_risk_manager(instrument, current_price)
            return

        self.risk_manager.flatten(instrument)
        net = tracker.net_units(instrument)
        if net != 0:
            self.risk_manager.record_fill(
                instrument,
                net,
                float(current_price or fallback_price),
            )
