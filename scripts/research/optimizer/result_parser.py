"""Result aggregation and parsing for GPU backtests."""

from typing import Any, Dict, List
import numpy as np


def parse_gpu_results(
    combo_dicts: List[Dict[str, Any]],
    pnl_out: np.ndarray,
    win_out: np.ndarray,
    loss_out: np.ndarray,
) -> List[Dict[str, Any]]:
    """Converts raw GPU output arrays back into sorted dictionary results."""
    results = []
    for i in range(len(combo_dicts)):
        total = win_out[i] + loss_out[i]
        win_rate = float(win_out[i] / total) if total > 0 else 0.0
        pnl = float(pnl_out[i])

        score = pnl * (win_rate**1.5) if pnl > 0 else pnl

        results.append(
            {
                "idx": i,
                "params": combo_dicts[i],
                "score": score,
                "metrics": {
                    "trades": int(total),
                    "win_rate": win_rate,
                    "pnl_bps": pnl,
                },
            }
        )
    results.sort(key=lambda x: x.get("score", x["metrics"]["pnl_bps"]), reverse=True)
    return results
