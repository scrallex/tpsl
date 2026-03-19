"""Simulator I/O Adapter for loading candles and gates."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dateutil.parser import parse

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore

from scripts.research.data_store import ManifoldDataStore
from scripts.research.simulator.gate_cache import (
    ensure_historical_gate_cache,
    gate_cache_path_for,
)
from scripts.trading.candle_utils import to_epoch_ms
from scripts.research.simulator.models import OHLCCandle

logger = logging.getLogger(__name__)
UTC = timezone.utc


class BacktestDataAdapter:
    """Handles loading of historical OHLC candles and Gate/Signature events from Valkey or Disk."""

    def __init__(self, redis_url: Optional[str] = None, granularity: str = "S5"):
        self.redis_url = redis_url
        self.granularity = granularity
        self._redis_client = redis.from_url(redis_url) if redis_url and redis else None

    def load_ohlc_candles(
        self, instrument: str, start: datetime, end: datetime
    ) -> List[OHLCCandle]:
        if ManifoldDataStore is None:
            return []

        gran = self.granularity.upper()
        store = ManifoldDataStore()
        payload = store.load_candles(instrument, start, end, gran)
        candles: List[OHLCCandle] = []
        for row in payload or []:
            mid = row.get("mid") or {}
            try:
                o = float(mid.get("o") or mid.get("open") or mid.get("c") or 0)
                h = float(mid.get("h") or mid.get("high") or mid.get("c") or 0)
                l = float(mid.get("l") or mid.get("low") or mid.get("c") or 0)
                c = float(mid.get("c") or mid.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if c == 0:
                continue
            ts_raw = row.get("time")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except Exception:
                continue
            if ts < start or ts > end:
                continue
            # Ensure high >= low
            if h < l:
                h, l = l, h
            candles.append(OHLCCandle(time=ts, open=o, high=h, low=l, close=c))
        candles.sort(key=lambda x: x.time)
        return candles

    def load_gate_events(
        self,
        instrument: str,
        start: datetime,
        end: datetime,
        signal_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        start_ms = to_epoch_ms(start)
        end_ms = to_epoch_ms(end)

        # --- START CACHE LOAD ---
        if ManifoldDataStore is not None:
            store = ManifoldDataStore()
            gate_cache = gate_cache_path_for(
                instrument, signal_type, base_dir=store.data_dir
            )
            candle_cache = store.data_dir / f"{instrument.upper()}.jsonl"
            ensure_historical_gate_cache(
                instrument,
                start,
                end,
                signal_type=signal_type,
                base_dir=store.data_dir,
                candle_cache_path=candle_cache if candle_cache.exists() else None,
                gate_cache_path=gate_cache,
            )
            if gate_cache.exists():
                try:
                    gate_entries = []
                    with open(gate_cache, "r") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            g = json.loads(line)

                            # Parse out time string into ms for the simulator
                            if "ts_ms" not in g and "time" in g:
                                parsed = parse(g["time"])
                                g["ts_ms"] = to_epoch_ms(parsed)

                            ts = int(g.get("ts_ms", 0))
                            if start_ms <= ts <= end_ms:
                                gate_entries.append(g)
                    if gate_entries:
                        gate_entries.sort(key=lambda r: r.get("ts_ms", 0))
                        logger.info(
                            f"Loaded {len(gate_entries)} gates from {gate_cache}"
                        )
                        return gate_entries
                except Exception as e:
                    logger.warning(f"Error reading gate cache: {e}")
        # --- END CACHE LOAD ---

        client = self._redis_client
        if client is None:  # Force synthetic for debugging bundle logic
            return []
        key = f"gate:index:{instrument.upper()}"
        entries = []
        try:
            raw_entries = client.zrangebyscore(key, start_ms, end_ms, withscores=True)
        except Exception:
            raw_entries = []
        for raw, score in raw_entries:
            try:
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            except Exception:
                continue
            data["ts_ms"] = int(score)
            data.setdefault("source", "valkey")
            entries.append(data)
        entries.sort(key=lambda r: r.get("ts_ms", 0))
        return entries
