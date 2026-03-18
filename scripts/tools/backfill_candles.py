#!/usr/bin/env python3
"""Backfill Valkey candle history directly from OANDA.

This script is a thin recovery tool for situations where `md:candles:*` keys
have been pruned or ingestion was paused. It fetches a configurable lookback
window per instrument and stores the candles in the canonical
`md:candles:{instrument}:{granularity}` sorted-set schema expected by the
manifold/gate services.
"""
from __future__ import annotations

from scripts.trading.candle_utils import to_epoch_ms

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Mapping

import redis

from scripts.trading.oanda import OandaConnector
from .time_utils import parse_utc_time


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_instruments(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def store_candles(
    client: redis.Redis,
    instrument: str,
    granularity: str,
    candles: Iterable[Mapping[str, object]],
) -> int:
    key = f"md:candles:{instrument}:{granularity}"
    pipe = client.pipeline(transaction=False)
    buffered = 0
    stored = 0
    for candle in candles:
        time_str = candle.get("time")
        mid = candle.get("mid") or {}
        if not isinstance(time_str, str) or not mid:
            continue
        try:
            ts_ms = to_epoch_ms(_parse_time(time_str))
        except Exception:
            continue
        record = {
            "time": time_str,
            "t": ts_ms,
            "o": float(mid.get("o")),
            "h": float(mid.get("h")),
            "l": float(mid.get("l")),
            "c": float(mid.get("c")),
            "v": float(candle.get("volume") or 0.0),
            "mid": mid,
        }
        member = json.dumps(record, separators=(",", ":"))
        pipe.zadd(key, {member: ts_ms})
        buffered += 1
        if buffered >= 512:
            pipe.execute()
            buffered = 0
        stored += 1
    if buffered:
        pipe.execute()
    return stored


def backfill(
    instruments: List[str],
    *,
    granularity: str,
    lookback_hours: int,
    chunk_minutes: int,
    redis_url: str,
) -> None:
    connector = OandaConnector(read_only=True)
    client = redis.from_url(redis_url)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)
    chunk = timedelta(minutes=chunk_minutes)
    for instrument in instruments:
        cursor = start
        total = 0
        while cursor < end:
            chunk_end = min(cursor + chunk, end)
            candles = connector.get_candles(
                instrument,
                granularity=granularity,
                from_time=_iso(cursor),
                to_time=_iso(chunk_end),
            )
            completes = [c for c in candles if c.get("complete")]
            stored = store_candles(client, instrument, granularity, completes)
            total += stored
            cursor = chunk_end
        print(
            f"[{instrument}] stored {total} candles into Valkey key md:candles:{instrument}:{granularity}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill OANDA candles into Valkey")
    parser.add_argument(
        "--instruments",
        default=os.getenv(
            "HOTBAND_PAIRS", "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF,USD_CAD,NZD_USD"
        ),
        help="Comma separated instrument list",
    )
    parser.add_argument(
        "--granularity", default="M1", help="OANDA granularity (default M1)"
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Hours of history to fetch per instrument",
    )
    parser.add_argument(
        "--chunk-minutes", type=int, default=60, help="Chunk size per request (minutes)"
    )
    parser.add_argument(
        "--redis",
        default=os.getenv("VALKEY_URL", "redis://localhost:6379/0"),
        help="Valkey URL",
    )
    args = parser.parse_args()

    instruments = _normalise_instruments(args.instruments)
    if not instruments:
        raise SystemExit("No instruments provided")

    backfill(
        instruments,
        granularity=args.granularity.upper(),
        lookback_hours=max(1, args.lookback_hours),
        chunk_minutes=max(1, args.chunk_minutes),
        redis_url=args.redis,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
