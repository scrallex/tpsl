"""Daily gate-compression study for end-of-day decision rules."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from .study import GateOutcomeStudyRunner


CompressionRule = Literal[
    "first_admitted",
    "strongest_admitted",
    "last_admitted",
    "majority_direction",
]


@dataclass(frozen=True, slots=True)
class GateCompressionStudyConfig:
    symbol: str
    gate_path: Path
    output_path: Path
    intraday_resolution_minutes: int = 1
    request_chunk_days: int = 30
    trading_day_horizons: tuple[int, ...] = (1, 3)
    regular_session_timezone: str = "America/New_York"
    include_only_admitted: bool = True
    calendar_buffer_days: int = 7
    extended_hours: bool = False
    adjust_splits: bool = False
    rules: tuple[CompressionRule, ...] = (
        "first_admitted",
        "strongest_admitted",
        "last_admitted",
        "majority_direction",
    )

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.gate_path:
            raise ValueError("gate_path is required")
        if self.intraday_resolution_minutes <= 0:
            raise ValueError("intraday_resolution_minutes must be positive")
        if self.request_chunk_days <= 0:
            raise ValueError("request_chunk_days must be positive")
        if not self.trading_day_horizons:
            raise ValueError("trading_day_horizons must not be empty")
        if any(horizon <= 0 for horizon in self.trading_day_horizons):
            raise ValueError("trading_day_horizons must be positive")
        if self.calendar_buffer_days <= 0:
            raise ValueError("calendar_buffer_days must be positive")
        invalid_rules = set(self.rules) - {
            "first_admitted",
            "strongest_admitted",
            "last_admitted",
            "majority_direction",
        }
        if invalid_rules:
            raise ValueError(f"unsupported compression rules: {sorted(invalid_rules)}")


@dataclass(frozen=True, slots=True)
class CompressedDecisionObservation:
    market_date: str
    rule_name: str
    decision_time: datetime
    representative_gate_time: datetime
    direction: str
    source: str
    regime: str
    hazard: float
    trend_strength: float
    admitted_gate_count: int
    buy_gate_count: int
    sell_gate_count: int
    returns: dict[str, float | None]
    directional_returns: dict[str, float | None]


class GateCompressionStudyRunner(GateOutcomeStudyRunner):
    """Evaluates end-of-day daily decision rules derived from intraday admitted gates."""

    def run(self, config: GateCompressionStudyConfig) -> dict[str, object]:
        gates = self.load_gates(config.gate_path)
        eligible_gates = [
            gate
            for gate in gates
            if (not config.include_only_admitted or bool(gate.get("admit")))
            and str(gate.get("direction") or "").upper() in {"BUY", "SELL"}
        ]
        if not eligible_gates:
            raise ValueError(f"No eligible gates found in {config.gate_path}")

        price_frame = self.fetch_price_frame(config=config, gates=eligible_gates)
        compressed = self.build_compressed_observations(
            config=config,
            gates=eligible_gates,
            price_frame=price_frame,
        )
        payload = self._serialize_payload(
            config=config,
            gate_count=len(gates),
            eligible_gate_count=len(eligible_gates),
            price_frame=price_frame,
            compressed=compressed,
        )
        config.output_path.parent.mkdir(parents=True, exist_ok=True)
        config.output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def build_compressed_observations(
        self,
        *,
        config: GateCompressionStudyConfig,
        gates: list[dict[str, object]],
        price_frame: pd.DataFrame,
    ) -> dict[str, dict[str, object]]:
        market_tz = ZoneInfo(config.regular_session_timezone)
        timestamps = [pd.Timestamp(value).to_pydatetime() for value in price_frame["timestamp"]]
        closes = [float(value) for value in price_frame["close"]]
        market_dates = [timestamp.astimezone(market_tz).date().isoformat() for timestamp in timestamps]
        close_index_by_date: dict[str, int] = {}
        for index, market_date in enumerate(market_dates):
            close_index_by_date[market_date] = index
        ordered_dates = sorted(close_index_by_date)
        date_position = {market_date: idx for idx, market_date in enumerate(ordered_dates)}

        grouped = self._group_gates_by_market_date(gates=gates, market_tz=market_tz)
        results: dict[str, dict[str, object]] = {}
        for rule_name in config.rules:
            observations: list[CompressedDecisionObservation] = []
            skip_reasons: Counter[str] = Counter()
            for market_date, day_gates in grouped.items():
                selected_gate, skip_reason = self._select_gate(rule_name=rule_name, day_gates=day_gates)
                if selected_gate is None:
                    skip_reasons[skip_reason or "selection_failed"] += 1
                    continue
                entry_index = close_index_by_date.get(market_date)
                if entry_index is None:
                    skip_reasons["no_session_close"] += 1
                    continue
                entry_time = timestamps[entry_index]
                entry_price = closes[entry_index]
                direction = str(selected_gate.get("direction") or "").upper()
                direction_sign = 1.0 if direction == "BUY" else -1.0
                current_date_pos = date_position[market_date]
                returns: dict[str, float | None] = {}
                directional_returns: dict[str, float | None] = {}
                for horizon in config.trading_day_horizons:
                    key = f"{horizon}d"
                    future_pos = current_date_pos + horizon
                    if future_pos >= len(ordered_dates):
                        returns[key] = None
                        directional_returns[key] = None
                        continue
                    future_date = ordered_dates[future_pos]
                    target_index = close_index_by_date[future_date]
                    value = (closes[target_index] / entry_price) - 1.0
                    returns[key] = value
                    directional_returns[key] = value * direction_sign
                buy_gate_count = sum(1 for gate in day_gates if str(gate.get("direction") or "").upper() == "BUY")
                sell_gate_count = sum(1 for gate in day_gates if str(gate.get("direction") or "").upper() == "SELL")
                observations.append(
                    CompressedDecisionObservation(
                        market_date=market_date,
                        rule_name=rule_name,
                        decision_time=entry_time,
                        representative_gate_time=self._gate_timestamp(selected_gate),
                        direction=direction,
                        source=str(selected_gate.get("source") or "unknown"),
                        regime=self._regime_label(selected_gate),
                        hazard=float(selected_gate.get("hazard") or 0.0),
                        trend_strength=self._trend_strength(selected_gate),
                        admitted_gate_count=len(day_gates),
                        buy_gate_count=buy_gate_count,
                        sell_gate_count=sell_gate_count,
                        returns=returns,
                        directional_returns=directional_returns,
                    )
                )
            results[rule_name] = {
                "decision_count": len(observations),
                "days_considered": len(grouped),
                "skip_reason_breakdown": dict(skip_reasons),
                "summary": self._summary_for_group(observations),
                "daily_decisions": [self._serialize_observation(item) for item in observations],
            }
        return results

    def _serialize_payload(
        self,
        *,
        config: GateCompressionStudyConfig,
        gate_count: int,
        eligible_gate_count: int,
        price_frame: pd.DataFrame,
        compressed: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        timestamps = [pd.Timestamp(value).to_pydatetime() for value in price_frame["timestamp"]]
        return {
            "symbol": config.symbol,
            "gate_path": str(config.gate_path),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "gate_count": gate_count,
            "eligible_gate_count": eligible_gate_count,
            "decisioning_policy": {
                "entry_price": "same_day_regular_session_close",
                "non_decisioning_fields": ["bundle_hits", "confidence"],
                "strongest_admitted_rule": "max_abs_trend_strength_then_min_hazard_then_earliest_gate",
                "majority_direction_rule": "last_gate_in_majority_direction",
                "majority_tie_policy": "skip_day",
            },
            "price_window": {
                "start": timestamps[0].isoformat(),
                "end": timestamps[-1].isoformat(),
                "bars": len(price_frame),
                "resolution_minutes": config.intraday_resolution_minutes,
            },
            "rules": compressed,
        }

    @staticmethod
    def _group_gates_by_market_date(
        *,
        gates: list[dict[str, object]],
        market_tz: ZoneInfo,
    ) -> dict[str, list[dict[str, object]]]:
        grouped: dict[str, list[dict[str, object]]] = {}
        sorted_gates = sorted(gates, key=GateOutcomeStudyRunner._gate_timestamp)
        for gate in sorted_gates:
            market_date = GateOutcomeStudyRunner._gate_timestamp(gate).astimezone(market_tz).date().isoformat()
            grouped.setdefault(market_date, []).append(gate)
        return grouped

    def _select_gate(
        self,
        *,
        rule_name: CompressionRule,
        day_gates: list[dict[str, object]],
    ) -> tuple[dict[str, object] | None, str | None]:
        if not day_gates:
            return None, "no_day_gates"
        if rule_name == "first_admitted":
            return day_gates[0], None
        if rule_name == "last_admitted":
            return day_gates[-1], None
        if rule_name == "strongest_admitted":
            ordered = sorted(
                day_gates,
                key=lambda gate: (
                    -abs(self._trend_strength(gate)),
                    float(gate.get("hazard") or 0.0),
                    self._gate_timestamp(gate),
                ),
            )
            return ordered[0], None
        buy_gates = [gate for gate in day_gates if str(gate.get("direction") or "").upper() == "BUY"]
        sell_gates = [gate for gate in day_gates if str(gate.get("direction") or "").upper() == "SELL"]
        if len(buy_gates) == len(sell_gates):
            return None, "tied_direction"
        if len(buy_gates) > len(sell_gates):
            return buy_gates[-1], None
        return sell_gates[-1], None

    @staticmethod
    def _trend_strength(record: dict[str, object]) -> float:
        regime = record.get("regime")
        if isinstance(regime, dict) and regime.get("trend_strength") not in (None, ""):
            return float(regime["trend_strength"])
        structure = record.get("structure")
        if isinstance(structure, dict) and structure.get("trend_strength") not in (None, ""):
            return float(structure["trend_strength"])
        return 0.0

    @staticmethod
    def _serialize_observation(item: CompressedDecisionObservation) -> dict[str, object]:
        return {
            "market_date": item.market_date,
            "rule_name": item.rule_name,
            "decision_time": item.decision_time.isoformat(),
            "representative_gate_time": item.representative_gate_time.isoformat(),
            "direction": item.direction,
            "source": item.source,
            "regime": item.regime,
            "hazard": item.hazard,
            "trend_strength": item.trend_strength,
            "admitted_gate_count": item.admitted_gate_count,
            "buy_gate_count": item.buy_gate_count,
            "sell_gate_count": item.sell_gate_count,
            "returns": item.returns,
            "directional_returns": item.directional_returns,
        }
