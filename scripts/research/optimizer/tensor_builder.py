"""Data loading and tensor initialization for GPU backtests."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch

from scripts.trading.candle_utils import to_epoch_ms

logger = logging.getLogger(__name__)
UTC = timezone.utc

SOURCE_MAP = {
    "trend_sniper": 1,
    "structural_extension": 1,
    "regime_manifold_codec": 3,
    "regime_manifold": 3,
    "squeeze_breakout": 2,
    "mean_reversion": 3,
    "manual": 0,
}

GpuDataTuple = Tuple[
    torch.Tensor,  # High
    torch.Tensor,  # Low
    torch.Tensor,  # Close
    torch.Tensor,  # GateHaz
    torch.Tensor,  # GateReps
    torch.Tensor,  # GateCoh
    torch.Tensor,  # GateStab
    torch.Tensor,  # GateEnt
    torch.Tensor,  # GateAct
    torch.Tensor,  # GateSrc
    torch.Tensor,  # GateSTPeak
    torch.Tensor,  # ContHaz
    List[datetime],
]


def _empty_gpu_result(device: torch.device) -> GpuDataTuple:
    f32 = lambda: torch.empty(0, dtype=torch.float32, device=device)
    b8 = lambda: torch.empty(0, dtype=torch.bool, device=device)
    return (
        f32(),
        f32(),
        f32(),
        f32(),
        f32(),
        f32(),
        f32(),
        f32(),
        torch.empty(0, dtype=torch.int8, device=device),
        torch.empty(0, dtype=torch.int8, device=device),
        b8(),
        f32(),
        [],
    )


def _gate_metric_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    structure = payload.get("structure")
    if isinstance(structure, dict) and structure:
        return structure
    components = payload.get("components")
    if isinstance(components, dict) and components:
        return components
    return {}


def _gate_float(payload: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _gate_hazard(payload: Dict[str, Any]) -> float:
    if payload.get("hazard") is not None:
        return _gate_float(payload, "hazard", 999.0)
    metric_source = _gate_metric_source(payload)
    try:
        return float(metric_source.get("hazard", 999.0))
    except (TypeError, ValueError):
        return 999.0


def _gate_repetitions(payload: Dict[str, Any]) -> float:
    if payload.get("repetitions") is not None:
        return _gate_float(payload, "repetitions", 1.0)
    return _gate_float(payload, "reps", 1.0)


def load_data_to_gpu(
    instrument: str,
    start: datetime,
    end: datetime,
    cache_path: Optional[Path],
    device: torch.device,
    target_signal_type: str = "trend_sniper",
) -> GpuDataTuple:
    """Loads OHLC + Gates (including Source) into CUDA tensors."""
    logger.info(f"Loading {instrument} data {start.date()} -> {end.date()} to VRAM...")

    try:
        from scripts.research.data_store import ManifoldDataStore

        store = ManifoldDataStore()
        payload = store.load_candles(instrument, start, end, "S5")
    except Exception as e:
        logger.error(f"Candle load error: {e}")
        return _empty_gpu_result(device)

    if not payload:
        return _empty_gpu_result(device)

    C = len(payload)
    np_opens = np.empty(C, dtype=np.float32)
    np_highs = np.empty(C, dtype=np.float32)
    np_lows = np.empty(C, dtype=np.float32)
    np_closes = np.empty(C, dtype=np.float32)
    parsed_times: List[datetime] = []
    fallback_dt = datetime.min.replace(tzinfo=UTC)

    for i, row in enumerate(payload):
        ts_raw = row.get("time", "")
        if ts_raw:
            try:
                parsed_times.append(
                    datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(
                        UTC
                    )
                )
            except Exception:
                parsed_times.append(fallback_dt)
        else:
            parsed_times.append(fallback_dt)

        mid = row.get("mid", {})
        np_opens[i] = float(mid.get("o", 0.0))
        np_highs[i] = float(mid.get("h", 0.0))
        np_lows[i] = float(mid.get("l", 0.0))
        np_closes[i] = float(mid.get("c", 0.0))

    opens = torch.from_numpy(np_opens).to(device)
    highs = torch.from_numpy(np_highs).to(device)
    lows = torch.from_numpy(np_lows).to(device)
    closes = torch.from_numpy(np_closes).to(device)

    # Gate Vectorization
    GateH = torch.full((C,), 999.0, dtype=torch.float32, device=device)
    GateR = torch.zeros(C, dtype=torch.float32, device=device)
    GateC = torch.zeros(C, dtype=torch.float32, device=device)
    GateS = torch.zeros(C, dtype=torch.float32, device=device)
    GateE = torch.full((C,), 999.0, dtype=torch.float32, device=device)
    GateAction = torch.zeros(C, dtype=torch.int8, device=device)
    GateSource = torch.zeros(C, dtype=torch.int8, device=device)

    market_data_dir = Path("output/market_data")
    try:
        from scripts.research.simulator.gate_cache import (
            ensure_historical_gate_cache,
            gate_cache_path_for,
        )

        cache_p = market_data_dir / f"{instrument}.jsonl"
        gate_cache = gate_cache_path_for(
            instrument, target_signal_type, base_dir=market_data_dir
        )
        ensure_historical_gate_cache(
            instrument,
            start,
            end,
            signal_type=target_signal_type,
            gate_cache_path=gate_cache,
            candle_cache_path=cache_p if cache_p.exists() else None,
        )
    except ImportError as e:
        logger.warning(f"Failed to materialize gate cache: {e}")
        gate_cache = market_data_dir / f"{instrument}.gates.jsonl"

    if gate_cache.exists():
        start_ms = to_epoch_ms(start)
        end_ms = to_epoch_ms(end)
        time_idx_map = {to_epoch_ms(ts): idx for idx, ts in enumerate(parsed_times)}

        gates_mapped = 0
        with open(gate_cache, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                g = json.loads(line)
                gate_ts_ms = int(g.get("ts_ms", 0))
                if start_ms <= gate_ts_ms <= end_ms:
                    if gate_ts_ms in time_idx_map:
                        idx = time_idx_map[gate_ts_ms]
                        comp = _gate_metric_source(g)
                        GateH[idx] = _gate_hazard(g)
                        GateR[idx] = _gate_repetitions(g)
                        GateC[idx] = _gate_float(comp, "coherence", 0.0)
                        GateS[idx] = _gate_float(comp, "stability", 0.0)
                        GateE[idx] = _gate_float(comp, "entropy", 999.0)

                        direction = str(g.get("direction", "FLAT")).upper()
                        GateAction[idx] = (
                            1
                            if direction == "BUY"
                            else (-1 if direction == "SELL" else 0)
                        )

                        src_str = str(g.get("source", "trend_sniper")).lower()
                        GateSource[idx] = SOURCE_MAP.get(src_str, 0)

                        gates_mapped += 1
        logger.info(f"Mapped {gates_mapped} gates into dense CUDA tensors.")

    GateST = GateR * GateC * torch.exp(-1.0 * GateH)
    GateSTPeak = torch.zeros_like(GateAction, dtype=torch.bool)

    gate_idxs = torch.where(GateAction != 0)[0]
    if len(gate_idxs) > 1:
        st_vals = GateST[gate_idxs]
        st_shifted = torch.cat(
            [torch.zeros(1, dtype=torch.float32, device=device), st_vals[:-1]]
        )
        is_peak = (st_vals < st_shifted) & (st_shifted > 0.0)
        GateSTPeak[gate_idxs] = is_peak

    # --- Continuous Hazard Loading for Hazard Exit ---
    ContH_np = np.full((C,), 999.0, dtype=np.float32)
    sig_cache = market_data_dir / f"{instrument}.signatures.jsonl"
    if sig_cache.exists():
        sigs = []
        with open(sig_cache, "r", encoding="utf-8") as sf:
            for sline in sf:
                if not sline.strip():
                    continue
                sg = json.loads(sline)
                if "time" in sg and "hazard" in sg:
                    try:
                        sts = int(
                            datetime.fromisoformat(
                                sg["time"].replace("Z", "+00:00")
                            ).timestamp()
                            * 1000
                        )
                        sigs.append({"ts_ms": sts, "hazard": float(sg["hazard"])})
                    except Exception:
                        pass
        sigs.sort(key=lambda x: x["ts_ms"])

        sig_idx = 0
        curr_h = 999.0
        for i, tval in enumerate(parsed_times):
            t_ms = to_epoch_ms(tval)
            while sig_idx < len(sigs) and sigs[sig_idx]["ts_ms"] <= t_ms:
                curr_h = sigs[sig_idx]["hazard"]
                sig_idx += 1
            ContH_np[i] = curr_h

    ContHaz = torch.from_numpy(ContH_np).to(device)

    valid_mask = opens > 0.0
    times = [parsed_times[i] for i, v in enumerate(valid_mask.cpu().numpy()) if v]
    return (
        highs[valid_mask],
        lows[valid_mask],
        closes[valid_mask],
        GateH[valid_mask],
        GateR[valid_mask],
        GateC[valid_mask],
        GateS[valid_mask],
        GateE[valid_mask],
        GateAction[valid_mask],
        GateSource[valid_mask],
        GateSTPeak[valid_mask],
        ContHaz[valid_mask],
        times,
    )


def build_regime_filters(
    closes: torch.Tensor, window_ticks: int
) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
    if window_ticks <= 1 or closes.numel() < window_ticks + 1:
        return None
    cumsum = torch.cumsum(closes, dim=0)
    zero = torch.zeros(1, device=closes.device, dtype=closes.dtype)
    window_sum = cumsum[window_ticks - 1 :] - torch.cat([zero, cumsum[:-window_ticks]])
    sma = torch.empty_like(closes)
    sma[: window_ticks - 1] = torch.nan
    sma[window_ticks - 1 :] = window_sum / window_ticks
    ready = ~torch.isnan(sma)
    long_ok = torch.ones_like(ready, dtype=torch.bool)
    short_ok = torch.ones_like(ready, dtype=torch.bool)
    long_ok[ready] = closes[ready] >= sma[ready]
    short_ok[ready] = closes[ready] <= sma[ready]
    return long_ok, short_ok


@dataclass
class SweepConfigTensors:
    arr_hold: torch.Tensor
    arr_reps: torch.Tensor
    arr_haz: torch.Tensor
    arr_coh: torch.Tensor
    arr_stab: torch.Tensor
    arr_ent: torch.Tensor
    arr_sl: torch.Tensor
    arr_tp: torch.Tensor
    arr_trail: torch.Tensor
    arr_haz_exit: torch.Tensor
    arr_be: torch.Tensor


@dataclass
class SweepStateTensors:
    in_trade: torch.Tensor
    be_activated: torch.Tensor
    trade_dir: torch.Tensor
    entry_price: torch.Tensor
    hold_timer: torch.Tensor
    peak_profit: torch.Tensor
    cooldown_timer: torch.Tensor


@dataclass
class SweepMetricsTensors:
    cum_pnl_bps: torch.Tensor
    comp_win: torch.Tensor
    comp_loss: torch.Tensor


def initialize_tensors(
    combo_dicts: List[Dict[str, Any]], device: torch.device, N: int, MAX_TRADES: int = 5
) -> Tuple[SweepConfigTensors, SweepStateTensors, SweepMetricsTensors]:
    def t(key, default, scale=1.0, dtype=torch.float32):
        return torch.tensor(
            [c.get(key, default) * scale for c in combo_dicts],
            dtype=dtype,
            device=device,
        )

    sl_inf = 99.0
    arr_sl = torch.tensor(
        [
            c.get("SL", sl_inf) if c.get("SL") is not None else sl_inf
            for c in combo_dicts
        ],
        dtype=torch.float32,
        device=device,
    )
    arr_tp = torch.tensor(
        [
            c.get("TP", sl_inf) if c.get("TP") is not None else sl_inf
            for c in combo_dicts
        ],
        dtype=torch.float32,
        device=device,
    )
    arr_trail = torch.tensor(
        [
            c.get("Trail", sl_inf) if c.get("Trail") is not None else sl_inf
            for c in combo_dicts
        ],
        dtype=torch.float32,
        device=device,
    )
    arr_haz_exit = torch.tensor(
        [
            c.get("HazEx", -1.0) if c.get("HazEx") is not None else -1.0
            for c in combo_dicts
        ],
        dtype=torch.float32,
        device=device,
    )
    arr_be = torch.tensor(
        [
            c.get("BE", sl_inf) if c.get("BE") is not None else sl_inf
            for c in combo_dicts
        ],
        dtype=torch.float32,
        device=device,
    )

    cfg = SweepConfigTensors(
        t("Hold", 30, 12, torch.int32),  # S5 ticks
        t("Reps", 1, 1.0, torch.float32),
        t("Haz", 1.0, 1.0, torch.float32),
        t("Coh", 0.0, 1.0, torch.float32),
        t("Stab", 0.0, 1.0, torch.float32),
        t("Ent", 999.0, 1.0, torch.float32),
        arr_sl,
        arr_tp,
        arr_trail,
        arr_haz_exit,
        arr_be,
    )

    state = SweepStateTensors(
        torch.zeros((N, MAX_TRADES), dtype=torch.bool, device=device),
        torch.zeros((N, MAX_TRADES), dtype=torch.bool, device=device),
        torch.zeros((N, MAX_TRADES), dtype=torch.int8, device=device),
        torch.zeros((N, MAX_TRADES), dtype=torch.float32, device=device),
        torch.zeros((N, MAX_TRADES), dtype=torch.int32, device=device),
        torch.zeros((N, MAX_TRADES), dtype=torch.float32, device=device),
        torch.zeros(N, dtype=torch.int32, device=device),
    )
    metrics = SweepMetricsTensors(
        torch.zeros(N, dtype=torch.float32, device=device),
        torch.zeros(N, dtype=torch.int32, device=device),
        torch.zeros(N, dtype=torch.int32, device=device),
    )
    return cfg, state, metrics
