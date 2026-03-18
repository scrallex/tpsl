#!/usr/bin/env python3
"""Portfolio risk limits and exposure management."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class RiskLimits:
    """Configuration for core portfolio guardrails."""

    max_position_size: float = 0.0
    max_total_exposure: float = 0.0
    max_positions_per_pair: int = 5
    max_total_positions: int = 4
    max_net_units_per_pair: int = 1_000_000
    max_total_units: Optional[int] = None

    @classmethod
    def from_env(cls) -> "RiskLimits":
        """Load static guardrail defaults from environment variables."""

        def _int_env(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)) or default)
            except (TypeError, ValueError):
                return default

        def _float_env(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)) or default)
            except (TypeError, ValueError):
                return default

        max_total_units_raw = os.getenv("RISK_MAX_TOTAL_UNITS")
        max_total_units: Optional[int]
        if max_total_units_raw is None or not str(max_total_units_raw).strip():
            max_total_units = None
        else:
            try:
                max_total_units = int(max_total_units_raw)
            except (TypeError, ValueError):
                max_total_units = None

        return cls(
            max_position_size=max(0.0, _float_env("RISK_MAX_POSITION_SIZE", 0.0)),
            max_total_exposure=max(0.0, _float_env("RISK_MAX_TOTAL_EXPOSURE", 0.0)),
            max_positions_per_pair=max(
                1, _int_env("RISK_MAX_POSITIONS_PER_PAIR", 5)
            ),
            max_total_positions=max(1, _int_env("RISK_MAX_TOTAL_POSITIONS", 4)),
            max_net_units_per_pair=max(
                1, _int_env("RISK_MAX_NET_UNITS_PER_PAIR", 1_000_000)
            ),
            max_total_units=max_total_units,
        )


class RiskManager:
    """Minimal inventory of open exposure used by the portfolio manager."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._positions: Dict[str, int] = {}
        self._exposure: Dict[str, float] = {}
        self._nav_snapshot = 0.0
        self._last_updated = datetime.now(timezone.utc)

    def set_nav(self, nav: float) -> None:
        """Update the account Net Asset Value snapshot.

        Args:
            nav: The new NAV in USD.
        """
        self._nav_snapshot = max(0.0, float(nav or 0.0))
        self._last_updated = datetime.now(timezone.utc)

    def record_fill(
        self, instrument: str, units: int, price: Optional[float] = None
    ) -> None:
        """Register an executed trade to update the internal inventory.

        Args:
            instrument: The instrument symbol.
            units: The number of units bought (positive) or sold (negative).
            price: Optional execution price used to compute USD exposure.
        """
        inst = instrument.upper()
        current = self._positions.get(inst, 0)
        new_units = current + int(units)
        if new_units == 0:
            self._positions.pop(inst, None)
            self._exposure.pop(inst, None)
        else:
            self._positions[inst] = new_units
            notional = self._usd_notional(inst, new_units, price)
            if notional is not None:
                self._exposure[inst] = notional
        self._last_updated = datetime.now(timezone.utc)

    def flatten(self, instrument: str) -> None:
        """Clear all tracked positions and exposure for a given instrument.

        Args:
            instrument: The instrument symbol to flatten.
        """
        inst = instrument.upper()
        self._positions.pop(inst, None)
        self._exposure.pop(inst, None)
        self._last_updated = datetime.now(timezone.utc)

    def configure_dynamic_limits(
        self,
        *,
        max_position_size: Optional[float] = None,
        max_total_exposure: Optional[float] = None,
        max_total_positions: Optional[int] = None,
    ) -> None:
        """Refresh live limits derived from the latest NAV snapshot."""
        if max_position_size is not None:
            self.limits.max_position_size = max(0.0, float(max_position_size))
        if max_total_exposure is not None:
            self.limits.max_total_exposure = max(0.0, float(max_total_exposure))
        if max_total_positions is not None:
            self.limits.max_total_positions = max(1, int(max_total_positions))
        self._last_updated = datetime.now(timezone.utc)

    def net_units(self, instrument: str) -> int:
        """Get the current net unit inventory for a specific instrument.

        Args:
            instrument: The instrument symbol.

        Returns:
            The number of net units held (positive for long, negative for short).
        """
        return self._positions.get(instrument.upper(), 0)

    def positions(self) -> Dict[str, int]:
        """Get a copy of all active positions.

        Returns:
            A dictionary mapping instrument symbols to their net unit counts.
        """
        return dict(self._positions)

    def total_units(self) -> int:
        """Calculate the absolute total number of units held across all instruments."""
        return sum(abs(units) for units in self._positions.values())

    def exposure(self) -> float:
        """Calculate the total gross exposure in USD across all active positions."""
        return sum(self._exposure.values())

    def instrument_exposure(self, instrument: str) -> float:
        """Return the tracked USD exposure for a single instrument."""
        return float(self._exposure.get(instrument.upper(), 0.0))

    def position_breakdown(self) -> List[Dict[str, object]]:
        """Generate a detailed breakdown of all active positions and their exposure.

        Returns:
            A list of dictionaries containing instrument, net_units, and exposure.
        """
        return [
            {
                "instrument": instrument,
                "net_units": int(units),
                "exposure": float(self._exposure.get(instrument, 0.0)),
            }
            for instrument, units in self._positions.items()
        ]

    def get_risk_summary(self) -> Dict[str, float]:
        """Generate an aggregated summary of the current portfolio risk state.

        Returns:
            A dictionary containing the NAV snapshot, total units, USD exposure,
            and the timestamp of the last update.
        """
        return {
            "nav_snapshot": self._nav_snapshot,
            "total_units": float(self.total_units()),
            "exposure_usd": float(self.exposure()),
            "last_updated": self._last_updated.timestamp(),
        }

    def can_add(
        self, instrument: str, planned_units: int, price: Optional[float] = None
    ) -> bool:
        """Determine if a proposed trade complies with the portfolio risk limits.

        Checks against max position size, max total units, and position limits.

        Args:
            instrument: The instrument symbol.
            planned_units: The proposed number of units to add (or subtract).

        Returns:
            True if the trade is permitted, False otherwise.
        """
        inst = instrument.upper()
        planned = int(planned_units)
        if planned == 0:
            return True

        current = self.net_units(inst)
        proposed = current + planned
        is_new = current == 0 and proposed != 0
        is_reducing = current != 0 and (
            (current > 0 and planned < 0)
            or (current < 0 and planned > 0)
            or abs(proposed) < abs(current)
        )

        if (
            self.limits.max_net_units_per_pair
            and abs(proposed) > self.limits.max_net_units_per_pair
        ):
            return False
        if (
            self.limits.max_total_units is not None
            and (
                self.total_units() + max(0, abs(proposed) - abs(current))
            )
            > self.limits.max_total_units
        ):
            return False
        if is_new and len(self._positions) >= self.limits.max_total_positions:
            return False

        current_exposure = self.instrument_exposure(inst)
        proposed_exposure = self._usd_notional(inst, proposed, price)
        if proposed_exposure is None:
            proposed_exposure = current_exposure

        if not is_reducing:
            if (
                self.limits.max_position_size > 0
                and proposed_exposure > self.limits.max_position_size
            ):
                return False

            proposed_total_exposure = (
                self.exposure() - current_exposure + float(proposed_exposure)
            )
            if (
                self.limits.max_total_exposure > 0
                and proposed_total_exposure > self.limits.max_total_exposure
            ):
                return False

        return True

    def _usd_notional(
        self, instrument: str, net_units: int, price: Optional[float]
    ) -> Optional[float]:
        units = abs(int(net_units))
        if units == 0:
            return 0.0
        parts = instrument.upper().split("_", 1)
        base = parts[0] if parts else ""
        if base == "USD":
            return float(units)
        if price is None:
            return None
        return float(units) * float(price)
