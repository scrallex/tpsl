import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List


def load_gates(gate_path: Path) -> List[Dict[str, Any]]:
    gates = []
    with open(gate_path, "r") as f:
        for line in f:
            if line.strip():
                gates.append(json.loads(line))
    return gates


def calculate_metrics(
    gates: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], float]:
    mr_gates = [
        g for g in gates if str(g.get("source", "")).lower() == "mean_reversion"
    ]
    max_haz = max((g.get("hazard", 0) for g in gates), default=0)
    return mr_gates, max_haz


def print_report(
    gates: List[Dict[str, Any]], mr_gates: List[Dict[str, Any]], max_haz: float
):
    print(f"Total gates: {len(gates)}")
    print(f"Mean Reversion gates: {len(mr_gates)}")
    print(f"Absolute max hazard recorded in any gate: {max_haz:.4f}")

    if mr_gates:
        print("\n--- SAMPLE MR GATES ---")
        for g in mr_gates[:25]:
            haz = g.get("hazard")
            ent = g.get("components", {}).get("entropy")
            dt = datetime.fromtimestamp(g.get("ts_ms") / 1000.0).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(
                f"[{dt}] Dir: {g.get('direction')} | Haz: {haz:.3f} | Ent: {ent:.3f} | Regime: {g.get('regime')}"
            )


if __name__ == "__main__":
    gate_path = Path("output/market_data/USD_CAD.gates.jsonl")
    try:
        gates = load_gates(gate_path)
        mr_gates, max_haz = calculate_metrics(gates)
        print_report(gates, mr_gates, max_haz)
    except FileNotFoundError:
        print(f"Gate file not found at {gate_path}")
