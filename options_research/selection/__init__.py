"""Contract selection interfaces for defined-risk option structures."""

from .debit_spread import (
    DebitSpreadSelectionConfig,
    DebitSpreadSelector,
    SelectionOutcome,
    VerticalDebitSpreadSelector,
)

__all__ = [
    "DebitSpreadSelectionConfig",
    "DebitSpreadSelector",
    "SelectionOutcome",
    "VerticalDebitSpreadSelector",
]
