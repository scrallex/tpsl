"""Interfaces for signal generation from underlying price action."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from options_research.models import SignalEvent, UnderlyingBar


@dataclass(frozen=True, slots=True)
class SignalContext:
    underlying: str
    bars: tuple[UnderlyingBar, ...]
    metadata: dict[str, object] = field(default_factory=dict)


class UnderlyingSignalGenerator(Protocol):
    """Transforms underlying bars into directional signal events."""

    def generate(self, context: SignalContext) -> Sequence[SignalEvent]:
        ...
