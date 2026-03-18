#!/usr/bin/env python3
"""Risk calculation and sizing (Pure Mathematics)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class RiskSizerCaps:
    """Margin caps returned by :class:`RiskSizer`."""

    nav_risk_cap: float
    per_position_cap: float
    portfolio_cap: float


class RiskSizer:
    """Convert target exposure into units under simple leverage assumptions."""

    def __init__(
        self,
        *,
        nav_risk_pct: float,
        per_position_pct_cap: float,
        alloc_top_k: int,
    ) -> None:
        self._nav_risk_pct = max(0.0, min(1.0, float(nav_risk_pct)))
        self._per_position_pct_cap = max(0.0, min(1.0, float(per_position_pct_cap)))
        self._alloc_top_k = max(1, int(alloc_top_k))

    def compute_caps(self, nav_snapshot: float) -> RiskSizerCaps:
        nav = max(0.0, float(nav_snapshot or 0.0))
        nav_risk_cap = nav * self._nav_risk_pct
        per_position_cap = nav_risk_cap
        if self._per_position_pct_cap > 0:
            per_position_cap = min(per_position_cap, nav * self._per_position_pct_cap)
        portfolio_cap = nav_risk_cap * self._alloc_top_k
        return RiskSizerCaps(
            nav_risk_cap=nav_risk_cap,
            per_position_cap=per_position_cap,
            portfolio_cap=portfolio_cap,
        )

    def compute_notional_caps(
        self, nav_snapshot: float, *, exposure_scale: float
    ) -> RiskSizerCaps:
        """Translate scalar exposure caps into gross-notional limits.

        The live risk manager tracks gross USD notional, while the sizing path
        budgets exposure using `exposure_scale`. Align the two by converting the
        scalar caps back into gross notional.
        """
        caps = self.compute_caps(nav_snapshot)
        scale = float(exposure_scale or 0.0)
        if scale <= 0:
            return caps
        return RiskSizerCaps(
            nav_risk_cap=caps.nav_risk_cap / scale,
            per_position_cap=caps.per_position_cap / scale,
            portfolio_cap=caps.portfolio_cap / scale,
        )

    def target_units(
        self,
        instrument: str,
        *,
        target_exposure: float,
        exposure_scale: float,
        price_data: Dict[str, float],
        auxiliary_prices: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Tuple[int, float, float]:
        live_price = float(price_data.get("mid", 0.0)) if price_data else 0.0
        margin_per_unit = self._margin_per_unit(
            instrument,
            live_price,
            float(exposure_scale or 0.0),
            auxiliary_prices or {},
        )
        if margin_per_unit <= 0:
            return 0, 0.0, 0.0

        adjusted_exposure = max(0.0, float(target_exposure or 0.0))
        raw_units = adjusted_exposure / margin_per_unit
        units = (
            int(raw_units) if raw_units >= 1 else (1 if adjusted_exposure > 0 else 0)
        )
        return units, margin_per_unit, adjusted_exposure

    def _margin_per_unit(
        self,
        instrument: str,
        live_price: float,
        exposure_scale: float,
        auxiliary_prices: Dict[str, Dict[str, float]],
    ) -> float:
        if exposure_scale <= 0:
            return 0.0

        base, quote = (instrument.upper().split("_", 1) + [""])[:2]
        live_price = max(0.0, live_price)

        if base == "USD":
            return exposure_scale

        if quote == "USD" or not auxiliary_prices:
            return live_price * exposure_scale

        bpair = f"{base}_USD"
        if bpair in auxiliary_prices and "mid" in auxiliary_prices[bpair]:
            return float(auxiliary_prices[bpair]["mid"]) * exposure_scale

        qpair = f"USD_{quote}"
        if qpair in auxiliary_prices and "mid" in auxiliary_prices[qpair]:
            return float(auxiliary_prices[qpair]["mid"]) * live_price * exposure_scale

        return live_price * exposure_scale

    def target_position_size_for_r(
        self,
        instrument: str,
        nav_snapshot: float,
        entry_price: float,
        stop_loss_price: float,
        auxiliary_prices: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Tuple[int, float]:
        """
        Calculate the exact number of units to trade so that the physical
        Trap Door risk distance exactly equals the nav_risk_pct.

        Args:
            instrument: e.g. "EUR_USD"
            nav_snapshot: Current account NAV in USD
            entry_price: The execution price
            stop_loss_price: The physical Trap Door invalidation price
            auxiliary_prices: Dictionary of live rates for Cross-pair conversions

        Returns:
            Tuple [units to trade, absolute scalar fractional risk limit USD]
        """
        if entry_price <= 0 or stop_loss_price <= 0 or entry_price == stop_loss_price:
            return 0, 0.0

        # Global cap for risk on this trade
        risk_usd_cap = nav_snapshot * self._nav_risk_pct

        # Physical distance of the trap door stop
        pip_dist = abs(entry_price - stop_loss_price)

        # Pip value math
        is_jpy = "JPY" in instrument.upper()
        pip_multi = 100.0 if is_jpy else 10000.0

        # Determine the USD value of 1 pip for 1 standard lot (100,000 units)
        base, quote = (instrument.upper().split("_", 1) + [""])[:2]

        # If USD is the quote currency (e.g. EUR_USD), pip value is fixed: $10 per lot
        # That means $0.0001 per single unit
        if quote == "USD":
            pip_value_usd_per_unit = 1.0 / pip_multi

        # If USD is the base currency (e.g. USD_JPY, USD_CAD)
        elif base == "USD":
            pip_value_usd_per_unit = (1.0 / pip_multi) / entry_price

        # Cross pairs (e.g. EUR_GBP, AUD_JPY) requires the auxiliary price of the quote currency
        else:
            quote_pair = f"{quote}_USD"
            usd_quote_pair = f"USD_{quote}"

            conversion_rate = 1.0
            if auxiliary_prices:
                if (
                    quote_pair in auxiliary_prices
                    and "mid" in auxiliary_prices[quote_pair]
                ):
                    conversion_rate = float(auxiliary_prices[quote_pair]["mid"])
                elif (
                    usd_quote_pair in auxiliary_prices
                    and "mid" in auxiliary_prices[usd_quote_pair]
                ):
                    # Inverse conversion
                    rate = float(auxiliary_prices[usd_quote_pair]["mid"])
                    if rate > 0:
                        conversion_rate = 1.0 / rate

            pip_value_usd_per_unit = (1.0 / pip_multi) * conversion_rate

        if pip_value_usd_per_unit <= 0:
            return 0, risk_usd_cap

        # Total USD risk if we traded exactly 1 unit
        risk_per_unit = pip_dist * pip_multi * pip_value_usd_per_unit

        # Scale units to meet the USD cap
        if risk_per_unit <= 0:
            return 0, risk_usd_cap

        target_units = int(risk_usd_cap / risk_per_unit)

        return target_units, risk_usd_cap
