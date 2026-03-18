"""Outcome study for historical SEP gate artifacts on underlying returns."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import mean, median
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from options_research.data import MarketDataClient


NEW_YORK = ZoneInfo("America/New_York")


def _ensure_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GateOutcomeStudyConfig:
    symbol: str
    gate_path: Path
    output_path: Path
    intraday_resolution_minutes: int = 1
    request_chunk_days: int = 30
    intraday_horizons_minutes: tuple[int, ...] = (5, 15, 30, 60)
    trading_day_horizons: tuple[int, ...] = (1, 3)
    regular_session_timezone: str = "America/New_York"
    include_only_admitted: bool = True
    calendar_buffer_days: int = 7
    extended_hours: bool = False
    adjust_splits: bool = False

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.gate_path:
            raise ValueError("gate_path is required")
        if self.intraday_resolution_minutes <= 0:
            raise ValueError("intraday_resolution_minutes must be positive")
        if self.request_chunk_days <= 0:
            raise ValueError("request_chunk_days must be positive")
        if not self.intraday_horizons_minutes:
            raise ValueError("intraday_horizons_minutes must not be empty")
        if any(horizon <= 0 for horizon in self.intraday_horizons_minutes):
            raise ValueError("intraday_horizons_minutes must be positive")
        if any(horizon <= 0 for horizon in self.trading_day_horizons):
            raise ValueError("trading_day_horizons must be positive")
        if self.calendar_buffer_days <= 0:
            raise ValueError("calendar_buffer_days must be positive")


@dataclass(frozen=True, slots=True)
class GateObservation:
    occurred_at: datetime
    market_date: str
    time_bucket: str
    source: str
    regime: str
    confidence: float
    confidence_bucket: str
    hazard: float
    hazard_bucket: str
    direction: str
    bundle_hits: tuple[str, ...]
    returns: dict[str, float | None]
    directional_returns: dict[str, float | None]


class GateOutcomeStudyRunner:
    """Runs a forward-return study on a historical gate file."""

    def __init__(self, client: MarketDataClient | None = None) -> None:
        self.client = client or MarketDataClient()

    def run(self, config: GateOutcomeStudyConfig) -> dict[str, object]:
        gates = self.load_gates(config.gate_path)
        admitted_gates = [
            gate
            for gate in gates
            if (not config.include_only_admitted or bool(gate.get("admit")))
            and str(gate.get("direction") or "").upper() in {"BUY", "SELL"}
        ]
        if not admitted_gates:
            raise ValueError(f"No eligible gates found in {config.gate_path}")

        price_frame = self.fetch_price_frame(config=config, gates=admitted_gates)
        observations = self.build_observations(
            config=config,
            gates=admitted_gates,
            price_frame=price_frame,
        )
        study = self._serialize_study(
            config=config,
            all_gate_count=len(gates),
            admitted_gate_count=len(admitted_gates),
            price_frame=price_frame,
            observations=observations,
        )
        config.output_path.parent.mkdir(parents=True, exist_ok=True)
        config.output_path.write_text(json.dumps(study, indent=2, sort_keys=True), encoding="utf-8")
        return study

    @staticmethod
    def load_gates(path: Path) -> list[dict[str, object]]:
        if not path.exists():
            raise FileNotFoundError(f"Gate file not found: {path}")
        records: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def fetch_price_frame(
        self,
        *,
        config: GateOutcomeStudyConfig,
        gates: list[dict[str, object]],
    ) -> pd.DataFrame:
        start = self._gate_timestamp(gates[0]) - timedelta(days=1)
        end = self._gate_timestamp(gates[-1]) + timedelta(days=config.calendar_buffer_days)
        frames: list[pd.DataFrame] = []
        cursor = start
        step = timedelta(seconds=1)
        chunk_span = timedelta(days=config.request_chunk_days)
        while cursor < end:
            chunk_end = min(cursor + chunk_span, end)
            frame = self.client.fetch_intraday_bars(
                symbol=config.symbol,
                resolution_minutes=config.intraday_resolution_minutes,
                start=cursor,
                end=chunk_end,
                adjust_splits=config.adjust_splits,
                extended_hours=config.extended_hours,
            )
            if not frame.empty:
                frames.append(frame)
            if chunk_end >= end:
                break
            cursor = chunk_end + step
        if not frames:
            raise ValueError(f"No intraday prices returned for {config.symbol} between {start} and {end}")
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
        return combined.reset_index(drop=True)

    def build_observations(
        self,
        *,
        config: GateOutcomeStudyConfig,
        gates: list[dict[str, object]],
        price_frame: pd.DataFrame,
    ) -> list[GateObservation]:
        market_tz = ZoneInfo(config.regular_session_timezone)
        timestamps = [pd.Timestamp(value).to_pydatetime() for value in price_frame["timestamp"]]
        closes = [float(value) for value in price_frame["close"]]
        market_dates = [timestamp.astimezone(market_tz).date().isoformat() for timestamp in timestamps]

        close_index_by_date: dict[str, int] = {}
        for index, market_date in enumerate(market_dates):
            close_index_by_date[market_date] = index
        ordered_dates = sorted(close_index_by_date)
        date_position = {market_date: idx for idx, market_date in enumerate(ordered_dates)}

        observations: list[GateObservation] = []
        for gate in gates:
            occurred_at = self._gate_timestamp(gate)
            entry_index = bisect_left(timestamps, occurred_at)
            if entry_index >= len(timestamps):
                continue
            entry_price = closes[entry_index]
            market_date = market_dates[entry_index]
            direction = str(gate.get("direction") or "").upper()
            direction_sign = 1.0 if direction == "BUY" else -1.0

            raw_returns: dict[str, float | None] = {}
            directional_returns: dict[str, float | None] = {}

            for horizon in config.intraday_horizons_minutes:
                key = f"{horizon}m"
                target_index = entry_index + int(horizon / config.intraday_resolution_minutes)
                if target_index >= len(closes):
                    raw_returns[key] = None
                    directional_returns[key] = None
                    continue
                value = (closes[target_index] / entry_price) - 1.0
                raw_returns[key] = value
                directional_returns[key] = value * direction_sign

            session_close_index = close_index_by_date.get(market_date)
            if session_close_index is None or session_close_index < entry_index:
                raw_returns["close"] = None
                directional_returns["close"] = None
            else:
                value = (closes[session_close_index] / entry_price) - 1.0
                raw_returns["close"] = value
                directional_returns["close"] = value * direction_sign

            current_date_pos = date_position[market_date]
            for horizon in config.trading_day_horizons:
                key = f"{horizon}d"
                future_pos = current_date_pos + horizon
                if future_pos >= len(ordered_dates):
                    raw_returns[key] = None
                    directional_returns[key] = None
                    continue
                future_date = ordered_dates[future_pos]
                target_index = close_index_by_date[future_date]
                value = (closes[target_index] / entry_price) - 1.0
                raw_returns[key] = value
                directional_returns[key] = value * direction_sign

            confidence = self._regime_confidence(gate)
            hazard = float(gate.get("hazard") or 0.0)
            observations.append(
                GateObservation(
                    occurred_at=occurred_at,
                    market_date=market_date,
                    time_bucket=self._time_bucket(occurred_at.astimezone(market_tz)),
                    source=str(gate.get("source") or "unknown"),
                    regime=self._regime_label(gate),
                    confidence=confidence,
                    confidence_bucket=self._fixed_bucket(confidence),
                    hazard=hazard,
                    hazard_bucket=self._fixed_bucket(hazard),
                    direction=direction,
                    bundle_hits=self._bundle_hits(gate),
                    returns=raw_returns,
                    directional_returns=directional_returns,
                )
            )
        return observations

    def _serialize_study(
        self,
        *,
        config: GateOutcomeStudyConfig,
        all_gate_count: int,
        admitted_gate_count: int,
        price_frame: pd.DataFrame,
        observations: list[GateObservation],
    ) -> dict[str, object]:
        breakdowns = {
            "source": self._group_summary(observations, lambda item: (item.source,)),
            "regime": self._group_summary(observations, lambda item: (item.regime,)),
            "confidence_bucket": self._group_summary(observations, lambda item: (item.confidence_bucket,)),
            "hazard_bucket": self._group_summary(observations, lambda item: (item.hazard_bucket,)),
            "time_of_day": self._group_summary(observations, lambda item: (item.time_bucket,)),
            "bundle_hit": self._group_summary(
                observations,
                lambda item: item.bundle_hits if item.bundle_hits else ("none",),
            ),
        }
        timestamps = [pd.Timestamp(value).to_pydatetime() for value in price_frame["timestamp"]]
        return {
            "symbol": config.symbol,
            "gate_path": str(config.gate_path),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "gate_count": all_gate_count,
            "eligible_gate_count": admitted_gate_count,
            "observation_count": len(observations),
            "horizons": [f"{value}m" for value in config.intraday_horizons_minutes]
            + ["close"]
            + [f"{value}d" for value in config.trading_day_horizons],
            "price_window": {
                "start": timestamps[0].isoformat(),
                "end": timestamps[-1].isoformat(),
                "bars": len(price_frame),
                "resolution_minutes": config.intraday_resolution_minutes,
            },
            "overall": self._summary_for_group(observations),
            "breakdowns": breakdowns,
        }

    @staticmethod
    def _summary_for_group(observations: Iterable[GateObservation]) -> dict[str, object]:
        items = list(observations)
        directions = {
            "BUY": sum(1 for item in items if item.direction == "BUY"),
            "SELL": sum(1 for item in items if item.direction == "SELL"),
        }
        if not items:
            return {"count": 0, "directions": directions, "horizons": {}}

        horizon_keys = list(items[0].returns)
        horizon_summary: dict[str, object] = {}
        for key in horizon_keys:
            raw_values = [item.returns[key] for item in items if item.returns[key] is not None]
            directional_values = [
                item.directional_returns[key]
                for item in items
                if item.directional_returns[key] is not None
            ]
            horizon_summary[key] = {
                "count": len(raw_values),
                "mean_raw_return": mean(raw_values) if raw_values else None,
                "median_raw_return": median(raw_values) if raw_values else None,
                "mean_directional_return": mean(directional_values) if directional_values else None,
                "median_directional_return": median(directional_values) if directional_values else None,
                "directional_win_rate": (
                    sum(1 for value in directional_values if value > 0) / len(directional_values)
                    if directional_values
                    else None
                ),
            }

        return {
            "count": len(items),
            "directions": directions,
            "horizons": horizon_summary,
        }

    def _group_summary(
        self,
        observations: list[GateObservation],
        key_fn,
    ) -> dict[str, object]:
        groups: dict[str, list[GateObservation]] = {}
        for observation in observations:
            for key in key_fn(observation):
                groups.setdefault(key, []).append(observation)
        return {
            key: self._summary_for_group(value)
            for key, value in sorted(groups.items(), key=lambda item: item[0])
        }

    @staticmethod
    def _gate_timestamp(record: dict[str, object]) -> datetime:
        ts_ms = record.get("ts_ms")
        if ts_ms in (None, ""):
            raise ValueError("gate record is missing ts_ms")
        return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)

    @staticmethod
    def _regime_label(record: dict[str, object]) -> str:
        regime = record.get("regime")
        if isinstance(regime, dict):
            value = regime.get("label")
            return str(value or "unknown")
        return str(regime or "unknown")

    @staticmethod
    def _regime_confidence(record: dict[str, object]) -> float:
        if record.get("regime_confidence") not in (None, ""):
            return float(record["regime_confidence"])
        regime = record.get("regime")
        if isinstance(regime, dict) and regime.get("confidence") not in (None, ""):
            return float(regime["confidence"])
        return 0.0

    @staticmethod
    def _bundle_hits(record: dict[str, object]) -> tuple[str, ...]:
        raw = record.get("bundle_hits") or []
        bundle_ids: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                bundle_id = item.get("id")
                if bundle_id:
                    bundle_ids.append(str(bundle_id))
            elif item:
                bundle_ids.append(str(item))
        return tuple(sorted(set(bundle_ids)))

    @staticmethod
    def _fixed_bucket(value: float) -> str:
        if value < 0:
            return "<0.0"
        lower = min(int(value * 10), 9) / 10.0
        upper = lower + 0.1
        if value >= 1.0:
            return "1.0+"
        return f"{lower:.1f}-{upper:.1f}"

    @staticmethod
    def _time_bucket(timestamp: datetime) -> str:
        session_open = timestamp.replace(hour=9, minute=30, second=0, microsecond=0)
        delta = timestamp - session_open
        bucket_minutes = max(0, int(delta.total_seconds() // 60))
        bucket_start = session_open + timedelta(minutes=(bucket_minutes // 30) * 30)
        bucket_end = bucket_start + timedelta(minutes=30)
        return f"{bucket_start:%H:%M}-{bucket_end:%H:%M} ET"
