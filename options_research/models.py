"""Canonical domain models for the isolated options research framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Iterable


def _ensure_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_non_negative(value: float | int | None, field_name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{field_name} must be non-negative")


class SignalDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    FLAT = "flat"


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"


class LegAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class StrategyFamily(str, Enum):
    LONG_CALL_DEBIT_SPREAD = "long_call_debit_spread"
    LONG_PUT_DEBIT_SPREAD = "long_put_debit_spread"


class ExitReason(str, Enum):
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    FORCED_EXIT = "forced_exit"
    SIGNAL_REVERSAL = "signal_reversal"
    EXPIRY = "expiry"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class UnderlyingBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    adjusted_close: float | None = None

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.timestamp, "timestamp")
        for field_name in ("open", "high", "low", "close"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar high/low is inconsistent with open/close")
        if self.low <= 0:
            raise ValueError("low must be positive")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        _validate_non_negative(self.adjusted_close, "adjusted_close")


@dataclass(frozen=True, slots=True)
class CorporateAction:
    symbol: str
    ex_date: date
    action_type: str
    value: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.action_type:
            raise ValueError("action_type is required")
        _validate_non_negative(self.value, "value")


@dataclass(frozen=True, slots=True)
class SignalEvent:
    underlying: str
    occurred_at: datetime
    direction: SignalDirection
    signal_name: str
    strength: float = 1.0
    regime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.occurred_at, "occurred_at")
        if not self.underlying:
            raise ValueError("underlying is required")
        if not self.signal_name:
            raise ValueError("signal_name is required")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength must be within [0.0, 1.0]")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    as_of: datetime
    contract_symbol: str
    underlying: str
    expiry: date
    strike: float
    option_type: OptionRight
    bid: float
    ask: float
    last: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    underlying_spot: float | None = None
    multiplier: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.as_of, "as_of")
        if not self.contract_symbol:
            raise ValueError("contract_symbol is required")
        if not self.underlying:
            raise ValueError("underlying is required")
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        if self.bid < 0:
            raise ValueError("bid must be non-negative")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        for field_name in (
            "last",
            "implied_volatility",
            "gamma",
            "vega",
            "underlying_spot",
        ):
            _validate_non_negative(getattr(self, field_name), field_name)
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.open_interest is not None and self.open_interest < 0:
            raise ValueError("open_interest must be non-negative")
        if self.expiry < self.as_of.date():
            raise ValueError("expiry cannot be before the quote date")

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_width(self) -> float:
        return self.ask - self.bid

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry - self.as_of.date()).days


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    underlying: str
    as_of: datetime
    underlying_spot: float
    quotes: tuple[OptionQuote, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.as_of, "as_of")
        if not self.underlying:
            raise ValueError("underlying is required")
        if self.underlying_spot <= 0:
            raise ValueError("underlying_spot must be positive")
        normalized_quotes = tuple(self.quotes)
        object.__setattr__(self, "quotes", normalized_quotes)
        for quote in normalized_quotes:
            if quote.underlying != self.underlying:
                raise ValueError("all quotes in a snapshot must share the same underlying")

    def filter_quotes(
        self,
        *,
        option_type: OptionRight | None = None,
        min_dte: int | None = None,
        max_dte: int | None = None,
    ) -> tuple[OptionQuote, ...]:
        selected: list[OptionQuote] = []
        for quote in self.quotes:
            if option_type is not None and quote.option_type != option_type:
                continue
            if min_dte is not None and quote.days_to_expiry < min_dte:
                continue
            if max_dte is not None and quote.days_to_expiry > max_dte:
                continue
            selected.append(quote)
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class OptionLeg:
    action: LegAction
    quantity: int
    quote: OptionQuote

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    @property
    def mark_price(self) -> float:
        return self.quote.mid_price

    @property
    def signed_mark_price(self) -> float:
        sign = 1.0 if self.action is LegAction.BUY else -1.0
        return sign * self.mark_price * self.quantity


@dataclass(frozen=True, slots=True)
class PackageFill:
    filled_at: datetime
    net_price: float
    leg_prices: tuple[float, ...]
    commission: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.filled_at, "filled_at")
        if any(price < 0 for price in self.leg_prices):
            raise ValueError("leg_prices must be non-negative")
        _validate_non_negative(self.commission, "commission")
        _validate_non_negative(self.fees, "fees")
        _validate_non_negative(self.slippage, "slippage")

    @property
    def total_costs(self) -> float:
        return self.commission + self.fees


@dataclass(frozen=True, slots=True)
class OptionStrategyIntent:
    intent_id: str
    created_at: datetime
    underlying: str
    strategy_family: StrategyFamily
    signal_event: SignalEvent
    entry_snapshot_time: datetime
    legs: tuple[OptionLeg, ...]
    contracts: int = 1
    max_loss: float = 0.0
    profit_target: float | None = None
    stop_loss: float | None = None
    force_exit_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.created_at, "created_at")
        _ensure_tz_aware(self.entry_snapshot_time, "entry_snapshot_time")
        if self.force_exit_at is not None:
            _ensure_tz_aware(self.force_exit_at, "force_exit_at")
        if not self.intent_id:
            raise ValueError("intent_id is required")
        if not self.underlying:
            raise ValueError("underlying is required")
        if self.signal_event.underlying != self.underlying:
            raise ValueError("signal_event underlying must match intent underlying")
        if len(self.legs) < 2:
            raise ValueError("defined-risk spread intent requires at least two legs")
        if self.contracts <= 0:
            raise ValueError("contracts must be positive")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be positive")
        if self.profit_target is not None and self.profit_target <= 0:
            raise ValueError("profit_target must be positive when provided")
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive when provided")
        buy_legs = [leg for leg in self.legs if leg.action is LegAction.BUY]
        sell_legs = [leg for leg in self.legs if leg.action is LegAction.SELL]
        if not buy_legs or not sell_legs:
            raise ValueError("debit spread intent requires both buy and sell legs")
        expiries = {leg.quote.expiry for leg in self.legs}
        if len(expiries) != 1:
            raise ValueError("all legs in a v1 debit spread must share the same expiry")
        option_types = {leg.quote.option_type for leg in self.legs}
        if len(option_types) != 1:
            raise ValueError("all legs in a v1 debit spread must share the same option type")
        if any(leg.quote.underlying != self.underlying for leg in self.legs):
            raise ValueError("all legs must reference the same underlying")

    @property
    def estimated_entry_debit(self) -> float:
        debit = sum(leg.signed_mark_price for leg in self.legs)
        if debit <= 0:
            raise ValueError("estimated_entry_debit must be positive for debit strategies")
        return debit

    @property
    def contract_multiplier(self) -> int:
        return self.legs[0].quote.multiplier

    @property
    def expiry(self) -> date:
        return self.legs[0].quote.expiry


@dataclass(slots=True)
class FilledOptionPosition:
    position_id: str
    intent: OptionStrategyIntent
    opened_at: datetime
    entry_fill: PackageFill
    status: PositionStatus = PositionStatus.OPEN
    closed_at: datetime | None = None
    exit_fill: PackageFill | None = None
    exit_reason: ExitReason | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.opened_at, "opened_at")
        if not self.position_id:
            raise ValueError("position_id is required")
        if self.status is PositionStatus.CLOSED:
            if self.closed_at is None or self.exit_fill is None or self.exit_reason is None:
                raise ValueError("closed positions require close timestamp, exit fill, and exit reason")
        if self.closed_at is not None:
            _ensure_tz_aware(self.closed_at, "closed_at")
            if self.closed_at < self.opened_at:
                raise ValueError("closed_at cannot be before opened_at")
        if self.entry_fill.filled_at > self.opened_at:
            raise ValueError("entry_fill.filled_at cannot be after opened_at")
        if self.exit_fill is not None and self.closed_at is not None and self.exit_fill.filled_at > self.closed_at:
            raise ValueError("exit_fill.filled_at cannot be after closed_at")

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN

    @property
    def contract_multiplier(self) -> int:
        return self.intent.contract_multiplier

    @property
    def entry_cash_spent(self) -> float:
        gross = self.entry_fill.net_price * self.intent.contract_multiplier * self.intent.contracts
        return gross + self.entry_fill.total_costs

    @property
    def exit_cash_received(self) -> float | None:
        if self.exit_fill is None:
            return None
        gross = self.exit_fill.net_price * self.intent.contract_multiplier * self.intent.contracts
        return gross - self.exit_fill.total_costs

    @property
    def realized_pnl(self) -> float | None:
        exit_cash = self.exit_cash_received
        if exit_cash is None:
            return None
        return exit_cash - self.entry_cash_spent

    @property
    def holding_period(self) -> timedelta | None:
        if self.closed_at is None:
            return None
        return self.closed_at - self.opened_at

    def unrealized_pnl(self, mark_price: float) -> float:
        if mark_price < 0:
            raise ValueError("mark_price must be non-negative")
        current_value = mark_price * self.intent.contract_multiplier * self.intent.contracts
        return current_value - self.entry_cash_spent


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    drawdown: float

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.timestamp, "timestamp")
        _validate_non_negative(self.equity, "equity")
        _validate_non_negative(self.cash, "cash")
        _validate_non_negative(self.drawdown, "drawdown")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy_name: str
    started_at: datetime
    finished_at: datetime
    positions: tuple[FilledOptionPosition, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: dict[str, float]
    config: dict[str, Any]
    rejected_entries: int = 0
    strategy_summaries: dict[str, dict[str, float]] = field(default_factory=dict)
    underlying_summaries: dict[str, dict[str, float]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.started_at, "started_at")
        _ensure_tz_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be before started_at")
        if not self.strategy_name:
            raise ValueError("strategy_name is required")
        if self.rejected_entries < 0:
            raise ValueError("rejected_entries must be non-negative")
        if any(point.equity < 0 for point in self.equity_curve):
            raise ValueError("equity_curve cannot contain negative equity values")

    @property
    def total_trades(self) -> int:
        return len(self.positions)

    @property
    def fill_rejection_rate(self) -> float:
        attempted = self.total_trades + self.rejected_entries
        if attempted == 0:
            return 0.0
        return self.rejected_entries / attempted

    @property
    def total_return(self) -> float | None:
        if len(self.equity_curve) < 2:
            return None
        first = self.equity_curve[0].equity
        last = self.equity_curve[-1].equity
        if first == 0:
            return None
        return (last - first) / first


def ensure_iterable_quotes(quotes: Iterable[OptionQuote]) -> tuple[OptionQuote, ...]:
    """Normalize an arbitrary iterable of quotes into the package tuple convention."""

    return tuple(quotes)
