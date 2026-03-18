#!/usr/bin/env python3
"""Train HistGBM classification models on structural market data.

This trains instrument-specific tree models to predict if a structural gate
will realize a profitable mean-reverting outcome (target = 1) based on both
intrinsic structural metrics and temporal context.
"""
import argparse
import glob
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, precision_score
from sklearn.inspection import permutation_importance

import pickle

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("MLTraner")

def train_instrument(instrument: str, data_dir: Path, model_dir: Path):
    parquet_file = data_dir / f"{instrument}_features.parquet"
    if not parquet_file.exists():
        logger.error(f"Missing parquet dataset for {instrument} at {parquet_file}")
        return

    logger.info(f"=== Training {instrument} ===")
    df = pd.read_parquet(parquet_file)
    
    # We want to use the features we proved work.
    features = [
        "lambda_hazard",
        "coherence",
        "entropy",
        "stability",
        "reps",
        "st",
        "st_peak",
        "rsi",
        "volatility",
        "dist_ema60"
    ]
    
    # Validate columns
    missing = [f for f in features if f not in df.columns]
    if missing:
        logger.error(f"Dataset for {instrument} is missing features: {missing}")
        return

    X = df[features]
    y = df["target"]
    
    # Stratified chronological split: we shouldn't purely shuffle time series 
    # but since this is an isolated proof of concept classifier taking independent gates
    # we can use standard holdout, but preferably strict chronological.
    split_idx = int(len(df) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    
    pos = y_train.sum()
    if pos < 10:
        logger.error(f"Not enough positive samples for {instrument} ({pos} found).")
        return

    ratio = (len(y_train) - pos) / max(pos, 1)
    logger.info(f"[{instrument}] Imbalance Ratio: {ratio:.1f}")

    # Disable sample weighting to allow true probability calibration.
    # Upping max_depth to allow more expressive intersections.
    sample_weight = None
    
    model = HistGradientBoostingClassifier(
        max_iter=250,
        learning_rate=0.03,
        max_depth=8,
        l2_regularization=0.01,
        early_stopping=True,
        validation_fraction=0.1,
        random_state=42
    )
    
    model.fit(X_train, y_train, sample_weight=sample_weight)
    
    # Evaluate
    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]
    
    precision = precision_score(y_test, preds, zero_division=0)
    logger.info(f"[{instrument}] Baseline Precision (>0.5 prob): {precision:.2%}")
    
    # Let's see precision at a higher confidence threshold that we would actually trade
    # Because ML gates shouldn't just be >0.5, they should be highly confident structural setups.
    conf_mask = probs > 0.80
    high_conf_trades = conf_mask.sum()
    if high_conf_trades > 0:
        high_conf_prec = y_test[conf_mask].mean()
        logger.info(f"[{instrument}] High Conf (>0.8) Precision: {high_conf_prec:.2%} (Trades: {high_conf_trades})")
    else:
        logger.info(f"[{instrument}] No trades met the >0.8 threshold.")
        
    # Feature Importance
    r = permutation_importance(model, X_test, y_test, n_repeats=5, random_state=42)
    top_features = []
    for i in r.importances_mean.argsort()[::-1][:3]:
        top_features.append(f"{features[i]} ({r.importances_mean[i]:.3f})")
    logger.info(f"[{instrument}] Top Features: {', '.join(top_features)}")

    # Save model
    model_path = model_dir / f"{instrument}_histgbm.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    logger.info(f"[{instrument}] Saved model to {model_path}\n")

def main():
    parser = argparse.ArgumentParser(description="Train HistGBM classification models on structural market data.")
    parser.add_argument("--instruments", nargs="+", help="Instruments to process (e.g. EUR_USD). Leave blank for all found.")
    args = parser.parse_args()
    
    data_dir = Path("output/ml_data")
    if not data_dir.exists():
        logger.error(f"Data directory {data_dir} does not exist. Run build_features.py first.")
        return 1
        
    model_dir = Path("output/models")
    model_dir.mkdir(parents=True, exist_ok=True)
    
    if args.instruments:
        for inst in args.instruments:
            train_instrument(inst, data_dir, model_dir)
    else:
        # Auto-discover from Parquet files
        parquet_files = glob.glob(str(data_dir / "*_features.parquet"))
        for pf in parquet_files:
            inst = Path(pf).name.replace("_features.parquet", "")
            train_instrument(inst, data_dir, model_dir)
            
    return 0

if __name__ == "__main__":
    sys.exit(main())
