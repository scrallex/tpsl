"""Historical SEP gate-file adapter for the isolated options research package."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from options_research.env import load_options_env
from options_research.models import SignalDirection, SignalEvent
from options_research.signals.base import SignalContext


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class SEPRegimeSignalConfig:
    signal_name: str = "sep_regime_adapter"
    min_regime_confidence: float = 0.0
    allowed_sources: tuple[str, ...] = ()
    require_admit: bool = True
    emit_on_direction_change_only: bool = True
    gate_root: Path | None = None
    gate_file_pattern: str = "{underlying}.gates.jsonl"
    require_gate_file: bool = True

    def __post_init__(self) -> None:
        if not self.signal_name:
            raise ValueError("signal_name is required")
        if not 0.0 <= self.min_regime_confidence <= 1.0:
            raise ValueError("min_regime_confidence must be within [0.0, 1.0]")
        if not self.gate_file_pattern:
            raise ValueError("gate_file_pattern is required")


class SEPRegimeSignalGenerator:
    """Maps historical SEP gate records into directional signal events."""

    def __init__(self, config: SEPRegimeSignalConfig | None = None) -> None:
        load_options_env()
        self.config = config or SEPRegimeSignalConfig()

    def generate(self, context: SignalContext) -> list[SignalEvent]:
        if not context.bars:
            return []

        gate_records = self._load_gate_events(
            underlying=context.underlying,
            metadata=context.metadata,
        )
        if not gate_records:
            return []

        start = context.metadata.get("backtest_start")
        end = context.metadata.get("backtest_end")
        start_dt = self._coerce_datetime(start)
        end_dt = self._coerce_datetime(end)

        events: list[SignalEvent] = []
        last_direction: SignalDirection | None = None
        allowed_sources = {source.lower() for source in self.config.allowed_sources}

        for record in gate_records:
            if self.config.require_admit and not bool(record.get("admit", 1)):
                continue

            direction = self._map_direction(record.get("direction"))
            if direction is None:
                continue

            source = str(record.get("source") or "").strip().lower()
            if allowed_sources and source not in allowed_sources:
                continue

            regime_confidence = self._resolve_regime_confidence(record)
            if regime_confidence < self.config.min_regime_confidence:
                continue

            occurred_at = self._resolve_timestamp(record)
            if occurred_at is None:
                continue
            if start_dt is not None and occurred_at < start_dt:
                continue
            if end_dt is not None and occurred_at > end_dt:
                continue
            if self.config.emit_on_direction_change_only and direction is last_direction:
                continue

            metadata = {
                "source": source,
                "hazard": self._coerce_float(record.get("hazard"), default=0.0),
                "structural_tension": self._coerce_float(record.get("structural_tension"), default=None),
                "repetition_count": int(record.get("repetition_count") or record.get("repetitions") or 0),
                "components": dict(record.get("components") or {}),
                "bundle_hits": list(record.get("bundle_hits") or []),
                "reasons": list(record.get("reasons") or []),
                "gate_file": str(self._resolve_gate_path(underlying=context.underlying, metadata=context.metadata)),
            }
            events.append(
                SignalEvent(
                    underlying=context.underlying,
                    occurred_at=occurred_at,
                    direction=direction,
                    signal_name=self.config.signal_name,
                    strength=max(0.0, min(1.0, max(regime_confidence, metadata["hazard"] or 0.0))),
                    regime=self._resolve_regime(record),
                    metadata=metadata,
                )
            )
            last_direction = direction

        return events

    def _load_gate_events(self, *, underlying: str, metadata: dict[str, object]) -> list[dict[str, Any]]:
        path = self._resolve_gate_path(underlying=underlying, metadata=metadata)
        if not path.exists():
            if self.config.require_gate_file:
                raise FileNotFoundError(f"SEP gate file not found: {path}")
            return []

        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [record for record in payload if isinstance(record, dict)]
            raise ValueError(f"SEP gate file at {path} did not contain a JSON array")

        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _resolve_gate_path(self, *, underlying: str, metadata: dict[str, object]) -> Path:
        override = metadata.get("gate_path")
        if isinstance(override, str) and override:
            return Path(override)
        if isinstance(override, Path):
            return override

        root_override = metadata.get("gate_root")
        if isinstance(root_override, str) and root_override:
            root = Path(root_override)
        elif isinstance(root_override, Path):
            root = root_override
        else:
            root = self.config.gate_root or Path(os.getenv("OPTIONS_RESEARCH_GATE_ROOT", "data/options_research/gates"))
        return root / self.config.gate_file_pattern.format(underlying=underlying.upper(), symbol=underlying.upper())

    @staticmethod
    def _map_direction(value: object) -> SignalDirection | None:
        normalized = str(value or "").strip().upper()
        if normalized == "BUY":
            return SignalDirection.BULLISH
        if normalized == "SELL":
            return SignalDirection.BEARISH
        return None

    @staticmethod
    def _resolve_regime(record: dict[str, Any]) -> str | None:
        regime = record.get("regime")
        if isinstance(regime, dict):
            label = regime.get("label")
            return str(label) if label else None
        if regime:
            return str(regime)
        return None

    @staticmethod
    def _resolve_regime_confidence(record: dict[str, Any]) -> float:
        top_level = record.get("regime_confidence")
        if top_level not in (None, ""):
            return float(top_level)
        regime = record.get("regime")
        if isinstance(regime, dict):
            nested = regime.get("confidence")
            if nested not in (None, ""):
                return float(nested)
        return 0.0

    @staticmethod
    def _resolve_timestamp(record: dict[str, Any]) -> datetime | None:
        candidates: Iterable[object] = (
            record.get("ts_ms"),
            record.get("end_ms"),
            record.get("time"),
        )
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            if isinstance(candidate, (int, float)):
                scale = 1000.0 if float(candidate) > 1e11 else 1.0
                return datetime.fromtimestamp(float(candidate) / scale, tz=UTC)
            if isinstance(candidate, str):
                try:
                    return datetime.fromisoformat(candidate.replace("Z", "+00:00")).astimezone(UTC)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _coerce_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        raise TypeError(f"Unsupported datetime value: {value!r}")

    @staticmethod
    def _coerce_float(value: object, *, default: float | None) -> float | None:
        if value in (None, ""):
            return default
        return float(value)
