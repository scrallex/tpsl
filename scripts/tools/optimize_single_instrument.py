#!/usr/bin/env python3
import json
import pandas as pd
import argparse
import itertools
from pathlib import Path
import sys


# Import the refactored V8 RAM Simulator
from scripts.research.validate_signals import load_m1_dataset, run_v8_simulation_mem

# Full spectrum tuning grid specifically designed to unlock higher trade volumes
GRID = {
    "c_drift_min": [0.35, 0.45, 0.55],
    "e_pct_max": [0.30, 0.40, 0.50],
    "v_tick_pct_min": [0.60, 0.70, 0.80],
    "db_min": [1],
}


def get_combinations():
    keys, values = zip(*GRID.items())
    return [dict(zip(keys, v)) for v in itertools.product(*values)]


def optimize_single_instrument(instrument: str):
    print("=====================================================")
    print(f"--- INITIATING REGIME TRIGGER OPTIMIZATION: {instrument} ---")
    print("=====================================================")

    output_dir = Path("output/market_data")
    output_dir.mkdir(parents=True, exist_ok=True)

    combinations = get_combinations()
    total_combinations = len(combinations)

    print(f"\n[*] Booting V8 RAM Engine for {instrument}...")

    # Load the M1 memory structures ONCE
    candle_list, sig_dict = load_m1_dataset(instrument)
    if candle_list is None:
        print(f"[!] FATAL: Data missing for {instrument}, cannot proceed.")
        return

    print(
        f"[*] Sweeping {total_combinations} matrix combinations to find the 600-1500 trade boundary..."
    )

    best_pf = 1.01  # Must be strictly positive
    best_config = None
    best_trades = []

    for idx, params in enumerate(combinations):
        trades = run_v8_simulation_mem(candle_list, sig_dict, params)
        count = len(trades)

        # The Target Count Filter (approx 3-8 trades per day over 6 months)
        if 600 <= count <= 1500:
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

                if pf > best_pf:
                    best_pf = pf
                    best_config = params
                    best_trades = trades
                    print(
                        f"  -> [Update] Found better PF: {pf:.3f} | Trades: {count} | Config: {params}"
                    )

    if best_config:
        print(
            f"\n[+] GLOBAL MAXIMUM Locked! Config: {best_config} | Best PF: {best_pf:.3f} | Trades: {len(best_trades)}"
        )

        # Assign true internal instrument
        for t in best_trades:
            t["Instrument"] = instrument

        output_file = output_dir / f"{instrument}_regime_sweep.trades.json"
        with open(output_file, "w") as f:
            json.dump(best_trades, f, indent=4)

        print(f"[SUCCESS] Exported {len(best_trades)} trades to {output_file}")
    else:
        print(
            f"\n[-] Exhausted all {total_combinations} configs. No combinations met the 600-1500 trade bound with PF > 1.01."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Optimize a single instrument for frequent regime triggers."
    )
    parser.add_argument(
        "--instrument",
        type=str,
        default="EUR_USD",
        help="The instrument pair to optimize (e.g., EUR_USD)",
    )
    args = parser.parse_args()

    try:
        optimize_single_instrument(args.instrument)
    except KeyboardInterrupt:
        print("\n[!] Process aborted by user.")
        sys.exit(1)
