#!/usr/bin/env python3
"""Exposure tracking and OANDA broker reconciliation."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from . import oanda as oanda_service
from .position_tracker import ActiveTicket, TPSLPositionTracker
from .risk_limits import RiskManager

logger = logging.getLogger(__name__)


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BrokerOpenTrade:
    instrument: str
    units: int
    entry_price: float
    entry_time: datetime
    trade_id: Optional[str] = None


class ExposureTracker:
    def __init__(self, service: Any, risk_manager: RiskManager) -> None:
        self.svc = service
        self.risk_manager = risk_manager
        self._price_cache: Dict[str, Dict[str, Optional[float]]] = {}
        self._tracker = TPSLPositionTracker(
            cost_bps=float(os.getenv("EXECUTION_COST_BPS", "1.5") or 1.5)
        )

    @property
    def price_cache(self) -> Dict[str, Dict[str, Optional[float]]]:
        return self._price_cache

    def net_units(self, instrument: str) -> int:
        return self._tracker.net_units(instrument)

    def has_position(self, instrument: str) -> bool:
        return self._tracker.has_position(instrument)

    def get_tickets(self, instrument: str) -> list[Any]:
        return self._tracker.get_tickets(instrument)

    def open_position(
        self,
        instrument: str,
        units: int,
        price: float,
        timestamp: Any,
        is_bundle: bool = False,
    ) -> None:
        self._tracker.open_position(instrument, units, price, timestamp, is_bundle)

    def sync_to_net_position(
        self,
        instrument: str,
        target_units: int,
        price: float,
        timestamp: Any,
        *,
        is_bundle: bool = False,
    ) -> None:
        self._tracker.sync_to_net_position(
            instrument,
            target_units,
            price,
            timestamp,
            is_bundle=is_bundle,
        )

    def sync_risk_manager(
        self, instrument: str, price: Optional[float] = None
    ) -> None:
        """Force the coarse risk inventory to match the confirmed local ticket net."""
        tracked_units = self.net_units(instrument)
        current_units = self.risk_manager.net_units(instrument)
        if tracked_units == current_units:
            return
        self.risk_manager.flatten(instrument)
        reference_price = price
        if reference_price is None:
            tickets = self.get_tickets(instrument)
            if tickets:
                reference_price = float(tickets[0].entry_price)
        if tracked_units != 0:
            self.risk_manager.record_fill(instrument, tracked_units, reference_price)

    def _extract_fill(
        self,
        response: Any,
        *,
        default_units: Optional[int],
        default_price: Optional[float],
    ) -> tuple[Optional[int], Optional[float]]:
        if not isinstance(response, Mapping):
            return default_units, default_price
        for key in (
            "orderFillTransaction",
            "longOrderFillTransaction",
            "shortOrderFillTransaction",
            "fillTransaction",
        ):
            tx = response.get(key)
            if not isinstance(tx, Mapping):
                continue
            filled_units = _coerce_int(tx.get("units"))
            fill_price = _coerce_float(tx.get("price"))
            return (
                filled_units if filled_units is not None else default_units,
                fill_price if fill_price is not None else default_price,
            )
        return default_units, default_price

    def close_ticket(
        self,
        instrument: str,
        ticket_index: int,
        price: float,
        timestamp: Any,
        reason: str,
        fill_price: float,
    ) -> Optional[Any]:
        tickets = self.get_tickets(instrument)
        if ticket_index < 0 or ticket_index >= len(tickets):
            logger.warning(
                "Ignoring close_ticket for %s[%d]: tracker slot missing",
                instrument,
                ticket_index,
            )
            return None

        ticket = tickets[ticket_index]
        requested_units = -int(ticket.units)
        logger.info(
            "ExposureTracker executing close_ticket for %s[%d] (%s)",
            instrument,
            ticket_index,
            reason,
        )
        try:
            response = oanda_service.close_position(self.svc, instrument, ticket.units)
            if not response:
                logger.warning(
                    "Broker rejected close_ticket for %s[%d] (%s)",
                    instrument,
                    ticket_index,
                    reason,
                )
                return None
            filled_units, broker_fill_price = self._extract_fill(
                response,
                default_units=requested_units,
                default_price=fill_price or price,
            )
            record = self._tracker.close_ticket(
                instrument,
                ticket_index,
                broker_fill_price or fill_price or price,
                timestamp,
                reason,
                fill_price or price,
            )
            if record is None:
                return None
            self.risk_manager.record_fill(
                instrument,
                filled_units if filled_units is not None else requested_units,
                broker_fill_price or fill_price or price,
            )
            return record
        except Exception as exc:
            logger.exception("Order closure network error for %s: %s", instrument, exc)
            return None

    def close_position(
        self, instrument: str, price: float, timestamp: Any, reason: str
    ) -> bool:
        logger.info(
            "ExposureTracker executing full close_position for %s (%s)",
            instrument,
            reason,
        )
        try:
            response = oanda_service.close_position(self.svc, instrument, None)
            if not response:
                logger.warning(
                    "Broker rejected close_position for %s (%s)", instrument, reason
                )
                return False
            self._tracker.close_position(
                instrument,
                price,
                timestamp,
                exit_reason=reason,
                trigger_price=price,
            )
            self.risk_manager.flatten(instrument)
            return True
        except Exception as exc:
            logger.exception("Order closure network error for %s: %s", instrument, exc)
            return False

    def execute_delta(
        self,
        instrument: str,
        delta_units: int,
        mid_price: Optional[float],
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> bool:
        """Execute a trade for the given instrument.

        Submits a market order via the OANDA service and records the fill
        in the risk manager.

        Args:
            instrument: The instrument symbol (e.g. 'EUR_USD').
            delta_units: Number of units to buy (positive) or sell (negative).
            mid_price: The current mid price for the fill.
            stop_loss_price: Optional price level at which to place a stop loss.
            take_profit_price: Optional price level at which to place a take profit.
        """
        try:
            response = oanda_service.submit_market_order(
                self.svc, instrument, delta_units, stop_loss_price, take_profit_price
            )
            if response is None:
                logger.warning(
                    "Order for %s not confirmed; inventory unchanged (%s units)",
                    instrument,
                    delta_units,
                )
                return False
            filled_units, fill_price = self._extract_fill(
                response,
                default_units=delta_units,
                default_price=mid_price,
            )
            if filled_units is None or filled_units == 0:
                logger.warning(
                    "Order for %s returned no filled units; inventory unchanged",
                    instrument,
                )
                return False
            self.risk_manager.record_fill(
                instrument,
                filled_units,
                fill_price if fill_price is not None else mid_price,
            )
            return True
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.exception(
                "Order submission network error for %s: %s", instrument, exc
            )
            return False
        except (KeyError, ValueError, TypeError) as exc:
            logger.exception("Order submission data error for %s: %s", instrument, exc)
            return False

    def fetch_prices(
        self, instruments: Iterable[str]
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Fetch the latest pricing data for the specified instruments.

        Caches and returns bid, ask, and mid prices.

        Args:
            instruments: List of instrument symbols to fetch prices for.

        Returns:
            A dictionary mapping instruments to their cached prices.
        """
        try:
            payload = self.svc.get_pricing(list(instruments))
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch prices from OANDA: %s", exc)
            payload = {}
        prices = (payload or {}).get("prices", {}) if isinstance(payload, dict) else {}
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for inst in instruments:
            entry = prices.get(inst, {}) if isinstance(prices, dict) else {}
            out[inst] = {
                key: float(entry.get(key)) if entry.get(key) is not None else None
                for key in ("bid", "ask", "mid")
            }
        self._price_cache = out
        return out

    def nav_snapshot(self) -> float:
        """Fetch and update the current account Net Asset Value (NAV).

        Returns:
            The current account balance in USD.
        """
        try:
            account = self.svc.get_oanda_account_info() or {}
            balance = float(account.get("account", {}).get("balance", 0.0) or 0.0)
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch OANDA account info: %s", exc)
            balance = float(
                self.risk_manager.get_risk_summary().get("nav_snapshot", 0.0) or 0.0
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Failed to parse OANDA balance: %s", exc)
            balance = float(
                self.risk_manager.get_risk_summary().get("nav_snapshot", 0.0) or 0.0
            )
        self.risk_manager.set_nav(balance)
        return balance

    def _coerce_trade_time(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except (TypeError, ValueError):
            return None

    def _position_units_and_price(
        self, entry: Mapping[str, Any]
    ) -> tuple[int, Optional[float]]:
        units = None
        for key in ("netUnits", "units"):
            value = entry.get(key)
            if value is not None:
                units = _coerce_int(value)
                if units is not None:
                    break
        if units is None:
            long_units = _coerce_int((entry.get("long") or {}).get("units")) or 0
            short_units = _coerce_int((entry.get("short") or {}).get("units")) or 0
            units = long_units + short_units

        price_val = _coerce_float(entry.get("averagePrice") or entry.get("price"))
        if price_val is None:
            side = entry.get("long") if units >= 0 else entry.get("short")
            if isinstance(side, Mapping):
                price_val = _coerce_float(side.get("averagePrice"))
        return units, price_val

    def _broker_trade_map(
        self,
        raw_trades: Iterable[Mapping[str, Any]],
        *,
        instruments: Optional[set[str]] = None,
    ) -> Dict[str, list[BrokerOpenTrade]]:
        by_inst: Dict[str, list[BrokerOpenTrade]] = {}
        for entry in raw_trades:
            inst = str(entry.get("instrument") or "").upper()
            if not inst:
                continue
            if instruments and inst not in instruments:
                continue
            units = _coerce_int(entry.get("currentUnits") or entry.get("units"))
            price = _coerce_float(entry.get("price"))
            opened_at = self._coerce_trade_time(
                entry.get("openTime") or entry.get("time")
            )
            if units is None or units == 0 or price is None or opened_at is None:
                continue
            by_inst.setdefault(inst, []).append(
                BrokerOpenTrade(
                    instrument=inst,
                    units=units,
                    entry_price=price,
                    entry_time=opened_at,
                    trade_id=(
                        str(entry.get("id")) if entry.get("id") is not None else None
                    ),
                )
            )
        for trades in by_inst.values():
            trades.sort(key=lambda trade: trade.entry_time)
        return by_inst

    def reconcile_portfolio(
        self, instruments: Optional[Iterable[str]] = None
    ) -> Dict[str, list[BrokerOpenTrade]]:
        """Sync local risk manager and ticket state with remote OANDA state.

        Detects out-of-band trades or manual closures and updates internal
        position tracking respectively.
        """
        instrument_filter = {
            str(inst).upper() for inst in (instruments or []) if str(inst).strip()
        }
        try:
            positions = self.svc.get_oanda_positions()
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch OANDA positions: %s", exc)
            positions = None
        if positions is None:
            logger.warning("Reconciling skipped: no OANDA positions returned")
            return {}
        if not positions and str(
            os.getenv("RECONCILE_ALLOW_EMPTY", "0")
        ).lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            logger.warning("Reconciling skipped: empty OANDA positions response")
            return {}

        try:
            raw_open_trades = self.svc.get_oanda_open_trades(
                sorted(instrument_filter) if instrument_filter else None
            )
        except (AttributeError, ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch OANDA open trades: %s", exc)
            raw_open_trades = []

        logger.info("Reconciling OANDA portfolio (%d positions)", len(positions or []))
        broker_trades = self._broker_trade_map(
            raw_open_trades or [],
            instruments=instrument_filter or None,
        )
        reconcile_targets: set[str] = set(instrument_filter)
        reconcile_targets.update(self.risk_manager.positions().keys())
        reconcile_targets.update(
            inst.upper() for inst in getattr(self._tracker, "_tickets", {}).keys()
        )

        for entry in positions or []:
            inst = str(entry.get("instrument") or "").upper()
            if not inst:
                continue
            if instrument_filter and inst not in instrument_filter:
                continue
            reconcile_targets.add(inst)
            units, price_val = self._position_units_and_price(entry)
            if units == 0:
                continue
            if inst not in broker_trades:
                broker_trades[inst] = [
                    BrokerOpenTrade(
                        instrument=inst,
                        units=units,
                        entry_price=float(price_val or 0.0),
                        entry_time=datetime.now(timezone.utc),
                    )
                ]

        for inst in reconcile_targets:
            rebuilt_tickets = [
                ActiveTicket(
                    units=int(trade.units),
                    entry_price=float(trade.entry_price),
                    entry_time=trade.entry_time,
                    entry_cost=0.0,
                    is_bundle=False,
                )
                for trade in broker_trades.get(inst, [])
            ]
            for ticket in rebuilt_tickets:
                ticket.tpsl_state.reset(ticket.entry_price, 1 if ticket.units > 0 else -1)
            self._tracker.replace_tickets(inst, rebuilt_tickets)

        for inst in list(self.risk_manager.positions().keys()):
            if not instrument_filter or inst in instrument_filter:
                self.risk_manager.flatten(inst)

        for inst, trades in broker_trades.items():
            net_units = sum(int(trade.units) for trade in trades)
            if net_units == 0:
                continue
            abs_units = sum(abs(int(trade.units)) for trade in trades)
            reference_price = (
                sum(abs(int(trade.units)) * float(trade.entry_price) for trade in trades)
                / float(abs_units)
                if abs_units > 0
                else float(trades[0].entry_price)
            )
            self.risk_manager.record_fill(inst, net_units, reference_price)

        logger.info("Risk inventory after reconcile: %s", self.risk_manager.positions())
        return broker_trades
