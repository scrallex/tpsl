#!/usr/bin/env python3
"""Dynamic risk limits aligned with the echo portfolio sizing model."""

from __future__ import annotations

import os
from typing import Optional

from scripts.trading.risk import RiskLimits


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def get_live_account_risk_limits(nav: Optional[float]) -> RiskLimits:
    """Return risk limits derived from the current NAV and leverage posture.

    - Each admission risks ``NAV × PORTFOLIO_NAV_RISK_PCT`` as margin (default 1%).
    - Leverage is assumed to be 50:1, baked into the sizing maths elsewhere (`exposure_scale=0.02`).
    - ``max_total_exposure`` is capped at ``per_position_cap × ALLOC_TOP_K`` so we never
      stage more than the deterministic stack of simultaneous legs.
    """

    nav_amount = max(0.0, float(nav or 0.0))
    nav_risk_pct = max(0.0, min(1.0, _float_env("PORTFOLIO_NAV_RISK_PCT", 0.01)))

    # Margin (in account currency) allocated to a single admission.
    per_position_cap = nav_amount * nav_risk_pct

    # Allow explicit override when ops needs a temporary hard ceiling.
    per_position_override = _float_env("RISK_MAX_POSITION_SIZE", 0.0)
    if per_position_override > 0.0:
        per_position_cap = per_position_override

    top_k = max(1, _int_env("ALLOC_TOP_K", 3))
    max_total_exposure = per_position_cap * top_k
    total_override = _float_env("RISK_MAX_TOTAL_EXPOSURE", 0.0)
    if total_override > 0.0:
        max_total_exposure = total_override

    daily_loss_pct = max(0.0, min(1.0, _float_env("RISK_DAILY_LOSS_PCT", 0.02)))
    max_daily_loss = nav_amount * daily_loss_pct
    explicit_daily_loss = _float_env("RISK_MAX_DAILY_LOSS", 0.0)
    if explicit_daily_loss > 0.0:
        max_daily_loss = explicit_daily_loss

    max_positions_per_pair = _int_env("RISK_MAX_POSITIONS_PER_PAIR", 2)
    max_total_positions = _int_env("RISK_MAX_TOTAL_POSITIONS", top_k)
    max_net_units = _int_env("RISK_MAX_NET_UNITS_PER_PAIR", 10000)

    return RiskLimits(
        max_position_size=per_position_cap,
        max_daily_loss=max_daily_loss,
        max_total_exposure=max_total_exposure,
        max_positions_per_pair=max_positions_per_pair,
        max_total_positions=max_total_positions,
        max_net_units_per_pair=max_net_units,
        stop_loss_percentage=_float_env("RISK_STOP_LOSS_PCT", 0.005),
        take_profit_percentage=_float_env("RISK_TAKE_PROFIT_PCT", 0.015),
        risk_per_trade=nav_risk_pct,
        min_risk_reward_ratio=_float_env("RISK_MIN_RR", 2.0),
    )
