#!/usr/bin/env python3

import json
from datetime import datetime, timezone, timedelta
from scripts.research.optimizer.gpu_runner import GpuBacktestRunner


def run_winner():
    instrument = "USD_CAD"
    signal_type = "mean_reversion"
    end_dt = datetime.now(timezone.utc).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=30)

    with open("output/live_params.json") as f:
        lp = json.load(f)
    winner_cfg = lp[instrument][signal_type]

    print(f"Executing GPU Trace on winner: {winner_cfg}")

    try:
        res, _ = GpuBacktestRunner.execute_gpu_sweep(
            instrument,
            start_dt,
            end_dt,
            [winner_cfg],
            use_regime=True,
            target_signal_type=signal_type,
        )
        print(f"--- GPU FINAL Result ---")
        print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    run_winner()
