#!/usr/bin/env python3
import json
import pandas as pd
import numpy as np
import itertools
from pathlib import Path
import sys


# Import the refactored V8 RAM Simulator
from scripts.research.validate_signals import load_m1_dataset, run_v8_simulation_mem

PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY", "USD_CAD", "NZD_USD", "USD_CHF"]

# The Fine-Tuning Grid (Moderate relaxation for 100-300 average per pair)
GRID = {
    "c_drift_min": [0.55, 0.60, 0.65],
    "e_pct_max": [0.15, 0.20, 0.25],
    "v_tick_pct_min": [0.80, 0.85, 0.90],
    "db_min": [1, 2],
}


def get_combinations():
    keys, values = zip(*GRID.items())
    return [dict(zip(keys, v)) for v in itertools.product(*values)]


def extract_golden_portfolio():
    print("=====================================================")
    print("--- INITIATING GOLDEN PORTFOLIO SWEEP (V8 RAM Engine)---")
    print("=====================================================")

    output_dir = Path("output/golden_portfolio")
    output_dir.mkdir(parents=True, exist_ok=True)

    golden_configs = {}
    master_ledger = []

    total_combinations = len(get_combinations())

    for pair in PAIRS:
        print(f"\n[*] Booting V8 RAM Engine for {pair}...")

        # 1. Load the 6-month M1 memory structures ONCE
        candle_list, sig_dict = load_m1_dataset(pair)
        if candle_list is None:
            print(f"[!] Warning: Data missing for {pair}, skipping...")
            continue

        print(
            f"[*] Sweeping {total_combinations} matrix combinations to find the 100-300 trade boundary..."
        )

        best_pf = 0.0
        best_config = None
        best_trades = []

        # 2. Sweep the memory
        for params in get_combinations():
            trades = run_v8_simulation_mem(candle_list, sig_dict, params)
            count = len(trades)

            # The Target Count Filter
            if 100 <= count <= 300:
                df_trades = pd.DataFrame(trades)

                # Check for strictly positive edges
                if not df_trades.empty:
                    wins = df_trades[df_trades["R_Multiple"] > 0]
                    losses = df_trades[df_trades["R_Multiple"] <= 0]

                    gross_loss_r = abs(losses["R_Multiple"].sum())
                    pf = (
                        (wins["R_Multiple"].sum() / gross_loss_r)
                        if gross_loss_r > 0
                        else float("inf")
                    )

                    # We optimize purely for the highest Profit Factor within the defined trade frequency boundary
                    if pf > best_pf:
                        best_pf = pf
                        best_config = params
                        best_trades = trades

        if best_config:
            print(
                f"[+] {pair} Locked! Config: {best_config} | Best PF: {best_pf:.2f} | Trades: {len(best_trades)}"
            )
            golden_configs[pair] = best_config

            # Assign true internal instrument
            for t in best_trades:
                t["Instrument"] = pair

            master_ledger.extend(best_trades)

            # Export the individual pair trades
            with open(output_dir / f"{pair}.golden_trades.json", "w") as f:
                json.dump(best_trades, f, indent=4)
        else:
            print(f"[-] {pair} could not find a config within the trade limits.")

    # Export the Global Configuration
    Path("config").mkdir(exist_ok=True)
    with open("config/golden_portfolio_config.json", "w") as f:
        json.dump(
            {"strategy": "V8_Golden_Matrix", "pairs": golden_configs}, f, indent=4
        )

    # Export and Score the Master Ledger
    if master_ledger:
        df = pd.DataFrame(master_ledger)
        time_col = "Entry_Time" if "Entry_Time" in df.columns else "entry_time"
        df[time_col] = pd.to_datetime(df[time_col])
        df = df.sort_values(by=time_col).reset_index(drop=True)

        # Ensure Net_R exists for metric tracking
        r_col = "Net_R" if "Net_R" in df.columns else "R_Multiple"
        df[r_col] = pd.to_numeric(df[r_col])

        df.to_csv("SEP_TRADER_MASTER_LEDGER.csv", index=False)
        print(
            f"\n[SUCCESS] Exported {len(df)} total portfolio trades to SEP_TRADER_MASTER_LEDGER.csv"
        )
        print("[SUCCESS] Master Config saved to config/golden_portfolio_config.json")

        print("\n--- GOLDEN PORTFOLIO MASTER STATS ---")
        wins = df[df["R_Multiple"] > 0]
        losses = df[df["R_Multiple"] <= 0]

        gross_loss_r = abs(losses["R_Multiple"].sum())
        pf = (
            (wins["R_Multiple"].sum() / gross_loss_r)
            if gross_loss_r > 0
            else float("inf")
        )
        exp = df["R_Multiple"].mean()

        print(f"Total Combined Trades: {len(df)}")
        print(f"Global Portfolio Profit Factor: {pf:.2f}")
        print(f"Global Portfolio Expectancy (R): {exp:.2f}")

    else:
        print("\n[-] FATAL: Failed to aggregate any portfolio trades.")


if __name__ == "__main__":
    extract_golden_portfolio()
