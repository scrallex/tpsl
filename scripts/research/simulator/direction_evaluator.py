"""
Direction and side calculation logic extracted from simulator replay loop.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class DirectionConfig:
    """Configuration for direction/side calculation."""

    allow_fallback: Optional[bool]
    st_reversal_mode: bool
    invert_bundles: bool


@dataclass
class DirectionResult:
    """Result of direction evaluation."""

    direction: Optional[str]  # "BUY", "SELL", or None
    requested_side: int  # 1, -1, or 0


class DirectionEvaluator:
    """
    Handles trade direction and side calculation logic including:
    - Gate direction parsing
    - Fallback to momentum/reversal modes
    - Bundle inversion
    """

    @staticmethod
    def evaluate_direction(
        gate_payload: Dict[str, Any],
        config: DirectionConfig,
        last_mid: Optional[float],
        current_close: float,
        profile_allow_fallback: bool,
        profile_invert_bundles: bool,
        is_bundle_entry: bool,
    ) -> DirectionResult:
        """
        Evaluate trade direction from gate and configuration.

        Args:
            gate_payload: Gate data containing direction field
            config: Direction configuration (from params)
            last_mid: Last mid price (for momentum/reversal calculation)
            current_close: Current candle close price
            profile_allow_fallback: Profile-level allow_fallback setting
            profile_invert_bundles: Profile-level invert_bundles setting
            is_bundle_entry: Whether this is a bundle entry

        Returns:
            DirectionResult with direction string and requested_side int
        """
        direction = str(gate_payload.get("direction") or "").upper() or None

        # If direction is invalid, apply fallback logic
        if direction not in {"BUY", "SELL"}:
            allow_fb = config.allow_fallback
            if allow_fb is None:
                allow_fb = profile_allow_fallback

            if not allow_fb:
                direction = None
            elif config.st_reversal_mode:
                # Reversal Mode: fade the move
                direction = (
                    "SELL"
                    if (last_mid is None or current_close >= last_mid)
                    else "BUY"
                )
            else:
                # Momentum Mode (default): follow the move
                direction = (
                    "BUY"
                    if (last_mid is None or current_close >= last_mid)
                    else "SELL"
                )

        # Convert to side
        requested_side = 1 if direction == "BUY" else (-1 if direction == "SELL" else 0)

        # Invert Bundles if requested (via params OR profile)
        should_invert = config.invert_bundles or profile_invert_bundles
        if should_invert and is_bundle_entry and requested_side != 0:
            requested_side *= -1
            direction = "SELL" if requested_side == -1 else "BUY"

        return DirectionResult(direction, requested_side)
