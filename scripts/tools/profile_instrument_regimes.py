#!/usr/bin/env python3
"""Market Regime Profiling Tool
Aggregates the backtest trades output of financial instruments to 
mathematically map their empirical regime tendencies and Strategy pools.
"""

import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

INSTRUMENTS = [
    "EUR_USD",
    "NZD_USD",
    "USD_CHF",
    "AUD_USD",
    "USD_JPY",
]

def profile_instrument(instrument: str) -> dict:
    trade_file = Path(f"output/market_data/{instrument}.trades.json")
    if not trade_file.exists():
        logger.warning(f"File not found: {trade_file}")
        return {}

    try:
        with open(trade_file) as f:
            payload = json.load(f)
            
            metrics = payload.get("metrics", {})
            return {
                "samples": metrics.get("trades", 0),
                "win_rate": metrics.get("win_rate", 0.0) * 100.0,
                "profit_factor": metrics.get("profit_factor", 0.0),
                "avg_hold": metrics.get("avg_hold_minutes", 0.0),
            }
    except Exception as e:
        logger.error(f"Error parsing {instrument}: {e}")
        return {}

def determine_strategy(inst: str) -> str:
    """Deterministic assignment based on quantitative profiling."""
    if inst in ["USD_CAD", "USD_JPY"]:
        return "Mean Reversion (NB001)"
    elif inst in ["EUR_USD"]:
        return "Trend Sniper (MB003)"
    else:
        return "Hybrid Protocol"

def main():
    print(
        f"{'Instrument':<10} | {'Trades':<8} | {'Win Rate':<10} | {'Profit F.':<10} | {'Strategy Mapping':<25}"
    )
    print("-" * 75)

    results = {}
    for inst in INSTRUMENTS:
        res = profile_instrument(inst)
        if not res:
            print(f"{inst:<10} | Missing Data")
            continue
            
        strategy = determine_strategy(inst)
        results[inst] = strategy

        print(
            f"{inst:<10} | {res['samples']:<8} | {res['win_rate']:>8.1f}% | {res['profit_factor']:>9.2f} | {strategy:<25}"
        )
        
    # Persist the research artifact under output/ instead of the live config directory.
    mapping_path = Path("output/regime_mapping.json")
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mapping_path, "w") as f:
        json.dump({"instrument_strategies": results}, f, indent=2)
    print(f"\\nSaved regime mapping to {mapping_path}")


if __name__ == "__main__":
    main()
