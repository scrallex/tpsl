"""
Helper functions for processing candles in the replay loop.
Extracted from backtest_simulator to reduce method complexity.
"""

from typing import Any, Dict, Optional, Tuple

from scripts.trading.portfolio_manager import StrategyInstrument
from scripts.trading.risk_calculator import RiskSizer

from .direction_evaluator import DirectionConfig, DirectionEvaluator


def compute_trade_direction_and_side(
    gate_payload: Dict[str, Any],
    params: Any,
    profile: StrategyInstrument,
    last_mid: Optional[float],
    current_close: float,
    is_bundle_entry: bool,
) -> Tuple[Optional[str], int, float]:
    """
    Compute trade direction and requested side.

    Args:
        gate_payload: Gate data dict
        params: Simulation parameters
        profile: Strategy instrument profile
        last_mid: Last mid price
        current_close: Current candle close
        is_bundle_entry: Whether this is a bundle entry

    Returns:
        Tuple of (direction, requested_side, updated_last_mid)
    """
    dir_config = DirectionConfig(
        allow_fallback=params.allow_fallback,
        st_reversal_mode=params.st_reversal_mode,
        invert_bundles=params.invert_bundles,
    )

    dir_result = DirectionEvaluator.evaluate_direction(
        gate_payload,
        dir_config,
        last_mid,
        current_close,
        getattr(profile, "allow_fallback", True),
        getattr(profile, "invert_bundles", False),
        is_bundle_entry,
    )

    return dir_result.direction, dir_result.requested_side, current_close


def compute_position_size(
    gate_entry_ready: bool,
    requested_side: int,
    tracker: Any,
    instrument: str,
    risk_sizer: RiskSizer,
    nav: float,
    params: Any,
    candle: Any,
) -> int:
    """
    Compute target position size using risk sizer.

    Returns:
        Absolute target units (int)
    """
    if not gate_entry_ready or requested_side == 0:
        return 0

    caps = risk_sizer.compute_caps(nav)
    units, _, _ = risk_sizer.target_units(
        instrument,
        target_exposure=caps.per_position_cap,
        exposure_scale=params.exposure_scale,
        price_data={"mid": candle.close},
    )

    if gate_entry_ready:
        pass

    return abs(units)
