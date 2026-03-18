#!/usr/bin/env python3
"""Trade stack evaluation and allocation."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .gate_loader import StrategyProfile
from .risk_limits import RiskManager
from .trade_state import TradeStateStore
from .risk_calculator import RiskSizer
from .execution_engine import ExecutionEngine
from .session_policy import SessionPolicy
from .gate_validation import gate_evaluation, relaxed_gate_profile

logger = logging.getLogger(__name__)

class TradeStackProcessor:
    def __init__(
        self,
        strategy: StrategyProfile,
        session_policy: SessionPolicy,
        risk_manager: RiskManager,
        trade_state: TradeStateStore,
        risk_sizer: RiskSizer,
        execution_engine: ExecutionEngine,
        hold_seconds: int,
    ) -> None:
        self.strategy = strategy
        self.session_policy = session_policy
        self.risk_manager = risk_manager
        self.trade_state = trade_state
        self.risk_sizer = risk_sizer
        self.execution_engine = execution_engine
        self.hold_seconds = hold_seconds
        self.exposure_scale = float(os.getenv("EXPOSURE_SCALE", "0.02") or 0.02)
        self.entry_cooldown_secs = max(
            0, int(os.getenv("TRADE_ENTRY_COOLDOWN_SECONDS", "60") or 60)
        )
        self._last_entry_ts: Dict[str, float] = {}

    def restore_entry_cooldown(self, instrument: str, entry_ts: float) -> None:
        if entry_ts > 0:
            self._last_entry_ts[instrument.upper()] = float(entry_ts)

    def clear_entry_cooldown(self, instrument: str) -> None:
        self._last_entry_ts.pop(instrument.upper(), None)

    def process_instrument(
        self,
        instrument: str,
        gate_info: Dict[str, Any],
        price_data: Dict[str, Optional[float]],
        per_trade_exposure: float,
        nav_snapshot: float,
        price_cache: Dict[str, Dict[str, Optional[float]]],
        tracker: Any,
    ) -> None:
        """Evaluate trading conditions and generate allocations for an instrument.

        Validates session rules, global risk limits, and signal thresholds to
        determine the optimal trade size and hold duration, then dispatches the
        trade to the execution engine.

        Args:
            instrument: The instrument symbol.
            gate_info: Data from the regime manifold gate including signals.
            price_data: Current pricing details (bid, ask, mid).
            per_trade_exposure: The target exposure per trade.
            nav_snapshot: The current Net Asset Value of the portfolio.
            price_cache: In-memory cache of recent prices.
            tracker: Exposure/ticket tracker that also executes broker deltas.
        """
        now = datetime.now(timezone.utc)
        now_ts = time.time()
        current_units = self.risk_manager.net_units(instrument)
        has_position = current_units != 0 or self.trade_state.has_trades(instrument)
        prior_trade_count = self.trade_state.trade_count(instrument)

        decision = self.session_policy.evaluate(instrument, now, has_position)
        profile = self.strategy.get(instrument)
        effective_profile = (
            relaxed_gate_profile(profile)
            if getattr(profile, "ml_primary_gate", False)
            else profile
        )
        admitted, gate_reasons = gate_evaluation(gate_info, effective_profile)
        is_bundle_entry = False

        hard_blocks: List[str] = []
        if not decision.tradable:
            hard_blocks.append(decision.reason)
        if not admitted:
            gate_reasons.append("gate_blocked")

        cooldown_active = False
        last_entry_ts = float(self._last_entry_ts.get(instrument.upper(), 0.0) or 0.0)
        if (
            self.entry_cooldown_secs > 0
            and last_entry_ts > 0.0
            and (now_ts - last_entry_ts) < float(self.entry_cooldown_secs)
        ):
            cooldown_active = True
            admitted = False
            if "global_cooldown" not in gate_reasons:
                gate_reasons.append("global_cooldown")
            if "cooldown_active" not in gate_reasons:
                gate_reasons.append("cooldown_active")

        # Phase 9: Global Risk Lock
        # Check if the portfolio is overexposed to USD-correlated risk.
        # We cap total portfolio heat at 2.0R to prevent systemic blowups from macro news gaps.
        # Use tracked USD exposure rather than raw position count.

        max_total_exposure = float(self.risk_manager.limits.max_total_exposure or 0.0)
        active_exposure = float(self.risk_manager.exposure())

        if not has_position and max_total_exposure > 0 and active_exposure >= max_total_exposure:
            admitted = False
            gate_reasons.append("global_risk_lock_exceeded")
            logger.warning(
                "Global Risk Lock triggering for %s! Exposure %.2f / %.2f USD",
                instrument,
                active_exposure,
                max_total_exposure,
            )

        target_units = 0
        requested_side = 0
        direction = None
        hold_secs = self.hold_seconds
        signal_key = str(gate_info.get("signal_key") or "")
        trade_exposure = per_trade_exposure
        raw_dir = str(gate_info.get("direction", "")).upper()
        if raw_dir not in {"BUY", "SELL"}:
            if not getattr(profile, "allow_fallback", True):
                logger.debug(
                    "No directional gate for %s and fallback is disabled.",
                    instrument,
                )
            return

        if getattr(profile, "invert_bundles", False):
            direction = "SELL" if raw_dir == "BUY" else "BUY"
        else:
            direction = raw_dir

        requested_side = self._direction_to_side(direction)
        hold_secs = 3600
        if profile and profile.hold_minutes is not None:
            hold_secs = profile.hold_minutes * 60
        signal_key = f"gate:{gate_info.get('ts_ms')}"

        # Phase 9: Western Macro-Alignment Enforcement
        # Ensure that whatever direction we are ultimately taking (after inversion)
        # fundamentally aligns with the underlying 200-SMA regime structure.
        from scripts.trading.gate_validation import _regime_payload

        regime_label, _ = _regime_payload(gate_info)
        if (
            profile
            and profile.regime_filter
            and not getattr(profile, "ml_primary_gate", False)
            and regime_label
        ):
            if requested_side == 1 and regime_label != "long_ok":
                gate_reasons.append("regime_direction_mismatch")
                admitted = False
            elif requested_side == -1 and regime_label != "short_ok":
                gate_reasons.append("regime_direction_mismatch")
                admitted = False

        stop_loss_price = 0.0
        take_profit_price = 0.0

        if admitted and decision.tradable and requested_side:
            entry_price = float(price_data.get("mid") or 0.0)

            # Phase 9: Dynamic Lot Sizing (R Translation)
            action_state = str(gate_info.get("action", "")).upper()
            trap_high = float(gate_info.get("trap_door_high", 0.0))
            trap_low = float(gate_info.get("trap_door_low", 0.0))

            if (
                profile
                and profile.stop_loss_pct is not None
                and profile.stop_loss_pct > 0
            ):
                # Keep TP/SL parity with the backtest while using the same scalar
                # position sizing model that the simulator currently replays.
                stop_loss_price = entry_price * (
                    1.0 - (profile.stop_loss_pct * requested_side)
                )
                if profile.take_profit_pct is not None and profile.take_profit_pct > 0:
                    take_profit_price = entry_price * (
                        1.0 + (profile.take_profit_pct * requested_side)
                    )

                target_units, _, _ = self.risk_sizer.target_units(
                    instrument,
                    target_exposure=trade_exposure,
                    exposure_scale=self.exposure_scale,
                    price_data=price_data,  # type: ignore[arg-type]
                    auxiliary_prices=price_cache,  # type: ignore[arg-type]
                )
            elif action_state == "ARMED" and trap_high > 0 and trap_low > 0:
                # Use Geometric SL bounding
                if requested_side == 1:  # LONG
                    stop_loss_price = trap_low
                else:  # SHORT
                    stop_loss_price = trap_high

                target_units, _ = self.risk_sizer.target_position_size_for_r(
                    instrument=instrument,
                    nav_snapshot=nav_snapshot,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss_price,
                    auxiliary_prices=price_cache,
                )
            else:
                # Fallback to scalar sizing
                target_units, _, _ = self.risk_sizer.target_units(
                    instrument,
                    target_exposure=trade_exposure,
                    exposure_scale=float(os.getenv("EXPOSURE_SCALE", "0.02") or 0.02),
                    price_data=price_data,  # type: ignore
                    auxiliary_prices=price_cache,  # type: ignore
                )

        self.execution_engine.execute_allocation(
            instrument=instrument,
            now_ts=now_ts,
            gate_entry_ready=admitted and decision.tradable,
            gate_reasons=gate_reasons,
            direction=direction,
            requested_side=requested_side,
            scaled_units_abs=abs(target_units),
            hold_secs=hold_secs,
            signal_key=signal_key,
            hard_blocks=hard_blocks,
            current_price=price_data.get("mid") or 0.0,
            timestamp=now,
            is_bundle_entry=is_bundle_entry,
            execute_callback=tracker.execute_delta,
            tracker=tracker,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )

        if self.trade_state.trade_count(instrument) > prior_trade_count:
            self._last_entry_ts[instrument.upper()] = now_ts

    def _direction_to_side(self, direction: Optional[str]) -> int:
        if direction == "BUY":
            return 1
        if direction == "SELL":
            return -1
        return 0

    def _close_stale_position_if_needed(
        self,
        instrument: str,
        current_units: int,
        price_data: Optional[Mapping[str, Optional[float]]],
        reason: str,
        execute_callback: Any,
    ) -> None:
        has_trades = self.trade_state.has_trades(instrument)
        if has_trades:
            logger.debug(
                "Bundle trade active for %s; skipping auto-close (%s)",
                instrument,
                reason,
            )
            return
        if not current_units:
            return
        self.trade_state.remove_trades(instrument)
        mid_price = price_data.get("mid") if price_data else None
        logger.info("Closing %s units for %s (%s)", current_units, instrument, reason)
        execute_callback(instrument, -current_units, mid_price)
