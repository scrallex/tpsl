"""GPU-sweep-faithful trade replay with live-aligned sizing."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from scripts.research.simulator.data_adapter import BacktestDataAdapter
from scripts.research.simulator.metrics_calculator import compute_tpsl_metrics
from scripts.research.simulator.models import (
    OHLCCandle,
    TPSLSimulationParams,
    TPSLSimulationResult,
    TPSLTradeRecord,
)
from scripts.research.simulator.pricing_utils import calculate_commission, convert_to_usd
from scripts.trading.candle_utils import to_epoch_ms
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.tpsl import TPSLConfig


@dataclass
class ParitySlot:
    direction: int
    units: int
    entry_price: float
    entry_time: datetime
    hold_ticks: int = 0
    peak_profit: float = 0.0
    breakeven_activated: bool = False
    entry_commission: float = 0.0
    max_favorable: float = 0.0
    max_adverse: float = 0.0


def _gate_direction(payload: Dict[str, Any]) -> int:
    direction = str(payload.get("direction", "")).upper()
    if direction == "BUY":
        return 1
    if direction == "SELL":
        return -1
    return 0


def _gate_repetitions(payload: Dict[str, Any]) -> float:
    if payload.get("repetitions") is not None:
        try:
            return float(payload.get("repetitions", 1.0))
        except (TypeError, ValueError):
            return 1.0
    try:
        return float(payload.get("reps", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _gate_component(payload: Dict[str, Any], key: str, default: float) -> float:
    components = payload.get("structure") or payload.get("components") or {}
    try:
        return float(components.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _gate_hazard(payload: Dict[str, Any]) -> float:
    if payload.get("hazard") is None:
        components = payload.get("structure") or payload.get("components") or {}
        try:
            return float(components.get("hazard", 999.0))
        except (TypeError, ValueError):
            return 999.0
    try:
        return float(payload.get("hazard", 999.0))
    except (TypeError, ValueError):
        return 999.0


def _structural_tension(payload: Dict[str, Any]) -> float:
    reps = _gate_repetitions(payload)
    coh = _gate_component(payload, "coherence", 0.0)
    haz = _gate_hazard(payload)
    return reps * coh * math.exp(-1.0 * haz)


def collapse_gates_for_gpu_parity(
    gates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Collapse gates to tensor semantics: last gate wins per timestamp."""

    latest_by_ts: Dict[int, Dict[str, Any]] = {}
    for raw in sorted(gates, key=lambda item: int(item.get("ts_ms", 0))):
        gate = dict(raw)
        ts_ms = int(gate.get("ts_ms", 0))
        gate["_gpu_action"] = _gate_direction(gate)
        gate["_gpu_hazard"] = _gate_hazard(gate)
        gate["_gpu_reps"] = _gate_repetitions(gate)
        gate["_gpu_coh"] = _gate_component(gate, "coherence", 0.0)
        gate["_gpu_stab"] = _gate_component(gate, "stability", 0.0)
        gate["_gpu_ent"] = _gate_component(gate, "entropy", 999.0)
        gate["_gpu_st"] = _structural_tension(gate)
        gate["_gpu_st_peak"] = False
        latest_by_ts[ts_ms] = gate

    collapsed = sorted(latest_by_ts.values(), key=lambda item: int(item.get("ts_ms", 0)))

    prev_st: Optional[float] = None
    for gate in collapsed:
        if gate["_gpu_action"] == 0:
            continue
        curr_st = float(gate["_gpu_st"])
        gate["_gpu_st_peak"] = bool(prev_st is not None and prev_st > 0.0 and curr_st < prev_st)
        prev_st = curr_st

    return collapsed


def _load_continuous_hazard(
    cache_path: Optional[Path],
    start: datetime,
    end: datetime,
) -> Dict[int, float]:
    if cache_path is None:
        return {}
    sig_cache = cache_path.with_suffix(".signatures.jsonl")
    if not sig_cache.exists():
        return {}

    start_ms = to_epoch_ms(start)
    end_ms = to_epoch_ms(end)
    signature_points: List[Tuple[int, float]] = []
    with sig_cache.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if "time" not in payload or "hazard" not in payload:
                continue
            try:
                ts_ms = int(
                    datetime.fromisoformat(payload["time"].replace("Z", "+00:00")).timestamp() * 1000
                )
                if ts_ms <= end_ms:
                    signature_points.append((ts_ms, float(payload["hazard"])))
            except (TypeError, ValueError):
                continue

    signature_points.sort(key=lambda item: item[0])
    hazard_by_ts: Dict[int, float] = {}
    current_hazard = 999.0
    idx = 0
    for ts_ms in range(start_ms, end_ms + 1, 5000):
        while idx < len(signature_points) and signature_points[idx][0] <= ts_ms:
            current_hazard = signature_points[idx][1]
            idx += 1
        hazard_by_ts[ts_ms] = current_hazard
    return hazard_by_ts


def _hold_ticks(params: TPSLSimulationParams) -> int:
    return max(0, int(float(params.hold_minutes or 0) * 12))


def _target_units(
    risk_sizer: RiskSizer,
    nav: float,
    instrument: str,
    exposure_scale: float,
    price: float,
) -> int:
    caps = risk_sizer.compute_caps(nav)
    units, _, _ = risk_sizer.target_units(
        instrument,
        target_exposure=caps.per_position_cap,
        exposure_scale=exposure_scale,
        price_data={"mid": price},
    )
    return abs(int(units))


def _slot_unrealized_usd(instrument: str, slot: ParitySlot, price: float) -> float:
    raw = (float(price) - slot.entry_price) * slot.units * slot.direction
    return convert_to_usd(instrument, raw, price)


def _update_slot_excursions(
    instrument: str,
    slot: ParitySlot,
    high: float,
    low: float,
) -> None:
    favorable_price = high if slot.direction > 0 else low
    adverse_price = low if slot.direction > 0 else high

    favorable_raw = (float(favorable_price) - slot.entry_price) * slot.units * slot.direction
    adverse_raw = (float(adverse_price) - slot.entry_price) * slot.units * slot.direction

    favorable_usd = convert_to_usd(instrument, favorable_raw, favorable_price)
    adverse_usd = convert_to_usd(instrument, adverse_raw, adverse_price)

    slot.max_favorable = max(slot.max_favorable, favorable_usd)
    slot.max_adverse = min(slot.max_adverse, adverse_usd)


def replay_gpu_parity(
    *,
    instrument: str,
    candles: Sequence[OHLCCandle],
    gates: Sequence[Dict[str, Any]],
    params: TPSLSimulationParams,
    nav: float,
    nav_risk_pct: float,
    per_position_pct_cap: float,
    cost_bps: float,
) -> TPSLSimulationResult:
    """Replay one parameter set using the same event logic as the GPU sweep."""

    slots: List[Optional[ParitySlot]] = [None] * 5
    realized_usd = 0.0
    equity_curve: List[Tuple[datetime, float]] = []
    trades: List[TPSLTradeRecord] = []
    cooldown_ticks = 0
    risk_sizer = RiskSizer(
        nav_risk_pct=nav_risk_pct,
        per_position_pct_cap=per_position_pct_cap,
        alloc_top_k=1,
    )

    gate_by_ts = {int(g["ts_ms"]): g for g in collapse_gates_for_gpu_parity(gates)}
    hazard_by_ts = _load_continuous_hazard(Path(f"output/market_data/{instrument}.jsonl"), candles[0].time, candles[-1].time) if candles else {}

    hold_limit = _hold_ticks(params)
    use_mean_reversion = (params.signal_type or "").lower() == "mean_reversion"
    hazard_threshold = params.hazard_min if use_mean_reversion else params.hazard_override

    for candle in candles:
        ts_ms = to_epoch_ms(candle.time)

        if cooldown_ticks > 0:
            cooldown_ticks -= 1

        for idx, slot in enumerate(slots):
            if slot is None:
                continue

            slot.hold_ticks += 1
            pct_change_h = (candle.high - slot.entry_price) / slot.entry_price
            pct_change_l = (candle.low - slot.entry_price) / slot.entry_price
            drawdown = pct_change_l if slot.direction == 1 else -pct_change_h
            profit = pct_change_h if slot.direction == 1 else -pct_change_l
            slot.peak_profit = max(slot.peak_profit, profit)
            _update_slot_excursions(instrument, slot, candle.high, candle.low)

            hit_sl = params.stop_loss_pct is not None and drawdown <= -float(params.stop_loss_pct)
            hit_tp = params.take_profit_pct is not None and profit >= float(params.take_profit_pct)

            if params.breakeven_trigger_pct is not None and not slot.breakeven_activated:
                if profit >= abs(float(params.breakeven_trigger_pct)):
                    slot.breakeven_activated = True
            hit_be = slot.breakeven_activated and drawdown <= 0.0

            hit_haz_exit = False
            if (
                params.hazard_exit_threshold is not None
                and float(params.hazard_exit_threshold) < 0.999
                and slot.hold_ticks > 0
            ):
                hit_haz_exit = hazard_by_ts.get(ts_ms, 999.0) <= float(params.hazard_exit_threshold)

            hit_trail = False
            if params.trailing_stop_pct is not None:
                hit_trail = slot.peak_profit > 0.0 and (
                    (slot.peak_profit - drawdown) >= float(params.trailing_stop_pct)
                )

            hit_time = hold_limit > 0 and slot.hold_ticks >= hold_limit
            if not any((hit_sl, hit_tp, hit_be, hit_haz_exit, hit_trail, hit_time)):
                continue

            exit_price = candle.close
            exit_reason = "hold_expiry"

            if hit_sl and params.stop_loss_pct is not None:
                exit_price = slot.entry_price * (
                    1.0 - (float(params.stop_loss_pct) * slot.direction)
                )
                exit_reason = "stop_loss_hit"
            if hit_tp and params.take_profit_pct is not None:
                exit_price = slot.entry_price * (
                    1.0 + (float(params.take_profit_pct) * slot.direction)
                )
                exit_reason = "take_profit_hit"
            if hit_trail and params.trailing_stop_pct is not None:
                exit_price = slot.entry_price * (
                    1.0 + ((slot.peak_profit - float(params.trailing_stop_pct)) * slot.direction)
                )
                exit_reason = "trailing_stop_hit"
            if hit_be:
                exit_price = slot.entry_price
                exit_reason = "breakeven_stop_hit"
            if hit_haz_exit:
                exit_reason = "hazard_exit"

            exit_commission = calculate_commission(
                instrument,
                exit_price,
                slot.units,
                cost_bps / 2.0,
            )
            raw_pnl = (float(exit_price) - slot.entry_price) * slot.units * slot.direction
            gross_usd = convert_to_usd(instrument, raw_pnl, exit_price)
            pnl_usd = gross_usd - slot.entry_commission - exit_commission
            realized_usd += gross_usd - exit_commission

            trades.append(
                TPSLTradeRecord(
                    instrument=instrument.upper(),
                    entry_time=slot.entry_time,
                    exit_time=candle.time,
                    direction="LONG" if slot.direction > 0 else "SHORT",
                    units=slot.units,
                    entry_price=slot.entry_price,
                    exit_price=float(exit_price),
                    pnl=pnl_usd,
                    commission=slot.entry_commission + exit_commission,
                    mae=abs(slot.max_adverse),
                    mfe=max(0.0, slot.max_favorable),
                    exit_reason=exit_reason,
                    tpsl_trigger_price=float(exit_price),
                    is_bundle_trade=False,
                )
            )
            slots[idx] = None

        gate = gate_by_ts.get(ts_ms)
        if gate is not None:
            actual_dir = int(gate["_gpu_action"])
            if use_mean_reversion:
                actual_dir *= -1

            gate_valid = actual_dir != 0
            if gate_valid and hazard_threshold is not None:
                if use_mean_reversion:
                    gate_valid = bool(gate["_gpu_hazard"] >= float(hazard_threshold))
                else:
                    gate_valid = bool(gate["_gpu_hazard"] <= float(hazard_threshold))
            if gate_valid:
                gate_valid = bool(gate["_gpu_reps"] >= max(1, params.min_repetitions))
            if gate_valid and params.coherence_threshold is not None:
                gate_valid = bool(gate["_gpu_coh"] >= float(params.coherence_threshold))
            if gate_valid and params.stability_threshold is not None:
                gate_valid = bool(gate["_gpu_stab"] >= float(params.stability_threshold))
            if gate_valid and params.entropy_threshold is not None:
                gate_valid = bool(gate["_gpu_ent"] <= float(params.entropy_threshold))
            if gate_valid and use_mean_reversion:
                gate_valid = bool(gate["_gpu_st_peak"])

            signed_open_units = sum(slot.direction for slot in slots if slot is not None)
            current_side = 1 if signed_open_units > 0 else (-1 if signed_open_units < 0 else 0)
            side_conflict = current_side != 0 and current_side != actual_dir
            first_open_idx = next((idx for idx, slot in enumerate(slots) if slot is None), None)

            if (
                gate_valid
                and cooldown_ticks == 0
                and first_open_idx is not None
                and not side_conflict
            ):
                units = _target_units(
                    risk_sizer,
                    nav,
                    instrument,
                    params.exposure_scale,
                    candle.close,
                )
                if units > 0:
                    entry_commission = calculate_commission(
                        instrument,
                        candle.close,
                        units,
                        cost_bps / 2.0,
                    )
                    realized_usd -= entry_commission
                    slots[first_open_idx] = ParitySlot(
                        direction=actual_dir,
                        units=units,
                        entry_price=float(candle.close),
                        entry_time=candle.time,
                        entry_commission=entry_commission,
                    )
                    cooldown_ticks = 12

        unrealized = sum(
            _slot_unrealized_usd(instrument, slot, candle.close)
            for slot in slots
            if slot is not None
        )
        equity_curve.append((candle.time, nav + realized_usd + unrealized))

    if equity_curve:
        # Mirror GPU score semantics: open positions at the end do not realize PnL.
        equity_curve.append((equity_curve[-1][0], nav + realized_usd))

    metrics = compute_tpsl_metrics(equity_curve, trades, nav)
    return TPSLSimulationResult(
        instrument=instrument.upper(),
        params=params,
        tpsl_config=params.to_tpsl_config(),
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
        source="gpu_parity",
    )


def run_gpu_parity_replay(
    *,
    instrument: str,
    start: datetime,
    end: datetime,
    params: TPSLSimulationParams,
    nav: float,
    nav_risk_pct: float,
    per_position_pct_cap: float,
    cost_bps: float,
    granularity: str = "S5",
) -> Optional[TPSLSimulationResult]:
    adapter = BacktestDataAdapter(redis_url=None, granularity=granularity)
    candles = adapter.load_ohlc_candles(instrument, start, end)
    if not candles:
        return None
    gates = adapter.load_gate_events(instrument, start, end)
    if not gates:
        return None
    return replay_gpu_parity(
        instrument=instrument,
        candles=candles,
        gates=gates,
        params=params,
        nav=nav,
        nav_risk_pct=nav_risk_pct,
        per_position_pct_cap=per_position_pct_cap,
        cost_bps=cost_bps,
    )
