#!/usr/bin/env python3
"""Build ML datasets by merging S5 candles with generated structural gates.

This implements the feature engineering pipeline from V3, specifically aiming to
provide temporal awareness (RSI, Volatility, EMA distance) to the raw manifold
gate metrics (Hazard, Coherence, Entropy) to improve Win Rate natively.
"""
import argparse
import glob
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("FeatureBuilder")

def add_technicals(df: pd.DataFrame) -> pd.DataFrame:
    """Add standard technical features natively on the S5 timeframe.
    As defined by the Gold Standard model tuning: RSI(14), Volatility(20), and EMA(60).
    """
    df["close"] = df["close"].astype(float)

    # RSI (14)
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # Volatility (20-period log return std)
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["volatility"] = df["log_ret"].rolling(20).std()

    # Trend Distance from 5-minute EMA (60 S5 periods)
    ema60 = df["close"].ewm(span=60).mean()
    df["dist_ema60"] = (df["close"] - ema60) / ema60

    df.fillna(0, inplace=True)
    return df

def build_instrument(instrument: str, output_dir: Path):
    gates_path = Path(f"output/market_data/{instrument}.gates.jsonl")
    bars_path = Path(f"output/market_data/{instrument}.jsonl")

    if not gates_path.exists():
        logger.warning(f"No gates found for {instrument}. Skipping.")
        return
    if not bars_path.exists():
        logger.warning(f"No bars found for {instrument}. Skipping.")
        return

    logger.info(f"[{instrument}] Loading S5 canonical bars...")
    data = []
    with open(bars_path, "r") as f:
        for line in f:
            if not line.strip(): continue
            try:
                b = json.loads(line)
                data.append({
                    "ts_ms": b["time"],
                    "close": float(b.get("mid", {}).get("c", b.get("mid_c", 0.0)))
                })
            except Exception as e:
                continue
                
    if not data:
        logger.error(f"Failed to load any valid bars for {instrument}")
        return
        
    df_bars = pd.DataFrame(data)
    # Parse ISO8601 into fast integer timestamps milliseconds
    df_bars["ts_ms"] = pd.to_datetime(df_bars["ts_ms"], format='ISO8601').astype('int64') // 10**6
    df_bars.sort_values("ts_ms", inplace=True)
    
    # Calculate tech indicators before merging, because they need continuous S5 series
    df_bars = add_technicals(df_bars)
    
    # Pre-calculate Future Return targets (22.5 mins = 270 S5 candles)
    # Target shift -270 gets the close 22.5 minutes ahead to match standard hold times.
    df_bars["future_close"] = df_bars["close"].shift(-270)
    df_bars["fwd_ret"] = (df_bars["future_close"] - df_bars["close"]) / df_bars["close"]
    
    # Set index for faster merge
    df_bars.set_index("ts_ms", inplace=True)

    logger.info(f"[{instrument}] Loading generated structural gates...")
    gates = []
    with open(gates_path, "r") as f:
        for line in f:
            if not line.strip(): continue
            try:
                g = json.loads(line)
                actual_dir = g.get("direction", "FLAT")
                
                hz = float(g.get("hazard", 0.0))
                coh = 0.0
                comps = g.get("components", {})
                ent = 0.0
                stab = 0.0
                
                if isinstance(comps, dict):
                    coh = float(comps.get("coherence", 0.0))
                    ent = float(comps.get("entropy", 0.0))
                    stab = float(comps.get("stability", 0.0))
                    if hz == 0.0:
                        hz = float(comps.get("hazard", 0.0))
                else:
                    st_metric = float(g.get("structural_tension", 0.0))
                    hz = float(g.get("hazard", 0.0))
                        
                r = g.get("repetitions", 0)
                st = r * coh * np.exp(-1.0 * hz)
                
                dir_mult = 1 if actual_dir == "BUY" else (-1 if actual_dir == "SELL" else 0)
                
                gates.append({
                    "ts_ms": g["ts_ms"],
                    "direction_str": actual_dir,
                    "direction": dir_mult,
                    "lambda_hazard": hz,  # Map to V3 nomenclature
                    "coherence": coh,
                    "entropy": ent,
                    "stability": stab,
                    "reps": r,
                    "st": st
                })
            except Exception as e:
                continue

    if not gates:
        logger.error(f"Failed to load any valid gates for {instrument}")
        return
        
    df_gates = pd.DataFrame(gates)
    
    # Calculate st_peak over the continuous gate series
    df_gates["st_prev"] = df_gates["st"].shift(1)
    df_gates["st_peak"] = ((df_gates["st"] < df_gates["st_prev"]) & (df_gates["st_prev"] > df_gates["st"].rolling(30).mean())).astype(int)
    
    # Filter out FLAT directions to ensure merge is target-focused
    df_gates = df_gates[df_gates["direction_str"] != "FLAT"].drop(columns=["direction_str", "st_prev"])
    
    logger.info(f"[{instrument}] Joining gates and saving Parquet...")
    # Inner join using index
    df = pd.merge(df_gates, df_bars, on="ts_ms", how="inner")
    df.dropna(subset=["fwd_ret"], inplace=True)
    
    # Establish Mean Reversion PnL target: if Gate is BUY, MR targets a DOWN move
    df["mr_pnl"] = -df["direction"] * df["fwd_ret"]
    df["target"] = (df["mr_pnl"] > 0.001).astype(int) # 10 bps positive bounce

    out_file = output_dir / f"{instrument}_features.parquet"
    df.to_parquet(out_file, engine="pyarrow")
    pos_samples = df['target'].sum()
    logger.info(f"[{instrument}] Wrote {len(df)} samples ({pos_samples} positives) -> {out_file.name}")

def main():
    parser = argparse.ArgumentParser(description="Merge S5 market data into structural gates to generate ML datasets.")
    parser.add_argument("--instruments", nargs="+", help="Instruments to process (e.g. EUR_USD). Leave blank for all found.")
    
    args = parser.parse_args()
    
    out_dir = Path("output/ml_data")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    if args.instruments:
        for inst in args.instruments:
            build_instrument(inst, out_dir)
    else:
        # Auto-discover from gates.jsonl
        gate_files = glob.glob("output/market_data/*.gates.jsonl")
        for gf in gate_files:
            inst = Path(gf).name.replace(".gates.jsonl", "")
            build_instrument(inst, out_dir)

if __name__ == "__main__":
    sys.exit(main())
