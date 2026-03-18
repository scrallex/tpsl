#!/usr/bin/env python3
"""Regime and trend math utilities using vectorization."""

import logging
from datetime import datetime
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


def compute_regime_filtering(
    instrument: str, start_dt: datetime, end_dt: datetime
) -> Dict[int, Tuple[bool, bool]]:
    """Generates a map of {timestamp_ms: is_trend_aligned} based on 200-hour SMA.

    Args:
        instrument: The instrument symbol.
        start_dt: The start datetime.
        end_dt: The end datetime.

    Returns:
        A map of timestamp MS to (long_ok, short_ok) booleans.
    """
    try:
        from scripts.research.data_store import ManifoldDataStore
        import numpy as np

        store = ManifoldDataStore()
        payload = store.load_candles(instrument, start_dt, end_dt, "S5")
        if not payload:
            return {}

        C = len(payload)
        np_closes = np.empty(C, dtype=np.float32)
        parsed_times_ms = []

        for i, row in enumerate(payload):
            ts_raw = row.get("time", "")
            if ts_raw:
                parsed_times_ms.append(
                    int(
                        datetime.fromisoformat(
                            ts_raw.replace("Z", "+00:00")
                        ).timestamp()
                        * 1000
                    )
                )
            else:
                parsed_times_ms.append(0)

            mid = row.get("mid", {})
            np_closes[i] = float(mid.get("c", 0.0))

        import torch

        closes = torch.from_numpy(np_closes)
        window_ticks = min(8640, max(100, C // 4))

        if window_ticks <= 1 or closes.numel() < window_ticks + 1:
            return {}

        cumsum = torch.cumsum(closes, dim=0)
        zero = torch.zeros(1, dtype=closes.dtype)
        window_sum = cumsum[window_ticks - 1 :] - torch.cat(
            [zero, cumsum[:-window_ticks]]
        )
        sma = torch.empty_like(closes)
        sma[: window_ticks - 1] = torch.nan
        sma[window_ticks - 1 :] = window_sum / window_ticks

        ready = ~torch.isnan(sma)
        slope = torch.zeros_like(sma)
        slope[12:] = sma[12:] - sma[:-12]

        long_ok = torch.zeros_like(ready, dtype=torch.bool)
        short_ok = torch.zeros_like(ready, dtype=torch.bool)
        long_ok[ready] = slope[ready] >= 0
        short_ok[ready] = slope[ready] <= 0

        long_map = long_ok.numpy()
        short_map = short_ok.numpy()

        regime_map = {}
        for i, ts_ms in enumerate(parsed_times_ms):
            regime_map[ts_ms] = (bool(long_map[i]), bool(short_map[i]))

        return regime_map
    except Exception as e:
        logger.error(f"Failed to pre-compute regimes: {e}")
        return {}
