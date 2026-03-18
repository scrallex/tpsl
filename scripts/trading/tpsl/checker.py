#!/usr/bin/env python3
import math
from typing import Tuple

from .config import TPSLConfig
from .state import TPSLTradeState

EXIT_STOP_LOSS = "stop_loss_hit"
EXIT_TAKE_PROFIT = "take_profit_hit"
EXIT_TRAILING_STOP = "trailing_stop_hit"
EXIT_BREAKEVEN_STOP = "breakeven_stop_hit"


class TPSLChecker:
    """Stateless evaluator – call ``check()`` on each tick / candle close."""

    @staticmethod
    def check(
        instrument: str,
        current_price: float,
        state: TPSLTradeState,
        config: TPSLConfig,
    ) -> Tuple[bool, str, float]:
        if not config.active or state.direction == 0:
            return False, "", current_price

        entry = state.entry_price
        direction = state.direction
        if math.isclose(entry, 0.0):
            return False, "", current_price

        pnl_frac = ((current_price - entry) / entry) * direction

        # 1. Stop-loss
        sl_dist = config.effective_sl(instrument, entry)
        if sl_dist is not None and pnl_frac <= -sl_dist:
            trigger = entry * (1.0 - sl_dist * direction)
            return True, EXIT_STOP_LOSS, trigger

        # 2. Take-profit
        tp_dist = config.effective_tp(instrument, entry)
        if tp_dist is not None and pnl_frac >= tp_dist:
            trigger = entry * (1.0 + tp_dist * direction)
            return True, EXIT_TAKE_PROFIT, trigger

        # 3. Breakeven
        be_trigger = config.breakeven_trigger_pct
        if be_trigger is not None and not state.breakeven_activated:
            if pnl_frac >= abs(be_trigger):
                state.breakeven_activated = True

        if state.breakeven_activated:
            if (current_price - entry) * direction <= 0:
                return True, EXIT_BREAKEVEN_STOP, entry

        # 4. Trailing stop
        trail_pct = config.trailing_stop_pct
        if trail_pct is not None:
            # Inject ALPHA_SCALP dynamic tension tightening
            try:
                from scripts.trading.structural_circuit_breaker import (
                    StructuralCircuitBreaker,
                )

                if StructuralCircuitBreaker.get_instance().is_scalping_regime:
                    trail_pct = trail_pct / 2.0  # Tighten stop by 50%
            except ImportError:
                pass

            state.update_peak(current_price)
            if (state.peak_price - entry) * direction > 0:
                trail_level = state.peak_price * (1.0 - abs(trail_pct) * direction)
                if (current_price - trail_level) * direction <= 0:
                    return True, EXIT_TRAILING_STOP, trail_level

        return False, "", current_price

    @staticmethod
    def check_intra_candle(
        instrument: str,
        high: float,
        low: float,
        state: TPSLTradeState,
        config: TPSLConfig,
    ) -> Tuple[bool, str, float]:
        if not config.active or state.direction == 0:
            return False, "", 0.0

        entry = state.entry_price
        direction = state.direction
        if math.isclose(entry, 0.0):
            return False, "", 0.0

        sl_dist = config.effective_sl(instrument, entry)
        tp_dist = config.effective_tp(instrument, entry)

        adverse_price = low if direction > 0 else high
        favorable_price = high if direction > 0 else low

        if sl_dist is not None:
            sl_price = entry * (1.0 - sl_dist * direction)
            if (adverse_price - sl_price) * direction <= 0:
                return True, EXIT_STOP_LOSS, sl_price

        if tp_dist is not None:
            tp_price = entry * (1.0 + tp_dist * direction)
            if (favorable_price - tp_price) * direction >= 0:
                return True, EXIT_TAKE_PROFIT, tp_price

        be_trigger = config.breakeven_trigger_pct
        if be_trigger is not None:
            if not state.breakeven_activated:
                pnl_fav = ((favorable_price - entry) / entry) * direction
                if pnl_fav >= abs(be_trigger):
                    state.breakeven_activated = True

            if state.breakeven_activated:
                if (adverse_price - entry) * direction <= 0:
                    return True, EXIT_BREAKEVEN_STOP, entry

        trail_pct = config.trailing_stop_pct
        if trail_pct is not None:
            # Inject ALPHA_SCALP dynamic tension tightening
            try:
                from scripts.trading.structural_circuit_breaker import (
                    StructuralCircuitBreaker,
                )

                if StructuralCircuitBreaker.get_instance().is_scalping_regime:
                    trail_pct = trail_pct / 2.0  # Tighten stop by 50%
            except ImportError:
                pass

            state.update_peak(favorable_price)
            if (state.peak_price - entry) * direction > 0:
                trail_level = state.peak_price * (1.0 - abs(trail_pct) * direction)
                if (adverse_price - trail_level) * direction <= 0:
                    return True, EXIT_TRAILING_STOP, trail_level

        return False, "", 0.0
