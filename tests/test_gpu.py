import torch
import json
from pathlib import Path
from datetime import datetime, timezone
import logging
import sys

# Setup logging to see output
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from scripts.research.optimizer.gpu_runner import GpuBacktestRunner

# Generate 1 single combo that is ultra loose
combo = {
    "Haz": 5.0,
    "Reps": 0,
    "SL": 999.0,
    "TP": 999.0,
    "Hold": 10,
    "Coh": 1.0,
    "Stab": 1.0,
    "Ent": 0.0,
}

start = datetime(2025, 11, 1, tzinfo=timezone.utc)
end = datetime(2026, 2, 1, tzinfo=timezone.utc)
cache = Path("output/market_data/USD_JPY.jsonl")

print("Starting sweep...")
res, _ = GpuBacktestRunner.execute_gpu_sweep(
    "USD_JPY", start, end, [combo], cache, target_signal_type="trend_sniper"
)
print(f"Sweep results: {res}")
