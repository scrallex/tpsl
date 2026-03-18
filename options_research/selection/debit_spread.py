"""Interfaces and config for vertical debit spread selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Protocol

from options_research.models import (
    LegAction,
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionRight,
    OptionStrategyIntent,
    SignalDirection,
    SignalEvent,
)
from options_research.strategies import (
    DirectionalExpressionConfig,
    VerticalDebitSpreadExpressionMapper,
)


@dataclass(frozen=True, slots=True)
class DebitSpreadSelectionConfig:
    min_dte: int = 21
    max_dte: int = 45
    target_dte: int | None = None
    long_delta_min: float = 0.30
    long_delta_max: float = 0.45
    short_delta_offset: float | None = 0.15
    spread_width: float | None = None
    max_relative_spread: float = 0.20
    min_open_interest: int = 100
    min_volume: int = 1
    allow_moneyness_fallback: bool = True

    def __post_init__(self) -> None:
        if self.min_dte <= 0 or self.max_dte <= 0:
            raise ValueError("DTE bounds must be positive")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte cannot exceed max_dte")
        if self.target_dte is not None and not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("target_dte must fall within the DTE band")
        if not 0.0 <= self.long_delta_min <= self.long_delta_max <= 1.0:
            raise ValueError("long delta bounds must fit within [0.0, 1.0]")
        if self.short_delta_offset is not None and self.short_delta_offset <= 0:
            raise ValueError("short_delta_offset must be positive when provided")
        if self.spread_width is not None and self.spread_width <= 0:
            raise ValueError("spread_width must be positive when provided")
        if self.spread_width is None and self.short_delta_offset is None:
            raise ValueError("either spread_width or short_delta_offset must be configured")
        if self.max_relative_spread < 0:
            raise ValueError("max_relative_spread must be non-negative")
        if self.min_open_interest < 0 or self.min_volume < 0:
            raise ValueError("liquidity thresholds must be non-negative")

    @property
    def preferred_dte(self) -> int:
        if self.target_dte is not None:
            return self.target_dte
        return int(round((self.min_dte + self.max_dte) / 2.0))

    @property
    def target_long_delta(self) -> float:
        return (self.long_delta_min + self.long_delta_max) / 2.0


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    accepted: bool
    intent: OptionStrategyIntent | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.accepted and self.intent is None:
            raise ValueError("accepted outcomes require an intent")
        if not self.accepted and not self.rejection_reason:
            raise ValueError("rejected outcomes require a rejection_reason")


class DebitSpreadSelector(Protocol):
    """Chooses a defined-risk debit spread from a historical chain snapshot."""

    def select(
        self,
        *,
        signal: SignalEvent,
        snapshot: OptionChainSnapshot,
    ) -> SelectionOutcome:
        ...


@dataclass(frozen=True, slots=True)
class _SelectionDirective:
    option_type: OptionRight
    strike_rule: Literal["higher", "lower"]


class VerticalDebitSpreadSelector:
    """Selects a defined-risk vertical debit spread from a historical chain snapshot."""

    def __init__(
        self,
        config: DebitSpreadSelectionConfig | None = None,
        *,
        expression_config: DirectionalExpressionConfig | None = None,
    ) -> None:
        self.config = config or DebitSpreadSelectionConfig()
        self.expression_mapper = VerticalDebitSpreadExpressionMapper(expression_config)

    def select(
        self,
        *,
        signal: SignalEvent,
        snapshot: OptionChainSnapshot,
    ) -> SelectionOutcome:
        if signal.underlying != snapshot.underlying:
            return SelectionOutcome(
                accepted=False,
                rejection_reason="signal_underlying_mismatch",
            )
        if snapshot.as_of < signal.occurred_at:
            return SelectionOutcome(
                accepted=False,
                rejection_reason="snapshot_precedes_signal",
            )

        directive = self._resolve_directive(signal.direction)
        if directive is None:
            return SelectionOutcome(
                accepted=False,
                rejection_reason="unsupported_signal_direction",
            )

        chain = snapshot.filter_quotes(
            option_type=directive.option_type,
            min_dte=self.config.min_dte,
            max_dte=self.config.max_dte,
        )
        if not chain:
            return SelectionOutcome(
                accepted=False,
                rejection_reason="no_quotes_in_dte_band",
            )

        expiries = sorted(
            {quote.expiry for quote in chain},
            key=lambda expiry: (
                abs((expiry - snapshot.as_of.date()).days - self.config.preferred_dte),
                (expiry - snapshot.as_of.date()).days,
            ),
        )

        saw_long_candidate = False
        saw_short_candidate = False
        saw_valid_debit = False
        saw_missing_delta_long_candidates = False

        for expiry in expiries:
            expiry_quotes = [quote for quote in chain if quote.expiry == expiry]
            liquid_quotes = [quote for quote in expiry_quotes if self._is_liquid(quote)]
            if not self.config.allow_moneyness_fallback and any(quote.delta is None for quote in liquid_quotes):
                saw_missing_delta_long_candidates = True
            long_candidates = [
                quote
                for quote in liquid_quotes
                if self._is_long_candidate(
                    quote=quote,
                    directive=directive,
                    underlying_spot=snapshot.underlying_spot,
                )
            ]
            if not long_candidates:
                continue
            saw_long_candidate = True

            for long_quote in sorted(
                long_candidates,
                key=lambda quote: self._long_candidate_rank(
                    quote=quote,
                    directive=directive,
                    underlying_spot=snapshot.underlying_spot,
                ),
            ):
                short_quote = self._select_short_quote(
                    long_quote=long_quote,
                    expiry_quotes=expiry_quotes,
                    directive=directive,
                )
                if short_quote is None:
                    continue
                saw_short_candidate = True

                strike_width = abs(short_quote.strike - long_quote.strike)
                net_debit = long_quote.mid_price - short_quote.mid_price
                if net_debit <= 0 or net_debit >= strike_width:
                    continue
                saw_valid_debit = True

                legs = (
                    OptionLeg(action=LegAction.BUY, quantity=1, quote=long_quote),
                    OptionLeg(action=LegAction.SELL, quantity=1, quote=short_quote),
                )
                intent = self.expression_mapper.build_intent(signal=signal, legs=legs)
                metadata = dict(intent.metadata)
                metadata.update(
                    {
                        "selector": "vertical_debit_spread",
                        "selection_basis": "spread_width"
                        if self.config.spread_width is not None
                        else "delta_offset",
                        "long_leg_selection_mode": "delta_band"
                        if long_quote.delta is not None
                        else "closest_liquid_otm_fallback",
                        "selected_dte": long_quote.days_to_expiry,
                        "selected_long_delta": abs(long_quote.delta) if long_quote.delta is not None else None,
                        "selected_short_delta": abs(short_quote.delta) if short_quote.delta is not None else None,
                        "selected_width": strike_width,
                        "selected_long_relative_spread": self._relative_spread(long_quote),
                        "selected_short_relative_spread": self._relative_spread(short_quote),
                    }
                )
                return SelectionOutcome(
                    accepted=True,
                    intent=replace(intent, metadata=metadata),
                )

        if not saw_long_candidate:
            rejection_reason = (
                "no_delta_long_leg_candidates"
                if saw_missing_delta_long_candidates and not self.config.allow_moneyness_fallback
                else "no_liquid_long_leg_candidates"
            )
        elif not saw_short_candidate:
            rejection_reason = "no_short_leg_match"
        elif not saw_valid_debit:
            rejection_reason = "no_valid_debit_structure"
        else:
            rejection_reason = "selection_failed"
        return SelectionOutcome(
            accepted=False,
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def _resolve_directive(direction: SignalDirection) -> _SelectionDirective | None:
        if direction is SignalDirection.BULLISH:
            return _SelectionDirective(
                option_type=OptionRight.CALL,
                strike_rule="higher",
            )
        if direction is SignalDirection.BEARISH:
            return _SelectionDirective(
                option_type=OptionRight.PUT,
                strike_rule="lower",
            )
        return None

    def _is_long_candidate(
        self,
        *,
        quote: OptionQuote,
        directive: _SelectionDirective,
        underlying_spot: float,
    ) -> bool:
        if quote.delta is None:
            return self.config.allow_moneyness_fallback and underlying_spot > 0
        absolute_delta = abs(quote.delta)
        return self.config.long_delta_min <= absolute_delta <= self.config.long_delta_max

    def _is_liquid(self, quote: OptionQuote) -> bool:
        if quote.volume is None or quote.open_interest is None:
            return False
        if quote.volume < self.config.min_volume or quote.open_interest < self.config.min_open_interest:
            return False
        return self._relative_spread(quote) <= self.config.max_relative_spread

    def _long_candidate_rank(
        self,
        *,
        quote: OptionQuote,
        directive: _SelectionDirective,
        underlying_spot: float,
    ) -> tuple[float, float, float, int, int]:
        if quote.delta is not None:
            selection_penalty = 0.0
            primary_distance = abs(abs(quote.delta) - self.config.target_long_delta)
        else:
            selection_penalty = 1.0
            primary_distance = self._fallback_moneyness_distance(
                quote=quote,
                directive=directive,
                underlying_spot=underlying_spot,
            )
        open_interest = quote.open_interest or 0
        volume = quote.volume or 0
        return (
            selection_penalty,
            primary_distance,
            self._relative_spread(quote),
            -open_interest,
            -volume,
        )

    def _select_short_quote(
        self,
        *,
        long_quote: OptionQuote,
        expiry_quotes: list[OptionQuote],
        directive: _SelectionDirective,
    ) -> OptionQuote | None:
        candidates = [
            quote
            for quote in expiry_quotes
            if quote.contract_symbol != long_quote.contract_symbol
            and self._is_liquid(quote)
            and self._matches_strike_rule(
                long_quote=long_quote,
                short_quote=quote,
                strike_rule=directive.strike_rule,
            )
        ]
        if not candidates:
            return None

        target_short_delta = self._target_short_delta(long_quote)
        if self.config.spread_width is not None:
            ranked = sorted(
                candidates,
                key=lambda quote: (
                    abs(abs(quote.strike - long_quote.strike) - self.config.spread_width),
                    abs(abs(quote.delta) - target_short_delta)
                    if quote.delta is not None and target_short_delta is not None
                    else 1.0,
                    self._relative_spread(quote),
                    -(quote.open_interest or 0),
                    -(quote.volume or 0),
                ),
            )
            return ranked[0]

        delta_candidates = [quote for quote in candidates if quote.delta is not None]
        if not delta_candidates or target_short_delta is None:
            return None
        ranked = sorted(
            delta_candidates,
            key=lambda quote: (
                abs(abs(quote.delta) - target_short_delta),
                self._relative_spread(quote),
                -(quote.open_interest or 0),
                -(quote.volume or 0),
            ),
        )
        return ranked[0]

    @staticmethod
    def _matches_strike_rule(
        *,
        long_quote: OptionQuote,
        short_quote: OptionQuote,
        strike_rule: Literal["higher", "lower"],
    ) -> bool:
        if strike_rule == "higher":
            return short_quote.strike > long_quote.strike
        return short_quote.strike < long_quote.strike

    def _target_short_delta(self, long_quote: OptionQuote) -> float | None:
        if self.config.short_delta_offset is None or long_quote.delta is None:
            return None
        return max(0.0, abs(long_quote.delta) - self.config.short_delta_offset)

    @staticmethod
    def _fallback_moneyness_distance(
        *,
        quote: OptionQuote,
        directive: _SelectionDirective,
        underlying_spot: float,
    ) -> float:
        if underlying_spot <= 0:
            return float("inf")
        if directive.option_type is OptionRight.CALL:
            signed_otm_pct = (quote.strike / underlying_spot) - 1.0
        else:
            signed_otm_pct = 1.0 - (quote.strike / underlying_spot)
        if signed_otm_pct >= 0:
            return signed_otm_pct
        return 1.0 + abs(signed_otm_pct)

    @staticmethod
    def _relative_spread(quote: OptionQuote) -> float:
        mid = quote.mid_price
        if mid <= 0:
            return float("inf")
        return quote.spread_width / mid
