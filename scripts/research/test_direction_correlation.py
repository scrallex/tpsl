#!/usr/bin/env python3
"""
Directional Correlation Benchmark
Tests whether structural vectors (coherence, entropy, hazard) or recent price action
can predict the Long/Short outcome of 'mean_revert' regimes.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.stats import pearsonr, spearmanr


def main():
    instrument = "USD_JPY"
    market_path = Path(f"output/market_data/{instrument}.jsonl")
    gates_path = Path(f"output/market_data/{instrument}.gates.jsonl")

    if not market_path.exists() or not gates_path.exists():
        print(f"Data not found for {instrument}. Need .jsonl and .gates.jsonl")
        return

    print("Loading candles...")
    candles = []
    with open(market_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                c = json.loads(line)
                ts = c.get("time")
                mid = c.get("mid", {})
                closes = float(mid.get("c", 0.0))
                candles.append({"time": ts, "close": closes})
            except:
                pass

    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["time"].str.replace("Z", "+00:00"))
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)

    print("Loading gates...")
    gates = []
    with open(gates_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            g = json.loads(line)
            # Focus on the 'mean_revert' regimes that the user mentioned
            if g.get("regime") == "mean_revert" and g.get("admit") == 1:
                gates.append(
                    {
                        "time": pd.to_datetime(g["ts_ms"], unit="ms", utc=True),
                        "coherence": g.get("components", {}).get("coherence", 0.0),
                        "stability": g.get("components", {}).get("stability", 0.0),
                        "entropy": g.get("components", {}).get("entropy", 0.0),
                        "hazard": g.get("hazard", 0.0),
                        "structural_tension": g.get("structural_tension", 0.0),
                    }
                )

    g_df = pd.DataFrame(gates)
    print(f"Loaded {len(g_df)} total admitted mean_revert gates.")

    # Calculate forward returns (e.g., 15 mins, 30 mins)
    # We will compute the correlation of structural vectors against these.

    # Fast reindex
    g_df = g_df.sort_values("time")
    idx = g_df.index

    # We'll calculate the exact forward return by doing an asof merge or mapping
    # 15 minutes = 15 * 60 = 900 seconds = 180 S5 candles roughly, but time-based is safer.

    for h_mins in [5, 15, 30, 60]:
        print(f"\n--- Correlating with {h_mins}-minute Forward Return ---")

        # Calculate trailing return (maybe fading is the answer?)
        trailing_prices = (
            df["close"]
            .reindex(g_df["time"] - pd.Timedelta(minutes=h_mins), method="bfill")
            .values
        )
        current_prices = df["close"].reindex(g_df["time"], method="bfill").values
        forward_prices = (
            df["close"]
            .reindex(g_df["time"] + pd.Timedelta(minutes=h_mins), method="bfill")
            .values
        )

        g_df["current_price"] = current_prices
        g_df["trailing_ret"] = (
            (current_prices - trailing_prices) / trailing_prices * 10000
        )  # in bps
        g_df["forward_ret"] = (forward_prices - current_prices) / current_prices * 10000

        # Clean drops
        clean_df = g_df.dropna(
            subset=["trailing_ret", "forward_ret", "coherence", "entropy"]
        )

        correlations = {}
        for feature in [
            "trailing_ret",
            "coherence",
            "entropy",
            "hazard",
            "structural_tension",
            "stability",
        ]:
            vec_x = clean_df[feature].values
            vec_y = clean_df["forward_ret"].values

            # Avoid perfectly flat arrays causing NaNs
            if np.std(vec_x) < 1e-9 or np.std(vec_y) < 1e-9:
                continue

            p_corr, p_p = pearsonr(vec_x, vec_y)
            s_corr, s_p = spearmanr(vec_x, vec_y)

            correlations[feature] = {
                "pearson": p_corr,
                "pearson_p": p_p,
                "spearman": s_corr,
                "spearman_p": s_p,
            }

        # Sort by absolute Spearman and print
        sorted_corr = sorted(
            correlations.items(), key=lambda x: abs(x[1]["spearman"]), reverse=True
        )
        for feat, scores in sorted_corr:
            print(
                f"{feat:>20} | Spearman: {scores['spearman']:.4f} (p={scores['spearman_p']:.4f})  |  Pearson: {scores['pearson']:.4f}"
            )


if __name__ == "__main__":
    main()
