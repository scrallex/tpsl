#!/usr/bin/env python3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .config import TPSLConfig

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

_EMPTY_CONFIG = TPSLConfig()


class TPSLConfigStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._configs: Dict[str, TPSLConfig] = {}
        self._global: TPSLConfig = _EMPTY_CONFIG
        if path is not None:
            self.load(path)

    def load(self, path: Path) -> None:
        if yaml is None or not path.exists():
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._global = self._parse_config(raw.get("global") or {})
        instruments = raw.get("instruments") or {}
        for symbol, payload in instruments.items():
            if not isinstance(payload, Mapping):
                continue
            self._configs[symbol.upper()] = self._parse_config(payload)

    def get(self, instrument: str) -> TPSLConfig:
        inst = instrument.upper()
        if inst in self._configs:
            return self._configs[inst]
        return self._global

    def all_instruments(self) -> Dict[str, TPSLConfig]:
        return dict(self._configs)

    @staticmethod
    def _parse_config(data: Mapping[str, Any]) -> TPSLConfig:
        def _float(key: str) -> Optional[float]:
            val = data.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        def _int(key: str) -> Optional[int]:
            val = data.get(key)
            if val is None:
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                return None

        return TPSLConfig(
            stop_loss_pct=_float("stop_loss_pct"),
            take_profit_pct=_float("take_profit_pct"),
            stop_loss_pips=_int("stop_loss_pips"),
            take_profit_pips=_int("take_profit_pips"),
            trailing_stop_pct=_float("trailing_stop_pct"),
            breakeven_trigger_pct=_float("breakeven_trigger_pct"),
        )
