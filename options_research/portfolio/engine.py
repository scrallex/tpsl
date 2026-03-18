"""Portfolio engine and exit logic for options backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

from options_research.execution import PackagedSpreadFillSimulator, SpreadExecutionSimulator
from options_research.models import (
    ExitReason,
    FilledOptionPosition,
    LegAction,
    OptionChainSnapshot,
    OptionStrategyIntent,
    PositionStatus,
    SignalDirection,
    SignalEvent,
)
from options_research.portfolio.risk import PortfolioRiskModel, SimplePortfolioRiskModel


def _ensure_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(slots=True)
class PortfolioState:
    as_of: datetime
    cash: float
    equity: float
    open_positions: list[FilledOptionPosition] = field(default_factory=list)
    closed_positions: list[FilledOptionPosition] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_tz_aware(self.as_of, "as_of")
        if self.cash < 0:
            raise ValueError("cash must be non-negative")
        if self.equity < 0:
            raise ValueError("equity must be non-negative")


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    max_holding_period: timedelta | None = None
    exit_on_signal_reversal: bool = True
    enforce_force_exit: bool = True
    exit_at_expiry: bool = True

    def __post_init__(self) -> None:
        if self.max_holding_period is not None and self.max_holding_period <= timedelta(0):
            raise ValueError("max_holding_period must be positive when provided")


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    reason: ExitReason | None
    mark_price: float | None
    unrealized_pnl: float | None


class PortfolioEngine(Protocol):
    """Applies fills, marks positions, and keeps portfolio state consistent."""

    def open_position(
        self,
        *,
        state: PortfolioState,
        intent: OptionStrategyIntent,
        snapshot: OptionChainSnapshot,
    ) -> tuple[PortfolioState, FilledOptionPosition | None, str | None]:
        ...

    def close_position(
        self,
        *,
        state: PortfolioState,
        position_id: str,
        snapshot: OptionChainSnapshot,
        reason: ExitReason,
    ) -> tuple[PortfolioState, FilledOptionPosition | None, str | None]:
        ...

    def mark_to_market(
        self,
        *,
        state: PortfolioState,
        snapshots: Mapping[str, OptionChainSnapshot] | Sequence[OptionChainSnapshot],
        as_of: datetime,
    ) -> PortfolioState:
        ...

    def evaluate_exit(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        current_signal: SignalEvent | None = None,
    ) -> ExitEvaluation:
        ...


class SimplePortfolioEngine:
    """Maintains cash, exposure, marks, and exit decisions for defined-risk positions."""

    def __init__(
        self,
        *,
        fill_simulator: SpreadExecutionSimulator | None = None,
        risk_model: PortfolioRiskModel | None = None,
        exit_policy: ExitPolicy | None = None,
    ) -> None:
        self.fill_simulator = fill_simulator or PackagedSpreadFillSimulator()
        self.risk_model = risk_model or SimplePortfolioRiskModel()
        self.exit_policy = exit_policy or ExitPolicy()

    def open_position(
        self,
        *,
        state: PortfolioState,
        intent: OptionStrategyIntent,
        snapshot: OptionChainSnapshot,
    ) -> tuple[PortfolioState, FilledOptionPosition | None, str | None]:
        allowed, rejection_reason = self.risk_model.allow_entry(
            intent=intent,
            open_positions=state.open_positions,
            equity=state.equity,
        )
        if not allowed:
            return state, None, rejection_reason

        entry_fill = self.fill_simulator.fill_open(intent=intent, snapshot=snapshot)
        if entry_fill is None:
            return state, None, "open_fill_rejected"

        position = FilledOptionPosition(
            position_id=intent.intent_id,
            intent=intent,
            opened_at=snapshot.as_of,
            entry_fill=entry_fill,
        )
        entry_cash_spent = position.entry_cash_spent
        if entry_cash_spent > state.cash:
            return state, None, "insufficient_cash"

        next_state = PortfolioState(
            as_of=snapshot.as_of,
            cash=state.cash - entry_cash_spent,
            equity=state.equity,
            open_positions=[*state.open_positions, position],
            closed_positions=list(state.closed_positions),
            metadata=dict(state.metadata),
        )
        marked_state = self.mark_to_market(
            state=next_state,
            snapshots={snapshot.underlying: snapshot},
            as_of=snapshot.as_of,
        )
        opened_position = next(position for position in marked_state.open_positions if position.position_id == intent.intent_id)
        return marked_state, opened_position, None

    def close_position(
        self,
        *,
        state: PortfolioState,
        position_id: str,
        snapshot: OptionChainSnapshot,
        reason: ExitReason,
    ) -> tuple[PortfolioState, FilledOptionPosition | None, str | None]:
        target = next((position for position in state.open_positions if position.position_id == position_id), None)
        if target is None:
            return state, None, "position_not_found"

        exit_fill = self.fill_simulator.fill_close(position=target, snapshot=snapshot, reason=reason)
        if exit_fill is None:
            return state, None, "close_fill_rejected"

        closed_position = replace(
            target,
            status=PositionStatus.CLOSED,
            closed_at=snapshot.as_of,
            exit_fill=exit_fill,
            exit_reason=reason,
        )
        exit_cash_received = closed_position.exit_cash_received
        if exit_cash_received is None:
            return state, None, "close_fill_rejected"

        remaining_positions = [position for position in state.open_positions if position.position_id != position_id]
        next_state = PortfolioState(
            as_of=snapshot.as_of,
            cash=state.cash + exit_cash_received,
            equity=state.equity,
            open_positions=remaining_positions,
            closed_positions=[*state.closed_positions, closed_position],
            metadata=dict(state.metadata),
        )
        marked_state = self.mark_to_market(
            state=next_state,
            snapshots={snapshot.underlying: snapshot},
            as_of=snapshot.as_of,
        )
        return marked_state, closed_position, None

    def mark_to_market(
        self,
        *,
        state: PortfolioState,
        snapshots: Mapping[str, OptionChainSnapshot] | Sequence[OptionChainSnapshot],
        as_of: datetime,
    ) -> PortfolioState:
        _ensure_tz_aware(as_of, "as_of")
        snapshot_map = self._normalize_snapshots(snapshots)

        marked_positions: list[FilledOptionPosition] = []
        market_value = 0.0
        total_unrealized_pnl = 0.0
        total_max_loss = 0.0
        underlying_exposure: dict[str, float] = {}
        aggregate_greeks: dict[str, float | None] = {
            "delta": 0.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
        }
        missing_greek = {key: False for key in aggregate_greeks}

        for position in state.open_positions:
            snapshot = snapshot_map.get(position.intent.underlying)
            mark_price = None
            if snapshot is not None:
                mark_price = self.fill_simulator.mark_position(position=position, snapshot=snapshot)
            if mark_price is None:
                mark_price = self._fallback_mark_price(position)
                mark_source = "fallback"
            else:
                mark_source = "snapshot"

            position_market_value = mark_price * position.contract_multiplier * position.intent.contracts
            position_unrealized = position.unrealized_pnl(mark_price)
            max_loss = self._position_max_loss(position.intent)
            market_value += position_market_value
            total_unrealized_pnl += position_unrealized
            total_max_loss += max_loss
            underlying_exposure[position.intent.underlying] = (
                underlying_exposure.get(position.intent.underlying, 0.0) + max_loss
            )

            position_greeks = self._position_greeks(position, snapshot)
            for greek_name, greek_value in position_greeks.items():
                if greek_value is None:
                    missing_greek[greek_name] = True
                elif not missing_greek[greek_name]:
                    current_value = aggregate_greeks[greek_name]
                    aggregate_greeks[greek_name] = float(current_value or 0.0) + greek_value

            position_metadata = dict(position.metadata)
            position_metadata.update(
                {
                    "mark_price": mark_price,
                    "mark_value": position_market_value,
                    "unrealized_pnl": position_unrealized,
                    "mark_source": mark_source,
                    "max_loss_dollars": max_loss,
                }
            )
            marked_positions.append(replace(position, metadata=position_metadata))

        for greek_name, missing in missing_greek.items():
            if missing:
                aggregate_greeks[greek_name] = None

        realized_pnl = sum((position.realized_pnl or 0.0) for position in state.closed_positions)
        equity = state.cash + market_value
        state_metadata = dict(state.metadata)
        state_metadata.update(
            {
                "realized_pnl": realized_pnl,
                "unrealized_pnl": total_unrealized_pnl,
                "portfolio_max_loss": total_max_loss,
                "underlying_exposure": underlying_exposure,
                "aggregate_greeks": aggregate_greeks,
                "aggregate_greeks_available": all(value is not None for value in aggregate_greeks.values()),
                "open_position_count": len(marked_positions),
                "closed_position_count": len(state.closed_positions),
            }
        )
        return PortfolioState(
            as_of=as_of,
            cash=state.cash,
            equity=equity,
            open_positions=marked_positions,
            closed_positions=list(state.closed_positions),
            metadata=state_metadata,
        )

    def evaluate_exit(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        current_signal: SignalEvent | None = None,
    ) -> ExitEvaluation:
        mark_price = self.fill_simulator.mark_position(position=position, snapshot=snapshot)
        if mark_price is None:
            return ExitEvaluation(reason=None, mark_price=None, unrealized_pnl=None)

        unrealized_pnl = position.unrealized_pnl(mark_price)
        entry_price = position.entry_fill.net_price
        profit_points = mark_price - entry_price
        loss_points = entry_price - mark_price

        if self.exit_policy.exit_at_expiry and snapshot.as_of.date() >= position.intent.expiry:
            return ExitEvaluation(reason=ExitReason.EXPIRY, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        if (
            self.exit_policy.enforce_force_exit
            and position.intent.force_exit_at is not None
            and snapshot.as_of >= position.intent.force_exit_at
        ):
            return ExitEvaluation(reason=ExitReason.FORCED_EXIT, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        if position.intent.stop_loss is not None and loss_points >= position.intent.stop_loss:
            return ExitEvaluation(reason=ExitReason.STOP_LOSS, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        if position.intent.profit_target is not None and profit_points >= position.intent.profit_target:
            return ExitEvaluation(reason=ExitReason.PROFIT_TARGET, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        if (
            self.exit_policy.max_holding_period is not None
            and (snapshot.as_of - position.opened_at) >= self.exit_policy.max_holding_period
        ):
            return ExitEvaluation(reason=ExitReason.TIME_STOP, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        if self._is_signal_reversal(position, current_signal):
            return ExitEvaluation(reason=ExitReason.SIGNAL_REVERSAL, mark_price=mark_price, unrealized_pnl=unrealized_pnl)
        return ExitEvaluation(reason=None, mark_price=mark_price, unrealized_pnl=unrealized_pnl)

    @staticmethod
    def _normalize_snapshots(
        snapshots: Mapping[str, OptionChainSnapshot] | Sequence[OptionChainSnapshot],
    ) -> dict[str, OptionChainSnapshot]:
        if isinstance(snapshots, Mapping):
            return dict(snapshots)
        return {snapshot.underlying: snapshot for snapshot in snapshots}

    @staticmethod
    def _position_max_loss(intent: OptionStrategyIntent) -> float:
        return intent.max_loss * intent.contract_multiplier * intent.contracts

    @staticmethod
    def _fallback_mark_price(position: FilledOptionPosition) -> float:
        metadata_mark = position.metadata.get("mark_price")
        if isinstance(metadata_mark, (int, float)):
            return max(0.0, float(metadata_mark))
        return max(0.0, position.entry_fill.net_price)

    def _position_greeks(
        self,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot | None,
    ) -> dict[str, float | None]:
        result: dict[str, float | None] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        if snapshot is None:
            return {key: None for key in result}
        quote_lookup = {quote.contract_symbol: quote for quote in snapshot.quotes}
        for leg in position.intent.legs:
            current_quote = quote_lookup.get(leg.quote.contract_symbol)
            if current_quote is None:
                return {key: None for key in result}
            sign = 1.0 if leg.action is LegAction.BUY else -1.0
            for greek_name in result:
                greek_value = getattr(current_quote, greek_name)
                if greek_value is None:
                    return {key: None for key in result}
                result[greek_name] = float(result[greek_name] or 0.0) + (
                    sign * greek_value * leg.quantity * position.intent.contracts * current_quote.multiplier
                )
        return result

    def _is_signal_reversal(
        self,
        position: FilledOptionPosition,
        current_signal: SignalEvent | None,
    ) -> bool:
        if not self.exit_policy.exit_on_signal_reversal or current_signal is None:
            return False
        if current_signal.underlying != position.intent.underlying:
            return False
        if position.intent.signal_event.direction is SignalDirection.BULLISH:
            return current_signal.direction is SignalDirection.BEARISH
        if position.intent.signal_event.direction is SignalDirection.BEARISH:
            return current_signal.direction is SignalDirection.BULLISH
        return False
