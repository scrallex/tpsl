#!/usr/bin/env python3
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple


def _load_pip_scales() -> Tuple[Dict[str, float], float]:
    try:
        root_dir = Path(__file__).resolve().parents[3]
        config_path = root_dir / "config" / "pip_scales.json"
        if config_path.exists():
            with open(config_path, "r") as f:
                data = json.load(f)
                return data.get("instruments", {}), data.get("default", 0.0001)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return {
        "USD_JPY": 0.01,
        "EUR_JPY": 0.01,
        "GBP_JPY": 0.01,
        "AUD_JPY": 0.01,
        "NZD_JPY": 0.01,
        "CAD_JPY": 0.01,
        "CHF_JPY": 0.01,
    }, 0.0001


_PIP_SCALES, _DEFAULT_PIP_SCALE = _load_pip_scales()


def pip_scale(instrument: str) -> float:
    return _PIP_SCALES.get(instrument.upper(), _DEFAULT_PIP_SCALE)


@dataclass(frozen=True)
class TPSLConfig:
    """Immutable TP/SL parameters for a single instrument or trade."""

    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    stop_loss_pips: Optional[int] = None
    take_profit_pips: Optional[int] = None
    trailing_stop_pct: Optional[float] = None
    breakeven_trigger_pct: Optional[float] = None

    @property
    def active(self) -> bool:
        return any(
            (
                self.stop_loss_pct is not None,
                self.take_profit_pct is not None,
                self.stop_loss_pips is not None,
                self.take_profit_pips is not None,
                self.trailing_stop_pct is not None,
                self.breakeven_trigger_pct is not None,
            )
        )

    def effective_sl(
        self, instrument: str, entry_price: float = 1.0
    ) -> Optional[float]:
        if self.stop_loss_pct is not None:
            return abs(self.stop_loss_pct)
        if self.stop_loss_pips is not None:
            return (abs(self.stop_loss_pips) * pip_scale(instrument)) / entry_price
        return None

    def effective_tp(
        self, instrument: str, entry_price: float = 1.0
    ) -> Optional[float]:
        if self.take_profit_pct is not None:
            return abs(self.take_profit_pct)
        if self.take_profit_pips is not None:
            return (abs(self.take_profit_pips) * pip_scale(instrument)) / entry_price
        return None
