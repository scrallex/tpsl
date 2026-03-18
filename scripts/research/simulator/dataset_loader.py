"""Shared data loader for loading and pre-processing merged candle and signature data in memory."""

import json
import logging
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional
import pandas as pd

from scripts.trading.candle_utils import to_epoch_ms
from scripts.research.data_store import parse
from scripts.research.simulator.signal_matching import parse_signature, compute_hazard

logger = logging.getLogger(__name__)


def load_dataset_in_memory(
    instrument: str, granularity: str = "S5"
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[int, Dict[str, Any]]]]:
    if granularity == "M1":
        candle_p = Path(f"output/market_data/{instrument}_Synthetic_M1.jsonl")
        sig_p = Path(f"output/market_data/{instrument}_Synthetic_M1.signatures.jsonl")
        window = 1440
        pip_multi = 100.0 if "JPY" in instrument else 10000.0
        roll_window_15m = 180
        atr_window = 168
        v_tick_window = 1440
        ema_span = 2880
        drift_window = 768
    else:
        candle_p = Path(f"output/market_data/{instrument}.jsonl")
        sig_p = Path(f"output/market_data/{instrument}.signatures.jsonl")
        window = 1440
        pip_multi = 100.0 if "JPY" in instrument else 10000.0
        roll_window_15m = 180
        atr_window = 14
        v_tick_window = 1440
        ema_span = 240
        drift_window = 64

    if not candle_p.exists() or not sig_p.exists():
        logger.error(f"Missing chronological data for {instrument} ({granularity})")
        return None, None

    logger.info(f"[{instrument}] Pre-loading {granularity} signatures to RAM...")
    sig_list = []
    with sig_p.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                s = json.loads(line)
                sig_ms = (
                    int(s.get("end_ms", 0))
                    if granularity == "M1"
                    else to_epoch_ms(parse(s["time"]))
                )
                curr_c, curr_s, curr_e = parse_signature(s["signature"])
                haz = compute_hazard(curr_c, curr_s, curr_e)
                sig_list.append({"ts_ms": sig_ms, "c": curr_c, "e": curr_e, "h": haz})

    # Step 1: Vectorize Normalization
    df_sig = pd.DataFrame(sig_list)
    df_sig = df_sig.sort_values("ts_ms")

    logger.info(f"[{instrument}] Computing Rolling Percentiles...")
    df_sig["c_pct"] = df_sig["c"].rolling(window).rank(pct=True)
    df_sig["e_pct"] = df_sig["e"].rolling(window).rank(pct=True)
    df_sig["h_pct"] = df_sig["h"].rolling(window).rank(pct=True)
    df_sig["c_drift"] = df_sig["c_pct"].rolling(drift_window).mean()

    df_sig.fillna(0.5, inplace=True)

    logger.info(f"[{instrument}] Pre-computing native Pandas Vectors...")
    candle_list = []
    with candle_p.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            c_dict = json.loads(line)
            ts_ms = to_epoch_ms(parse(c_dict["time"]))
            mid = c_dict["mid"]
            candle_list.append(
                {
                    "ts_ms": ts_ms,
                    "h": float(mid["h"]),
                    "l": float(mid["l"]),
                    "c": float(mid["c"]),
                    "o": float(mid["o"]),
                    "v": int(c_dict.get("volume", 0)),
                }
            )

    df_c = pd.DataFrame(candle_list)
    df_c = df_c.sort_values("ts_ms")

    df_c["roll_15m_high"] = df_c["h"].rolling(roll_window_15m).max()
    df_c["roll_15m_low"] = df_c["l"].rolling(roll_window_15m).min()
    df_c["session_range_pips"] = (
        df_c["roll_15m_high"] - df_c["roll_15m_low"]
    ) * pip_multi

    df_c["candle_range_pips"] = (df_c["h"] - df_c["l"]) * pip_multi
    df_c["tick_velocity"] = df_c["candle_range_pips"] / (df_c["v"] + 1)
    df_c["v_tick_pct"] = df_c["tick_velocity"].rolling(v_tick_window).rank(pct=True)

    df_c["ema_240"] = df_c["c"].ewm(span=ema_span, adjust=False).mean()

    df_c["tr"] = df_c["h"] - df_c["l"]
    df_c["atr_14"] = df_c["tr"].rolling(atr_window).mean() * pip_multi

    df_c.fillna(0.0, inplace=True)

    df_c.drop_duplicates(subset=["ts_ms"], keep="last", inplace=True)
    df_sig.drop_duplicates(subset=["ts_ms"], keep="last", inplace=True)

    df_sig = pd.merge(
        df_sig,
        df_c[["ts_ms", "session_range_pips", "v_tick_pct", "ema_240", "atr_14", "c"]],
        on="ts_ms",
        how="inner",
    )

    sig_dict = df_sig.set_index("ts_ms").to_dict("index")
    return candle_list, sig_dict
