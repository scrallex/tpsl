"""Execution-side position tracker without research package dependencies."""

from __future__ import annotations


from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from scripts.trading.pricing_utils import calculate_commission, convert_to_usd
from scripts.trading.tpsl import TPSLTradeState
from scripts.trading.trade_records import TPSLTradeRecord


@dataclass
class ActiveTicket:
    units: int
    entry_price: float
    entry_time: datetime
    entry_cost: float
    is_bundle: bool
    tpsl_state: TPSLTradeState = field(default_factory=TPSLTradeState)
    max_adverse: float = 0.0
    max_favorable: float = 0.0


class TPSLPositionTracker:
    """Tracks open exposure with per-trade TP/SL state."""

    def __init__(self, *, cost_bps: float) -> None:
        self.realized: float = 0.0
        self.trade_log: List[TPSLTradeRecord] = []
        self.cost_bps = max(0.0, float(cost_bps))
        self._tickets: Dict[str, List[ActiveTicket]] = {}

    def net_units(self, instrument: str) -> int:
        return sum(t.units for t in self._tickets.get(instrument.upper(), []))

    def has_position(self, instrument: str) -> bool:
        return self.net_units(instrument) != 0

    def get_tickets(self, instrument: str) -> List[ActiveTicket]:
        return self._tickets.get(instrument.upper(), [])

    def replace_tickets(self, instrument: str, tickets: List[ActiveTicket]) -> None:
        inst = instrument.upper()
        normalized = [ticket for ticket in tickets if int(ticket.units or 0) != 0]
        if normalized:
            self._tickets[inst] = normalized
        else:
            self._tickets.pop(inst, None)

    def open_position(
        self,
        instrument: str,
        units: int,
        price: float,
        timestamp: datetime,
        is_bundle: bool = False,
    ) -> None:
        inst = instrument.upper()
        if units == 0:
            return

        cost = self._commission(inst, price, abs(units))
        self.realized -= cost

        ticket = ActiveTicket(
            units=units,
            entry_price=float(price),
            entry_time=timestamp,
            entry_cost=cost,
            is_bundle=is_bundle,
        )
        direction = 1 if units > 0 else -1
        ticket.tpsl_state.reset(price, direction)

        self._tickets.setdefault(inst, []).append(ticket)

    def close_ticket(
        self,
        instrument: str,
        ticket_index: int,
        price: float,
        timestamp: datetime,
        exit_reason: str = "hold_expiry",
        trigger_price: float = 0.0,
    ) -> Optional[TPSLTradeRecord]:
        inst = instrument.upper()
        tickets = self._tickets.get(inst, [])
        if ticket_index < 0 or ticket_index >= len(tickets):
            return None

        ticket = tickets.pop(ticket_index)
        units = ticket.units
        if units == 0:
            return None

        avg = ticket.entry_price
        entry_cost = ticket.entry_cost
        close_units = abs(units)
        exit_cost = self._commission(inst, price, close_units)
        self.realized -= exit_cost

        if units > 0:
            gross = (float(price) - avg) * close_units
            direction_str = "LONG"
        else:
            gross = (avg - float(price)) * close_units
            direction_str = "SHORT"
        gross_usd = self._convert_to_usd(inst, gross, price)
        self.realized += gross_usd

        commission = entry_cost + exit_cost
        record = TPSLTradeRecord(
            instrument=inst,
            entry_time=ticket.entry_time,
            exit_time=timestamp,
            direction=direction_str,
            units=close_units,
            entry_price=avg,
            exit_price=float(price),
            pnl=gross_usd - commission,
            commission=commission,
            mae=abs(ticket.max_adverse),
            mfe=max(0.0, ticket.max_favorable),
            exit_reason=exit_reason,
            tpsl_trigger_price=trigger_price,
            is_bundle_trade=ticket.is_bundle,
        )
        self.trade_log.append(record)
        return record

    def close_position(
        self,
        instrument: str,
        price: float,
        timestamp: datetime,
        exit_reason: str = "hold_expiry",
        trigger_price: float = 0.0,
    ) -> None:
        inst = instrument.upper()
        tickets = self._tickets.get(inst, [])
        while tickets:
            self.close_ticket(inst, 0, price, timestamp, exit_reason, trigger_price)

    def sync_to_net_position(
        self,
        instrument: str,
        target_units: int,
        price: float,
        timestamp: datetime,
        *,
        is_bundle: bool = False,
    ) -> None:
        current_units = self.net_units(instrument)
        target_units = int(target_units)
        if current_units == target_units:
            return

        same_side = current_units == 0 or target_units == 0 or (
            (current_units > 0) == (target_units > 0)
        )
        if not same_side or abs(target_units) < abs(current_units):
            self.close_position(
                instrument,
                price,
                timestamp,
                exit_reason="position_sync",
                trigger_price=price,
            )
            current_units = 0

        delta_units = target_units - current_units
        if delta_units:
            self.open_position(
                instrument,
                delta_units,
                price,
                timestamp,
                is_bundle=is_bundle,
            )

    def mark(
        self,
        instrument: str,
        price: Optional[float],
        high: Optional[float] = None,
        low: Optional[float] = None,
    ) -> None:
        if price is None:
            return
        inst = instrument.upper()
        for ticket in self._tickets.get(inst, []):
            avg = ticket.entry_price
            if ticket.units > 0:
                gross = (float(price) - avg) * abs(ticket.units)
                optimal_price = high if high is not None else price
            else:
                gross = (avg - float(price)) * abs(ticket.units)
                optimal_price = low if low is not None else price
            usd = self._convert_to_usd(inst, gross, price)
            ticket.max_favorable = max(ticket.max_favorable, usd)
            ticket.max_adverse = min(ticket.max_adverse, usd)
            ticket.tpsl_state.update_peak(optimal_price)

    def unrealized(self, instrument: str, price: Optional[float]) -> float:
        if price is None:
            return 0.0
        inst = instrument.upper()
        total_unrealized = 0.0
        for ticket in self._tickets.get(inst, []):
            avg = ticket.entry_price
            if ticket.units > 0:
                gross = (float(price) - avg) * abs(ticket.units)
            else:
                gross = (avg - float(price)) * abs(ticket.units)
            total_unrealized += self._convert_to_usd(inst, gross, price)
        return total_unrealized

    def _commission(self, instrument: str, price: float, units: int) -> float:
        return calculate_commission(instrument, price, units, self.cost_bps)

    @staticmethod
    def _convert_to_usd(instrument: str, raw: float, price: float) -> float:
        return convert_to_usd(instrument, raw, price)
