"""Maps a directional signal and selected contracts into a strategy intent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Protocol, Sequence

from options_research.models import (
    OptionLeg,
    OptionRight,
    OptionStrategyIntent,
    SignalDirection,
    SignalEvent,
    StrategyFamily,
)


@dataclass(frozen=True, slots=True)
class DirectionalExpressionConfig:
    contracts: int = 1
    take_profit_pct: float = 0.50
    stop_loss_pct: float = 0.50
    close_before_expiry_days: int = 1

    def __post_init__(self) -> None:
        if self.contracts <= 0:
            raise ValueError("contracts must be positive")
        if self.take_profit_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("take_profit_pct and stop_loss_pct must be positive")
        if self.close_before_expiry_days < 0:
            raise ValueError("close_before_expiry_days must be non-negative")


class DirectionalExpressionMapper(Protocol):
    """Builds a position intent after contract selection chooses the legs."""

    def build_intent(
        self,
        *,
        signal: SignalEvent,
        legs: Sequence[OptionLeg],
    ) -> OptionStrategyIntent:
        ...


class VerticalDebitSpreadExpressionMapper:
    """Builds a v1 defined-risk vertical debit spread intent from selected legs."""

    def __init__(self, config: DirectionalExpressionConfig | None = None) -> None:
        self.config = config or DirectionalExpressionConfig()

    def build_intent(
        self,
        *,
        signal: SignalEvent,
        legs: Sequence[OptionLeg],
    ) -> OptionStrategyIntent:
        normalized_legs = tuple(legs)
        if len(normalized_legs) != 2:
            raise ValueError("v1 vertical debit spreads require exactly two legs")

        option_types = {leg.quote.option_type for leg in normalized_legs}
        if len(option_types) != 1:
            raise ValueError("all legs must share the same option type")
        option_type = next(iter(option_types))
        strategy_family = self._resolve_strategy_family(signal.direction, option_type)

        entry_snapshot_time = normalized_legs[0].quote.as_of
        estimated_entry_debit = sum(leg.signed_mark_price for leg in normalized_legs)
        if estimated_entry_debit <= 0:
            raise ValueError("estimated entry debit must be positive")

        spread_width = max(leg.quote.strike for leg in normalized_legs) - min(
            leg.quote.strike for leg in normalized_legs
        )
        if spread_width <= 0:
            raise ValueError("spread width must be positive")

        force_exit_at = self._force_exit_timestamp(
            expiry=normalized_legs[0].quote.expiry,
            reference_time=entry_snapshot_time,
        )
        metadata = {
            "entry_debit": estimated_entry_debit,
            "spread_width": spread_width,
            "take_profit_pct": self.config.take_profit_pct,
            "stop_loss_pct": self.config.stop_loss_pct,
            "threshold_mode": "premium_points_relative_to_entry_debit",
            "close_before_expiry_days": self.config.close_before_expiry_days,
        }
        return OptionStrategyIntent(
            intent_id=self._build_intent_id(
                signal=signal,
                strategy_family=strategy_family,
                entry_snapshot_time=entry_snapshot_time,
                legs=normalized_legs,
            ),
            created_at=signal.occurred_at,
            underlying=signal.underlying,
            strategy_family=strategy_family,
            signal_event=signal,
            entry_snapshot_time=entry_snapshot_time,
            legs=normalized_legs,
            contracts=self.config.contracts,
            max_loss=estimated_entry_debit,
            profit_target=estimated_entry_debit * self.config.take_profit_pct,
            stop_loss=estimated_entry_debit * self.config.stop_loss_pct,
            force_exit_at=force_exit_at,
            metadata=metadata,
        )

    @staticmethod
    def _resolve_strategy_family(
        direction: SignalDirection,
        option_type: OptionRight,
    ) -> StrategyFamily:
        if direction is SignalDirection.BULLISH and option_type is OptionRight.CALL:
            return StrategyFamily.LONG_CALL_DEBIT_SPREAD
        if direction is SignalDirection.BEARISH and option_type is OptionRight.PUT:
            return StrategyFamily.LONG_PUT_DEBIT_SPREAD
        raise ValueError("signal direction and option type do not form a supported debit spread")

    def _force_exit_timestamp(
        self,
        *,
        expiry,
        reference_time: datetime,
    ) -> datetime | None:
        exit_date = expiry - timedelta(days=self.config.close_before_expiry_days)
        force_exit_at = datetime.combine(exit_date, reference_time.timetz())
        if force_exit_at <= reference_time:
            return None
        return force_exit_at

    @staticmethod
    def _build_intent_id(
        *,
        signal: SignalEvent,
        strategy_family: StrategyFamily,
        entry_snapshot_time: datetime,
        legs: Sequence[OptionLeg],
    ) -> str:
        timestamp = entry_snapshot_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        leg_symbols = "-".join(leg.quote.contract_symbol for leg in legs)
        return f"{signal.underlying}-{strategy_family.value}-{timestamp}-{leg_symbols}"
