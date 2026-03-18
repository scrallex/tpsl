#!/usr/bin/env python3
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score, precision_score
from sklearn.inspection import permutation_importance

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

def main():
    instrument = "EUR_USD"
    gates_path = Path(f"output/market_data/{instrument}.gates.jsonl")
    bars_path = Path(f"output/market_data/{instrument}.jsonl")

    if not gates_path.exists() or not bars_path.exists():
        logger.error("Need gates and bars to run analysis.")
        return 1

    logger.info("1. Loading historical bars to compute future returns...")
    data = []
    with open(bars_path, "r") as f:
        for line in f:
            if not line.strip(): continue
            b = json.loads(line)
            data.append({
                "ts_ms": b["time"],
                "close": float(b.get("mid", {}).get("c", b.get("mid_c", 0.0)))
            })
    
    df_bars = pd.DataFrame(data)
    df_bars["ts_ms"] = pd.to_datetime(df_bars["ts_ms"], format='ISO8601').astype('int64') // 10**6
    df_bars.sort_values("ts_ms", inplace=True)
    df_bars.set_index("ts_ms", inplace=True)
    
    # Calculate 5-minute (60 S5 candles) forward return
    df_bars["future_close"] = df_bars["close"].shift(-60)
    df_bars["fwd_ret"] = (df_bars["future_close"] - df_bars["close"]) / df_bars["close"]
    
    logger.info("2. Loading generated gates...")
    gates = []
    with open(gates_path, "r") as f:
        for line in f:
            if not line.strip(): continue
            g = json.loads(line)
            actual_dir = g.get("direction", "FLAT")
            if actual_dir == "FLAT": continue
            
            # Reconstruct basic features
            hz = float(g.get("hazard", 0.0))
            coh = 0.0
            comps = g.get("components", {})
            if isinstance(comps, dict):
                coh = float(comps.get("coherence", 0.0))
                if hz == 0.0:
                    hz = float(comps.get("hazard", 0.0))
                    
            r = g.get("repetitions", 0)
            st = r * coh * np.exp(-hz)
            ent = float(comps.get("entropy", 0.0)) if isinstance(comps, dict) else 0.0
            
            dir_mult = 1 if actual_dir == "BUY" else -1
            # Mean Reversion uses opposite expected return direction
            # BUT the gate's `direction` field defaults to TREND (breakout direction). 
            # If we want to evaluate MR, we flip the multiplier. 
            # Or we can just train the ML model to predict direction!
            
            gates.append({
                "ts_ms": g["ts_ms"],
                "direction": dir_mult,
                "hazard": hz,
                "coherence": coh,
                "st": st,
                "entropy": ent,
                "reps": r
            })
            
    df_gates = pd.DataFrame(gates)
    
    logger.info("3. Joining gates with market outcomes...")
    df = pd.merge(df_gates, df_bars, on="ts_ms", how="inner")
    df.dropna(subset=["fwd_ret"], inplace=True)
    
    # Let's say we want to optimize for Mean Reversion. 
    # That means if gate says BUY (trend), MR expects price to go DOWN, so -fwd_ret
    # Wait, the `direction` in `export_optimal_trades.py` says:
    # "effective_dir = SELL if dir_str == BUY else BUY"
    
    # We will target Mean Reversion Profitable (meaning price mean-reverts against the gate direction)
    df["mr_pnl"] = -df["direction"] * df["fwd_ret"]
    
    # Target: is this a strictly profitable mean reversion bounce? (more than 0.5 bps)
    df["target"] = (df["mr_pnl"] > 0.00005).astype(int)
    
    logger.info(f"Total valid mean reversion opportunities: {len(df)}")
    logger.info(f"Positive samples: {df['target'].sum()} ({df['target'].mean():.2%})")
    
    # Static Threshold baseline
    # MR winner params from /tmp/final90sweep.log:
    static_pass = (df["hazard"] >= 0.88789) & \
                  (df["coherence"] >= 0.21525) & \
                  (df["entropy"] <= 0.94495)
                  
    baseline_acc = df.loc[static_pass, "target"].mean() if static_pass.sum() > 0 else 0
    logger.info(f"\n--- STATIC THRESHOLD BASELINE ---")
    logger.info(f"Trades Admitted: {static_pass.sum()} / {len(df)}")
    logger.info(f"Precision (Win Rate) of Admitted: {baseline_acc:.2%}")
    
    # Let's train a simple GBM
    features = ["hazard", "coherence", "entropy", "st", "reps"]
    X = df[features]
    y = df["target"]
    
    # Imbalance scale
    pos_weight = (len(y) - y.sum()) / max(y.sum(), 1)
    
    # Because scikit-learn HistGBM doesn't have scale_pos_weight natively in some older versions,
    # we can just pass sample_weight to fit.
    sample_weight = np.where(y == 1, pos_weight, 1.0)
    
    X_train, X_test, y_train, y_test, w_train, w_test = train_test_split(
        X, y, sample_weight, test_size=0.2, random_state=42, shuffle=False
    )
    
    clf = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.05, max_depth=5, l2_regularization=0.1)
    clf.fit(X_train, y_train, sample_weight=w_train)
    
    preds = clf.predict(X_test)
    probas = clf.predict_proba(X_test)[:, 1]
    
    logger.info("\n--- MACHINE LEARNING CLASSIFIER ---")
    logger.info(f"Predictions on valid holdout set: {len(y_test)} gates")
    print(classification_report(y_test, preds))
    
    # Evaluate a tighter threshold (confidence > 0.8)
    high_conf = probas > 0.8
    if high_conf.sum() > 0:
        high_conf_precision = y_test[high_conf].mean()
        logger.info(f"High Confidence Trades Admitted: {high_conf.sum()}")
        logger.info(f"High Confidence Precision: {high_conf_precision:.2%}")
    else:
        logger.info("No trades met 0.8 probability threshold.")

    # Show importance
    r = permutation_importance(clf, X_test, y_test, n_repeats=5, random_state=42)
    logger.info("\n--- FEATURE IMPORTANCES ---")
    for i in r.importances_mean.argsort()[::-1]:
        logger.info(f"{features[i]:<15}: {r.importances_mean[i]:.3f} +/- {r.importances_std[i]:.3f}")

if __name__ == "__main__":
    sys.exit(main())
