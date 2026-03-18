#!/usr/bin/env python3
"""
Live Structural Sequence Circuit Breaker.
Converts real-time PL/Duration motifs into synthetic code blocks to query
the Manifold Engine's non-von Neumann sequence entropy in O(1).
"""
import collections
import logging

try:
    from src.manifold.sidecar import encode_text
except ImportError:
    # Handle the fact that we might be running in a test environment without the C++ core
    encode_text = None

logger = logging.getLogger(__name__)

# P80 Max Chaos Threshold calculated from Window 15-18 flash-crash analysis
MAX_CHAOS_THRESHOLD = 0.8084
SCALP_CHAOS_THRESHOLD = 0.60


class StructuralCircuitBreaker:
    _instance = None

    @classmethod
    def get_instance(cls) -> "StructuralCircuitBreaker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, buffer_size: int = 50) -> None:
        self.trade_buffer = collections.deque(maxlen=buffer_size)
        self.is_blocked = False
        self.is_scalping_regime = False
        self.current_hazard = 0.0

    def record_closed_trade(
        self, instrument: str, pnl: float, duration_secs: int
    ) -> None:
        duration_hours = duration_secs / 3600.0

        if pnl > 0.1:
            pl_token = "WIN_H"
        elif pnl > 0:
            pl_token = "WIN_L"
        elif pnl < -0.1:
            pl_token = "LOSS_H"
        elif pnl < 0:
            pl_token = "LOSS_L"
        else:
            pl_token = "BREAK_EVEN"

        if duration_hours < 1:
            dur_token = "SPEED_FLASH"
        elif duration_hours < 4:
            dur_token = "SPEED_SCALP"
        elif duration_hours < 12:
            dur_token = "SPEED_DAY"
        else:
            dur_token = "SPEED_SWING"

        synthetic_block = f"{{\n  INSTRUMENT: {instrument};\n  STATE: [{pl_token}, {dur_token}];\n}}\n"
        self.trade_buffer.append(synthetic_block)

        # Trigger sequence analysis if we have enough motifs built up
        if len(self.trade_buffer) >= 15:
            self._evaluate_structural_tension()

    def _evaluate_structural_tension(self) -> None:
        if encode_text is None:
            return  # Skip evaluation if we're not running with the C++ sidecar compiled

        full_synthetic_document = "".join(self.trade_buffer)

        encoded = encode_text(
            full_synthetic_document,
            window_bytes=1024,
            stride_bytes=256,
            precision=3,
            use_native=True,
        )

        if not encoded or not encoded.windows:
            return

        latest_window = encoded.windows[-1]
        self.current_hazard = latest_window.hazard

        if self.current_hazard >= MAX_CHAOS_THRESHOLD:
            if not self.is_blocked:
                logger.warning(
                    "🚨 STRUCTURAL TENSION P80 CRITICAL (%.4f) - CIRCUIT BREAKER ENGAGED. Transitioning to Speed-Scalp shutdown mode.",
                    self.current_hazard,
                )
                self.is_blocked = True
            self.is_scalping_regime = False
        elif self.current_hazard >= SCALP_CHAOS_THRESHOLD:
            if not self.is_scalping_regime:
                logger.info(
                    "⚡ STRUCTURAL TENSION ELEVATED (%.4f) - ALPHA GENERATOR ENGAGED. Transitioning to aggressive scalping.",
                    self.current_hazard,
                )
                self.is_scalping_regime = True
            self.is_blocked = False
        else:
            if self.is_blocked or self.is_scalping_regime:
                logger.info(
                    "🌊 STRUCTURAL FLOW STABILIZING (%.4f) - CIRCUIT BREAKER LIFTED. Returning to base regime.",
                    self.current_hazard,
                )
                self.is_blocked = False
                self.is_scalping_regime = False

    def clear(self) -> None:
        """Reset the circuit breaker, typically used between test runs."""
        self.trade_buffer.clear()
        self.is_blocked = False
        self.is_scalping_regime = False
        self.current_hazard = 0.0
