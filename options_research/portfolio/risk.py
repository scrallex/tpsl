"""Portfolio-level risk boundaries for defined-risk option positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from options_research.models import FilledOptionPosition, OptionStrategyIntent


@dataclass(frozen=True, slots=True)
class PortfolioRiskLimits:
    max_open_positions: int = 5
    max_portfolio_max_loss: float = 0.20
    max_underlying_allocation: float = 0.20
    max_new_position_max_loss: float = 0.02

    def __post_init__(self) -> None:
        if self.max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive")
        for field_name in (
            "max_portfolio_max_loss",
            "max_underlying_allocation",
            "max_new_position_max_loss",
        ):
            value = getattr(self, field_name)
            if not 0 < value <= 1:
                raise ValueError(f"{field_name} must be within (0, 1]")


class PortfolioRiskModel(Protocol):
    """Approves or rejects a new intent given current book risk."""

    def allow_entry(
        self,
        *,
        intent: OptionStrategyIntent,
        open_positions: Sequence[FilledOptionPosition],
        equity: float,
    ) -> tuple[bool, str | None]:
        ...


class SimplePortfolioRiskModel:
    """Applies max-loss-based portfolio limits to defined-risk spreads."""

    def __init__(self, limits: PortfolioRiskLimits | None = None) -> None:
        self.limits = limits or PortfolioRiskLimits()

    def allow_entry(
        self,
        *,
        intent: OptionStrategyIntent,
        open_positions: Sequence[FilledOptionPosition],
        equity: float,
    ) -> tuple[bool, str | None]:
        if equity <= 0:
            return False, "non_positive_equity"
        if len(open_positions) >= self.limits.max_open_positions:
            return False, "max_open_positions"

        new_position_max_loss = self._position_max_loss(intent)
        if new_position_max_loss > (equity * self.limits.max_new_position_max_loss):
            return False, "max_new_position_max_loss"

        current_portfolio_max_loss = sum(self._position_max_loss(position.intent) for position in open_positions)
        if (current_portfolio_max_loss + new_position_max_loss) > (
            equity * self.limits.max_portfolio_max_loss
        ):
            return False, "max_portfolio_max_loss"

        current_underlying_exposure = sum(
            self._position_max_loss(position.intent)
            for position in open_positions
            if position.intent.underlying == intent.underlying
        )
        if (current_underlying_exposure + new_position_max_loss) > (
            equity * self.limits.max_underlying_allocation
        ):
            return False, "max_underlying_allocation"
        return True, None

    @staticmethod
    def _position_max_loss(intent: OptionStrategyIntent) -> float:
        return intent.max_loss * intent.contract_multiplier * intent.contracts
