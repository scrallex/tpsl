#!/usr/bin/env python3
import time
import subprocess
import os
import sys
from pathlib import Path
from datetime import datetime

# Instruments to watch
INSTRUMENTS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF"]
OUTPUT_DIR = Path("output")


def main():
    print("Starting Auto-Backfill Watcher (Strict Mtime Check)...")

    # Track state
    backfill_procs = {}  # inst -> Popen
    completed_backfills = set()

    # Capture start time. Only files modified AFTER this time will be processed.
    start_time = time.time()
    print(
        f"[{datetime.now()}] Watching for files modified after {datetime.fromtimestamp(start_time)}"
    )

    # Main loop
    while True:
        # 1. Check for new files and launch backfills
        for inst in INSTRUMENTS:
            if inst in backfill_procs or inst in completed_backfills:
                continue

            jsonl_path = OUTPUT_DIR / f"{inst}.jsonl"
            if not jsonl_path.exists():
                continue

            try:
                mtime = jsonl_path.stat().st_mtime
                size = jsonl_path.stat().st_size

                # Strict check: file must be NEWER than script start
                if mtime <= start_time:
                    continue

                if size < 1_000_000:
                    continue

                # Launch backfill
                print(
                    f"[{datetime.now()}] Detected new data for {inst} ({size/1e6:.1f} MB, mtime={datetime.fromtimestamp(mtime)})"
                )

                cmd = [
                    "python3",
                    "-m",
                    "scripts.research.data_store",
                    "--instruments",
                    inst,
                    "--start",
                    "2025-11-01",
                    "--end",
                    "2026-02-18",
                ]

                log_path = Path(f"output/backfill_{inst}.log")
                log_file = log_path.open("w")

                # Propagate environment
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
                backfill_procs[inst] = proc
                print(
                    f"[{datetime.now()}] Launched backfill for {inst} (PID {proc.pid})"
                )

            except Exception as e:
                print(f"Error checking {inst}: {e}")

        # 2. Check for completion of backfills
        active = list(backfill_procs.keys())
        for inst in active:
            proc = backfill_procs[inst]
            ret = proc.poll()
            if ret is not None:
                # Process finished
                if ret == 0:
                    print(
                        f"[{datetime.now()}] Backfill for {inst} COMPLETED successfully."
                    )
                    completed_backfills.add(inst)
                else:
                    print(
                        f"[{datetime.now()}] Backfill for {inst} FAILED (Exit Code {ret}). Check output/backfill_{inst}.log"
                    )
                    # Treat as done to allow pipeline to proceed?
                    # Retry? No, failure is usually permanent unless fixed.
                    completed_backfills.add(inst)

                del backfill_procs[inst]

        # 3. Check if ALL done
        if len(completed_backfills) == len(INSTRUMENTS):
            print(f"[{datetime.now()}] All {len(INSTRUMENTS)} instruments processed.")
            print(f"[{datetime.now()}] Triggering Optimization...")

            # Launch Optimization
            opt_cmd = ["bash", "scripts/research/run_extended_optimization.sh"]
            opt_log = open("output/optimization_pipeline.log", "w")

            opt_proc = subprocess.Popen(
                opt_cmd, stdout=opt_log, stderr=subprocess.STDOUT
            )
            print(
                f"[{datetime.now()}] Optimization launched (PID {opt_proc.pid}). Logging to output/optimization_pipeline.log"
            )
            print("Watcher pipeline sequence complete. Exiting.")
            break

        time.sleep(10)


if __name__ == "__main__":
    if not os.getenv("OANDA_API_KEY"):
        print("WARNING: OANDA env vars missing.")
    try:
        main()
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
