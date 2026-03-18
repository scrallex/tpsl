"""Codec analytics and aggregation utilities."""

from __future__ import annotations

import json
import math
import statistics
from typing import Dict, Iterable, List, Sequence

from .types import EncodedWindow


def _calculate_correlations(rows: List[EncodedWindow]) -> Dict[str, float]:
    coh = [row.metrics["coherence"] for row in rows]
    stab = [row.metrics["stability"] for row in rows]
    ent = [row.metrics["entropy"] for row in rows]
    hazard = [row.metrics["hazard"] for row in rows]
    vol = [row.canonical.realized_vol for row in rows]
    autocorr = [row.canonical.autocorr for row in rows]
    trend = [row.canonical.trend_strength for row in rows]

    def corr(a: Sequence[float], b: Sequence[float]) -> float:
        if len(a) != len(b) or len(a) < 2:
            return 0.0
        mean_a = statistics.fmean(a)
        mean_b = statistics.fmean(b)
        num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
        den = math.sqrt(
            sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b)
        )
        return num / den if den else 0.0

    return {
        "coherence_vs_autocorr": corr(coh, autocorr),
        "hazard_vs_vol": corr(hazard, vol),
        "stability_vs_trend": corr(stab, trend),
        "entropy_vs_vol": corr(ent, vol),
    }


def _calculate_regime_breakdown(rows: List[EncodedWindow]) -> Dict[str, float]:
    return {
        regime: sum(1 for row in rows if row.canonical.regime == regime) / len(rows)
        for regime in {
            "trend_bull",
            "trend_bear",
            "mean_revert",
            "chaotic",
            "neutral",
        }
    }


def window_summary(windows: Iterable[EncodedWindow]) -> Dict[str, object]:
    """Utility used by validation scripts to aggregate correlations."""
    rows = list(windows)
    if not rows:
        return {"count": 0}

    return {
        "count": len(rows),
        "corr": _calculate_correlations(rows),
        "regime_breakdown": _calculate_regime_breakdown(rows),
    }


def windows_to_jsonl(windows: Iterable[EncodedWindow]) -> str:
    """Serialize encoded windows into newline-delimited JSON for inspection."""
    return "\n".join(json.dumps(window.to_json()) for window in windows)
