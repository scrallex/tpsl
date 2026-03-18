"""Bid/ask-aware spread fill simulation for packaged option spreads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

from options_research.models import (
    ExitReason,
    FilledOptionPosition,
    LegAction,
    OptionChainSnapshot,
    OptionLeg,
    OptionQuote,
    OptionStrategyIntent,
    PackageFill,
)


@dataclass(frozen=True, slots=True)
class FillPolicy:
    price_reference: Literal["mid", "natural"] = "mid"
    open_penalty_half_spreads: float = 0.25
    close_penalty_half_spreads: float = 0.25
    per_contract_commission: float = 0.65
    per_contract_fee: float = 0.03
    enforce_inside_market: bool = True

    def __post_init__(self) -> None:
        if self.price_reference not in {"mid", "natural"}:
            raise ValueError("price_reference must be 'mid' or 'natural'")
        for field_name in (
            "open_penalty_half_spreads",
            "close_penalty_half_spreads",
            "per_contract_commission",
            "per_contract_fee",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")


class SpreadExecutionSimulator(Protocol):
    """Simulates packaged spread fills from historical chain quotes."""

    def fill_open(self, *, intent: OptionStrategyIntent, snapshot: OptionChainSnapshot) -> PackageFill | None:
        ...

    def fill_close(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        reason: ExitReason,
    ) -> PackageFill | None:
        ...

    def mark_position(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        price_reference: Literal["mid", "natural"] = "mid",
    ) -> float | None:
        ...


class PackagedSpreadFillSimulator:
    """Simulates package-level fills and marks using historical bid/ask quotes."""

    def __init__(self, policy: FillPolicy | None = None) -> None:
        self.policy = policy or FillPolicy()

    def fill_open(self, *, intent: OptionStrategyIntent, snapshot: OptionChainSnapshot) -> PackageFill | None:
        quote_pairs = self._resolve_quote_pairs(intent.legs, snapshot)
        if quote_pairs is None:
            return None
        leg_prices = self._price_legs(
            legs=intent.legs,
            quote_pairs=quote_pairs,
            penalty_half_spreads=self.policy.open_penalty_half_spreads,
            price_reference=self.policy.price_reference,
            opening=True,
        )
        if leg_prices is None:
            return None

        net_price = self._net_debit(intent.legs, leg_prices)
        if net_price <= 0:
            return None
        reference_mid = self._net_debit(
            intent.legs,
            tuple(current_quote.mid_price for _, current_quote in quote_pairs),
        )
        contract_count = self._filled_contract_count(intent.legs, intent.contracts)
        return PackageFill(
            filled_at=snapshot.as_of,
            net_price=net_price,
            leg_prices=leg_prices,
            commission=self.policy.per_contract_commission * contract_count,
            fees=self.policy.per_contract_fee * contract_count,
            slippage=abs(net_price - reference_mid),
            metadata={
                "fill_side": "open",
                "price_reference": self.policy.price_reference,
                "reference_mid": reference_mid,
            },
        )

    def fill_close(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        reason: ExitReason,
    ) -> PackageFill | None:
        quote_pairs = self._resolve_quote_pairs(position.intent.legs, snapshot)
        if quote_pairs is None:
            return None
        closing_legs = tuple(self._closing_leg(leg, quote) for leg, quote in quote_pairs)
        leg_prices = self._price_legs(
            legs=closing_legs,
            quote_pairs=quote_pairs,
            penalty_half_spreads=self.policy.close_penalty_half_spreads,
            price_reference=self.policy.price_reference,
            opening=False,
        )
        if leg_prices is None:
            return None

        net_price = self._net_credit(closing_legs, leg_prices)
        reference_mid = self._net_credit(
            closing_legs,
            tuple(current_quote.mid_price for _, current_quote in quote_pairs),
        )
        contract_count = self._filled_contract_count(position.intent.legs, position.intent.contracts)
        return PackageFill(
            filled_at=snapshot.as_of,
            net_price=net_price,
            leg_prices=leg_prices,
            commission=self.policy.per_contract_commission * contract_count,
            fees=self.policy.per_contract_fee * contract_count,
            slippage=abs(net_price - reference_mid),
            metadata={
                "fill_side": "close",
                "exit_reason": reason.value,
                "price_reference": self.policy.price_reference,
                "reference_mid": reference_mid,
            },
        )

    def mark_position(
        self,
        *,
        position: FilledOptionPosition,
        snapshot: OptionChainSnapshot,
        price_reference: Literal["mid", "natural"] = "mid",
    ) -> float | None:
        quote_pairs = self._resolve_quote_pairs(position.intent.legs, snapshot)
        if quote_pairs is None:
            return None
        closing_legs = tuple(self._closing_leg(leg, quote) for leg, quote in quote_pairs)
        mark_prices = self._price_legs(
            legs=closing_legs,
            quote_pairs=quote_pairs,
            penalty_half_spreads=0.0,
            price_reference=price_reference,
            opening=False,
        )
        if mark_prices is None:
            return None
        package_value = self._net_credit(closing_legs, mark_prices)
        return max(0.0, package_value)

    @staticmethod
    def _closing_leg(original_leg: OptionLeg, current_quote: OptionQuote) -> OptionLeg:
        action = LegAction.SELL if original_leg.action is LegAction.BUY else LegAction.BUY
        return OptionLeg(
            action=action,
            quantity=original_leg.quantity,
            quote=current_quote,
        )

    def _resolve_quote_pairs(
        self,
        legs: Sequence[OptionLeg],
        snapshot: OptionChainSnapshot,
    ) -> tuple[tuple[OptionLeg, OptionQuote], ...] | None:
        if not legs or snapshot.underlying != legs[0].quote.underlying:
            return None
        quote_lookup = {quote.contract_symbol: quote for quote in snapshot.quotes}
        resolved: list[tuple[OptionLeg, OptionQuote]] = []
        for leg in legs:
            current_quote = quote_lookup.get(leg.quote.contract_symbol)
            if current_quote is None:
                return None
            resolved.append((leg, current_quote))
        return tuple(resolved)

    def _price_legs(
        self,
        *,
        legs: Sequence[OptionLeg],
        quote_pairs: Sequence[tuple[OptionLeg, OptionQuote]],
        penalty_half_spreads: float,
        price_reference: Literal["mid", "natural"],
        opening: bool,
    ) -> tuple[float, ...] | None:
        prices: list[float] = []
        for leg, current_quote in zip(legs, (pair[1] for pair in quote_pairs), strict=True):
            if price_reference == "natural":
                leg_price = current_quote.ask if leg.action is LegAction.BUY else current_quote.bid
            else:
                half_spread = current_quote.spread_width / 2.0
                if leg.action is LegAction.BUY:
                    leg_price = current_quote.mid_price + (penalty_half_spreads * half_spread)
                else:
                    leg_price = current_quote.mid_price - (penalty_half_spreads * half_spread)
            if self.policy.enforce_inside_market and not self._inside_market(leg_price, current_quote):
                return None
            if leg_price < 0:
                return None
            prices.append(leg_price)
        return tuple(prices)

    @staticmethod
    def _inside_market(price: float, quote: OptionQuote) -> bool:
        tolerance = 1e-9
        return (quote.bid - tolerance) <= price <= (quote.ask + tolerance)

    @staticmethod
    def _net_debit(legs: Sequence[OptionLeg], prices: Sequence[float]) -> float:
        return sum(
            price * leg.quantity if leg.action is LegAction.BUY else -price * leg.quantity
            for leg, price in zip(legs, prices, strict=True)
        )

    @staticmethod
    def _net_credit(legs: Sequence[OptionLeg], prices: Sequence[float]) -> float:
        return sum(
            price * leg.quantity if leg.action is LegAction.SELL else -price * leg.quantity
            for leg, price in zip(legs, prices, strict=True)
        )

    @staticmethod
    def _filled_contract_count(legs: Sequence[OptionLeg], contracts: int) -> int:
        return contracts * sum(leg.quantity for leg in legs)
