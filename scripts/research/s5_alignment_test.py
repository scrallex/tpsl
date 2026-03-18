from scripts.trading.candle_utils import to_epoch_ms
import json
import argparse
from pathlib import Path
import sys
from collections import deque
from scripts.research.data_store import parse

S5_REVERSAL_WINDOW_MS = 60000


def test_s5_alignment(instrument):
    triggers_path = Path(f"output/{instrument.lower()}_m1_triggers.json")
    if not triggers_path.exists():
        print(f"No triggers found at {triggers_path}")
        return

    s5_candle_p = Path(f"output/market_data/{instrument}.jsonl")
    if not s5_candle_p.exists():
        print(f"No S5 candles found at {s5_candle_p}")
        return

    pip_multi = 100.0 if "JPY" in instrument else 10000.0

    triggers = []
    with triggers_path.open("r") as f:
        for line in f:
            if line.strip():
                triggers.append(json.loads(line))

    # Sort triggers by timestamp
    triggers.sort(key=lambda x: x["ts_ms"])

    triggers_by_time = {}
    for t in triggers:
        triggers_by_time[t["ts_ms"]] = t

    print(f"Loaded {len(triggers)} triggers. Processing S5 data...")

    price_improvements = []
    delays_sec = []
    missed_executions = 0

    active_trigger = None
    trigger_m1_close_time = 0
    trigger_end_window = 0

    with s5_candle_p.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            c_dict = json.loads(line)
            ts_ms = to_epoch_ms(parse(c_dict["time"]))

            p_o = float(c_dict["mid"]["o"])
            p_c = float(c_dict["mid"]["c"])
            direction = 1 if p_c >= p_o else 0

            # Check if this S5 candle corresponds to the open of an M1 trigger's execution window
            # The M1 candle was from T to T+S5_REVERSAL_WINDOW_MS. So the execution window opens exactly at T+S5_REVERSAL_WINDOW_MS.
            check_time = ts_ms - S5_REVERSAL_WINDOW_MS
            if check_time in triggers_by_time:
                active_trigger = triggers_by_time[check_time]
                trigger_m1_close_time = ts_ms
                trigger_end_window = (
                    ts_ms + S5_REVERSAL_WINDOW_MS
                )  # window to find an S5 reversal

            if active_trigger:
                if ts_ms > trigger_end_window:
                    # Window expired without finding a reversal.
                    missed_executions += 1
                    active_trigger = None
                    continue

                wanted_dir = 1 if active_trigger["is_long"] else 0

                # S5 Reversal
                if direction == wanted_dir:
                    new_entry = p_c
                    old_entry = active_trigger["entry_price"]

                    if active_trigger["is_long"]:
                        improvement = (old_entry - new_entry) * pip_multi
                    else:
                        improvement = (new_entry - old_entry) * pip_multi

                    price_improvements.append(improvement)
                    delays_sec.append((ts_ms - trigger_m1_close_time) / 1000.0)
                    active_trigger = None

    if len(price_improvements) == 0:
        print("No delayed executions found.")
        return

    avg_imp = sum(price_improvements) / len(price_improvements)
    avg_delay = sum(delays_sec) / len(delays_sec)
    win_rate_imp = (
        sum(1 for x in price_improvements if x > 0) / len(price_improvements) * 100.0
    )

    print("\n--- S5 EXECUTION ALIGNMENT RESULTS ---")
    print(f"Total M1 Triggers: {len(triggers)}")
    print(f"Executed via S5 Reversal: {len(price_improvements)}")
    print(f"Missed (No Reversal in 60s): {missed_executions}")
    print(f"Average Price Improvement: {avg_imp:+.2f} Pips")
    print(f"Average Execution Delay: {avg_delay:.1f} seconds")
    print(f"% Trades Improved: {win_rate_imp:.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", type=str, default="EUR_USD")
    args = parser.parse_args()
    test_s5_alignment(args.instrument)
