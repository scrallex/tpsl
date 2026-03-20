#!/usr/bin/env python3
"""Continuously fetch OANDA candles and push them into Valkey."""
from __future__ import annotations

from scripts.trading.candle_utils import to_epoch_ms

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence

import redis


from scripts.trading.oanda import OandaConnector  # type: ignore


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _normalise_candle(payload: Dict[str, object]) -> Dict[str, object] | None:
    mid = payload.get("mid") or {}
    if not isinstance(mid, dict):
        return None
    complete = bool(payload.get("complete", False))
    if not complete:
        return None
    try:
        timestamp_iso = str(payload.get("time"))
        dt = _parse_iso(timestamp_iso)
        ts_ms = to_epoch_ms(dt)
        record = {
            "time": dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "t": ts_ms,
            "o": float(mid.get("o") or mid.get("open")),
            "h": float(mid.get("h") or mid.get("high")),
            "l": float(mid.get("l") or mid.get("low")),
            "c": float(mid.get("c") or mid.get("close")),
            "v": float(payload.get("volume") or 0.0),
            "mid": {
                "o": float(mid.get("o") or mid.get("open")),
                "h": float(mid.get("h") or mid.get("high")),
                "l": float(mid.get("l") or mid.get("low")),
                "c": float(mid.get("c") or mid.get("close")),
            },
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return record


def _granularity_seconds(granularity: str) -> int:
    code = str(granularity or "").strip().upper()
    if len(code) < 2:
        return 60
    unit = code[0]
    try:
        value = int(code[1:])
    except ValueError:
        return 60
    if unit == "S":
        return max(1, value)
    if unit == "M":
        return max(1, value) * 60
    if unit == "H":
        return max(1, value) * 3600
    if unit == "D":
        return max(1, value) * 86400
    return 60


def _stale_threshold_seconds(granularity: str) -> float:
    override = os.getenv("CANDLE_STREAM_STALE_SECONDS")
    if override:
        try:
            return max(15.0, float(override))
        except ValueError:
            pass
    return max(90.0, float(_granularity_seconds(granularity) * 6))


def _target_latency_seconds(granularity: str) -> float:
    override = os.getenv("CANDLE_STREAM_TARGET_LATENCY_SECONDS")
    if override:
        try:
            return max(10.0, float(override))
        except ValueError:
            pass
    return max(20.0, float(_granularity_seconds(granularity) * 4))


def _latest_candle_ts_ms(candles: Sequence[Dict[str, object]]) -> Optional[int]:
    if not candles:
        return None
    latest = candles[-1].get("time")
    if not latest:
        return None
    try:
        latest_dt = _parse_iso(str(latest))
    except ValueError:
        return None
    return to_epoch_ms(latest_dt)


def _latest_candle_age_seconds(candles: Sequence[Dict[str, object]]) -> float | None:
    if not candles:
        return None
    latest = candles[-1].get("time")
    if not latest:
        return None
    try:
        latest_dt = _parse_iso(str(latest))
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - latest_dt).total_seconds())


def _window_count_for_instrument(
    *,
    last_written_ts_ms: Optional[int],
    granularity: str,
    bootstrap_count: int,
    incremental_count: int,
    cached_count: Optional[int] = None,
) -> int:
    bootstrap_window = max(5, bootstrap_count)
    incremental_window = max(5, incremental_count)
    if cached_count is not None and int(cached_count) < bootstrap_window:
        return bootstrap_window
    if last_written_ts_ms is None:
        return bootstrap_window

    latest_dt = datetime.fromtimestamp(last_written_ts_ms / 1000.0, tz=timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - latest_dt).total_seconds())
    recover_after = float(_granularity_seconds(granularity) * incremental_window)
    if age_seconds > recover_after:
        return bootstrap_window
    return incremental_window


def _refresh_stale_candles(
    connector: OandaConnector,
    *,
    instrument: str,
    granularity: str,
    recent_count: int,
    candles: Sequence[Dict[str, object]],
    connector_factory: Callable[..., OandaConnector] = OandaConnector,
) -> tuple[Sequence[Dict[str, object]], OandaConnector]:
    return _refresh_candles_if_needed(
        connector,
        instrument=instrument,
        granularity=granularity,
        recent_count=recent_count,
        candles=candles,
        connector_factory=connector_factory,
        refresh_after_seconds=_stale_threshold_seconds(granularity),
        reason_label="stale",
    )


def _refresh_candles_if_needed(
    connector: OandaConnector,
    *,
    instrument: str,
    granularity: str,
    recent_count: int,
    candles: Sequence[Dict[str, object]],
    connector_factory: Callable[..., OandaConnector],
    refresh_after_seconds: float,
    reason_label: str,
) -> tuple[Sequence[Dict[str, object]], OandaConnector]:
    age_seconds = _latest_candle_age_seconds(candles)
    if age_seconds is None or age_seconds <= refresh_after_seconds:
        return candles, connector

    print(
        f"[stream] {reason_label} {granularity} feed for {instrument}: latest age {age_seconds:.1f}s > {refresh_after_seconds:.1f}s; refreshing connector",
        flush=True,
    )
    try:
        connector.session.close()
    except Exception:
        pass

    refreshed = connector_factory(read_only=connector.read_only)
    try:
        fresh_candles = refreshed.get_candles(
            instrument,
            granularity=granularity,
            count=max(5, recent_count),
        )
    except (
        ConnectionError,
        TimeoutError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(
            f"[stream] refresh failed for {instrument}: {exc}",
            flush=True,
        )
        return candles, refreshed

    fresh_age = _latest_candle_age_seconds(fresh_candles)
    if fresh_age is not None and fresh_age < age_seconds:
        latest_time = fresh_candles[-1].get("time") if fresh_candles else "n/a"
        print(
            f"[stream] recovered {instrument} at {latest_time}",
            flush=True,
        )
    return fresh_candles, refreshed


def _fetch_instrument_candles(
    *,
    instrument: str,
    connector: OandaConnector,
    connector_factory: Callable[..., OandaConnector],
    granularity: str,
    bootstrap_count: int,
    incremental_count: int,
    last_written_ts_ms: Optional[int],
    cached_count: Optional[int],
) -> tuple[str, Sequence[Dict[str, object]], OandaConnector, int]:
    window_count = _window_count_for_instrument(
        last_written_ts_ms=last_written_ts_ms,
        granularity=granularity,
        bootstrap_count=bootstrap_count,
        incremental_count=incremental_count,
        cached_count=cached_count,
    )
    candles = connector.get_candles(
        instrument,
        granularity=granularity,
        count=window_count,
    )
    latest_ts_ms = _latest_candle_ts_ms(candles)
    target_latency = _target_latency_seconds(granularity)
    if (
        last_written_ts_ms is not None
        and latest_ts_ms is not None
        and latest_ts_ms <= last_written_ts_ms
        and (_latest_candle_age_seconds(candles) or 0.0) > target_latency
    ):
        candles, connector = _refresh_candles_if_needed(
            connector,
            instrument=instrument,
            granularity=granularity,
            recent_count=window_count,
            candles=candles,
            connector_factory=connector_factory,
            refresh_after_seconds=target_latency,
            reason_label="lagging",
        )
    candles, connector = _refresh_stale_candles(
        connector,
        instrument=instrument,
        granularity=granularity,
        recent_count=window_count,
        candles=candles,
        connector_factory=connector_factory,
    )
    return instrument, candles, connector, window_count


def stream_candles(
    *,
    connector: OandaConnector,
    instruments: Sequence[str],
    redis_url: str,
    granularity: str,
    interval: float,
    recent_count: int,
    max_entries: int,
    stop_event: Event,
    connector_factory: Callable[..., OandaConnector] = OandaConnector,
) -> None:
    client = redis.from_url(redis_url)
    instruments = [inst.upper() for inst in instruments]
    incremental_count = int(
        os.getenv("CANDLE_STREAM_INCREMENTAL_COUNT", str(min(12, recent_count))) or min(12, recent_count)
    )
    max_workers = int(
        os.getenv("CANDLE_STREAM_MAX_WORKERS", str(max(1, len(instruments)))) or max(1, len(instruments))
    )
    connector_map: Dict[str, OandaConnector] = {}
    for idx, instrument in enumerate(instruments):
        connector_map[instrument] = (
            connector if idx == 0 else connector_factory(read_only=connector.read_only)
        )
    last_written_ts_ms: Dict[str, Optional[int]] = {inst: None for inst in instruments}
    last_logged_ts_ms: Dict[str, int] = {}
    print(
        f"[stream] starting candle ingest for {', '.join(instruments)} gran={granularity} interval={interval}s",
        flush=True,
    )
    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(instruments))))
    try:
        while not stop_event.is_set():
            started = time.time()
            cached_counts: Dict[str, int] = {}
            count_pipe = client.pipeline()
            for instrument in instruments:
                count_pipe.zcard(f"md:candles:{instrument}:{granularity}")
            try:
                raw_counts = count_pipe.execute()
            except redis.RedisError as exc:  # pragma: no cover - redis issues
                print(f"[stream] redis zcard failed: {exc}", flush=True)
                raw_counts = [0] * len(instruments)
            for instrument, raw_count in zip(instruments, raw_counts):
                try:
                    cached_counts[instrument] = int(raw_count or 0)
                except (TypeError, ValueError):
                    cached_counts[instrument] = 0

            futures = {
                executor.submit(
                    _fetch_instrument_candles,
                    instrument=instrument,
                    connector=connector_map[instrument],
                    connector_factory=connector_factory,
                    granularity=granularity,
                    bootstrap_count=recent_count,
                    incremental_count=incremental_count,
                    last_written_ts_ms=last_written_ts_ms.get(instrument),
                    cached_count=cached_counts.get(instrument),
                ): instrument
                for instrument in instruments
            }
            for future in as_completed(futures):
                instrument = futures[future]
                try:
                    _, candles, refreshed_connector, window_count = future.result()
                except (
                    ConnectionError,
                    TimeoutError,
                    ValueError,
                    RuntimeError,
                ) as exc:  # pragma: no cover - network issues
                    print(f"[stream] fetch failed for {instrument}: {exc}", flush=True)
                    continue
                connector_map[instrument] = refreshed_connector

                key = f"md:candles:{instrument}:{granularity}"
                pipe = client.pipeline()
                inserted = 0
                latest_record_ts_ms: Optional[int] = None
                latest_record_time: Optional[str] = None
                for candle in candles or []:
                    record = _normalise_candle(candle)
                    if not record:
                        continue
                    ts_ms = int(record["t"])
                    blob = json.dumps(record, separators=(",", ":"))
                    pipe.zadd(key, {blob: ts_ms})
                    inserted += 1
                    latest_record_ts_ms = ts_ms
                    latest_record_time = str(record["time"])
                if max_entries > 0:
                    pipe.zremrangebyrank(key, 0, -max_entries - 1)
                try:
                    pipe.execute()
                except redis.RedisError as exc:  # pragma: no cover - redis issues
                    print(
                        f"[stream] redis write failed for {instrument}: {exc}", flush=True
                    )
                    continue

                if latest_record_ts_ms is not None:
                    last_written_ts_ms[instrument] = latest_record_ts_ms
                    if latest_record_ts_ms > last_logged_ts_ms.get(instrument, 0):
                        last_logged_ts_ms[instrument] = latest_record_ts_ms
                        print(
                            f"[stream] {instrument}: synced through {latest_record_time} (window={window_count}, inserted={inserted})",
                            flush=True,
                        )
            elapsed = time.time() - started
            delay = max(0.5, interval - elapsed)
            stop_event.wait(delay)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        for live_connector in connector_map.values():
            try:
                live_connector.session.close()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live OANDA candle ingestor")
    parser.add_argument(
        "--instruments", help="Comma separated instruments (defaults to HOTBAND_PAIRS)"
    )
    parser.add_argument(
        "--granularity", default=os.getenv("CANDLE_STREAM_GRANULARITY", "M1")
    )
    parser.add_argument(
        "--redis", default=os.getenv("VALKEY_URL", "redis://localhost:6379/0")
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="Polling interval seconds"
    )
    parser.add_argument(
        "--recent-count",
        type=int,
        default=120,
        help="How many candles to pull each cycle",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=5000,
        help="Max candles kept per key (0=unbounded)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_pairs = args.instruments or os.getenv("HOTBAND_PAIRS", "")
    instruments = [
        item.strip().upper() for item in raw_pairs.split(",") if item.strip()
    ]
    if not instruments:
        print(
            "[stream] no instruments configured (set --instruments or HOTBAND_PAIRS)",
            file=sys.stderr,
        )
        return 1
    connector = OandaConnector()
    if not connector.api_key or not connector.account_id:
        print("[stream] OANDA credentials missing", file=sys.stderr)
        return 2
    stop_event = Event()

    def _shutdown(signum, _frame):  # pragma: no cover - signal handler
        print(f"[stream] signal {signum} received; stopping", flush=True)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # pragma: no cover
            pass

    stream_candles(
        connector=connector,
        instruments=instruments,
        redis_url=args.redis,
        granularity=args.granularity,
        interval=max(0.5, args.interval),
        recent_count=max(5, args.recent_count),
        max_entries=max(0, args.max_entries),
        stop_event=stop_event,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
