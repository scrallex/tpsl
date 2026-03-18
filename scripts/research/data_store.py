"""Unified Data Store for Market History and Manifold Signatures."""

from scripts.trading.candle_utils import to_epoch_ms
import json
import logging
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from scripts.trading.oanda import OandaConnector
from scripts.trading.env_loader import load_oanda_env

ROOT = Path(__file__).resolve().parents[2]

GRANULARITY_SECONDS = {
    "S5": 5,
    "S10": 10,
    "S15": 15,
    "S30": 30,
    "M1": 60,
    "M2": 120,
    "M4": 240,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
    "H6": 21600,
    "H8": 28800,
    "H12": 43200,
    "D": 86400,
    "W": 604800,
    "M": 2592000,
}


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone(timezone.utc)

def iter_candles(
    connector: OandaConnector,
    instrument: str,
    granularity: str,
    start: datetime,
    end: datetime,
    step: timedelta,
    expected_start: Optional[datetime] = None,
    last_known_close: Optional[float] = None,
) -> Iterable[Dict[str, Any]]:
    current = start
    gran_seconds = GRANULARITY_SECONDS.get(granularity, 60)
    time_step = timedelta(seconds=gran_seconds)

    expected_next_time = expected_start
    last_close = last_known_close

    while current < end:
        chunk_end = min(current + step, end)
        candles = connector.get_candles(
            instrument,
            granularity=granularity,
            from_time=isoformat(current),
            to_time=isoformat(chunk_end),
        )
        if not candles:
            current = chunk_end
            if expected_next_time is not None and last_close is not None:
                while expected_next_time < chunk_end:
                    yield {
                        "time": isoformat(expected_next_time),
                        "volume": 0,
                        "complete": True,
                        "mid": {
                            "o": str(last_close),
                            "h": str(last_close),
                            "l": str(last_close),
                            "c": str(last_close),
                        },
                    }
                    expected_next_time += time_step
            continue

        for candle in candles:
            candle_time = parse(candle["time"])

            if expected_next_time is None:
                expected_next_time = candle_time
                last_close = float(candle["mid"]["c"])

            while expected_next_time < candle_time:
                yield {
                    "time": isoformat(expected_next_time),
                    "volume": 0,
                    "complete": True,
                    "mid": {
                        "o": str(last_close),
                        "h": str(last_close),
                        "l": str(last_close),
                        "c": str(last_close),
                    },
                }
                expected_next_time += time_step

            yield candle
            last_close = float(candle["mid"]["c"])
            expected_next_time = candle_time + time_step

        last_time = candles[-1].get("time")
        if last_time:
            current = parse(last_time) + time_step
        else:
            current = chunk_end

    if expected_next_time is not None and last_close is not None:
        while expected_next_time < end:
            yield {
                "time": isoformat(expected_next_time),
                "volume": 0,
                "complete": True,
                "mid": {
                    "o": str(last_close),
                    "h": str(last_close),
                    "l": str(last_close),
                    "c": str(last_close),
                },
            }
            expected_next_time += time_step


try:
    from manifold_engine import analyze_bytes  # type: ignore[import-not-found]
except ImportError:
    analyze_bytes = None

logger = logging.getLogger(__name__)

MARKET_DATA_DIR = Path("output/market_data")
UTC = timezone.utc


class ManifoldDataStore:
    """Manages incremental historical data fetching and manifold signature caching."""

    def __init__(self, data_dir: Path = MARKET_DATA_DIR) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        load_oanda_env(ROOT, override=True)
        self.connector = OandaConnector(read_only=True)

    def _get_paths(self, instrument: str, granularity: str = "S5") -> tuple[Path, Path]:
        ins = instrument.upper()
        if granularity == "S5":
            return (
                self.data_dir / f"{ins}.jsonl",
                self.data_dir / f"{ins}.signatures.jsonl",
            )
        else:
            return (
                self.data_dir / f"{ins}_{granularity}.jsonl",
                self.data_dir / f"{ins}_{granularity}.signatures.jsonl",
            )

    def load_candles(
        self, instrument: str, start: datetime, end: datetime, granularity: str = "S5"
    ) -> List[Dict[str, Any]]:
        """Load candles, incrementally fetching missing data from OANDA if needed."""

        candle_path, sig_path = self._get_paths(instrument, granularity)

        # 1. Load existing cache bounds
        existing_candles = []
        if candle_path.exists():
            try:
                with candle_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            existing_candles.append(json.loads(line))
            except Exception as e:
                logger.error(f"Error loading {candle_path}: {e}")

        # 2. Determine bounds
        if not existing_candles:
            cache_start = end
            cache_end = start
        else:
            cache_start = parse(existing_candles[0]["time"])
            cache_end = parse(existing_candles[-1]["time"])

        # 3. Fetch missing data blocks
        new_data_fetched = False
        prefix_candles = []
        suffix_candles = []
        prefix_appended = False
        suffix_appended = False

        step = timedelta(seconds=GRANULARITY_SECONDS.get(granularity, 5) * 5000)

        if start < cache_start:
            logger.info(
                f"Fetching missing historical prefix for {instrument} ({start} -> {cache_start})"
            )
            try:
                prefix_candles = list(
                    iter_candles(
                        self.connector,
                        instrument,
                        granularity,
                        start,
                        cache_start,
                        step,
                    )
                )
                if prefix_candles:
                    new_data_fetched = True
                    prefix_appended = True
            except Exception as e:
                logger.warning(f"Failed to fetch prefix: {e}")

        if end > cache_end:
            # If the cache is completely empty, cache_end == start, so this will fetch [start, end]
            fetch_start = max(start, cache_end)
            logger.info(
                f"Fetching missing historical suffix for {instrument} ({fetch_start} -> {end})"
            )
            try:
                suffix_candles = list(
                    iter_candles(
                        self.connector, instrument, granularity, fetch_start, end, step
                    )
                )
                if suffix_candles:
                    new_data_fetched = True
                    suffix_appended = True
            except Exception as e:
                logger.warning(f"Failed to fetch suffix: {e}")

        # 4. Merge and flush if needed
        if new_data_fetched:
            if prefix_appended:
                merged = prefix_candles + existing_candles + suffix_candles
                # Deduplicate by time just in case of overlap boundaries
                unique = {}
                for c in merged:
                    if "time" in c:
                        unique[c["time"]] = c

                final_list = sorted(unique.values(), key=lambda x: parse(x["time"]))

                logger.info(
                    f"Flushing {len(final_list)} merged candles to {candle_path}"
                )
                # Write back out all merged data
                with candle_path.open("w", encoding="utf-8") as f:
                    for c in final_list:
                        f.write(json.dumps(c) + "\n")

                existing_candles = final_list
                # Sync signatures when data is updated
                self.sync_signatures(instrument, existing_candles, granularity)
            elif suffix_appended and suffix_candles:
                logger.info(f"Appending {len(suffix_candles)} candles to {candle_path}")
                with candle_path.open("a", encoding="utf-8") as f:
                    for c in suffix_candles:
                        f.write(json.dumps(c) + "\n")

                existing_candles = existing_candles + suffix_candles
                # Sync signatures only for newly appended candles
                if not sig_path.exists():
                    logger.info(
                        f"Signature cache missing for {instrument}. Full sync required."
                    )
                    self.sync_signatures(instrument, existing_candles, granularity)
                else:
                    self.sync_signatures(instrument, suffix_candles, granularity)

        elif not sig_path.exists() and existing_candles:
            logger.info(
                f"Signature cache missing for {instrument}. Full sync required."
            )
            self.sync_signatures(instrument, existing_candles, granularity)

        # 5. Filter returning window
        filtered = [c for c in existing_candles if start <= parse(c["time"]) < end]
        return filtered

    def sync_signatures(
        self, instrument: str, candles: List[Dict[str, Any]], granularity: str = "S5"
    ) -> None:
        """Process candles through manifold_engine and cache signatures."""
        if not analyze_bytes:
            logger.warning(
                "manifold_engine C++ extension not found. Skipping signature cache sync."
            )
            return

        candle_path, sig_path = self._get_paths(instrument, granularity)

        # We process signatures using codec equivalent logic (canonical feature encoding)
        from scripts.research.regime_manifold.encoder import (
            MarketManifoldEncoder as MarketManifoldCodec,
            WindowBitEncoder,
            _ema_true_range,
            _bits_to_bytes,
        )
        from scripts.research.regime_manifold.types import Candle
        import statistics

        # Load existing signatures to avoid full recompute
        existing_sigs = {}
        if sig_path.exists():
            with sig_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        s = json.loads(line)
                        existing_sigs[s["time"]] = s

        stride = 1 if granularity == "M1" else 48
        codec = MarketManifoldCodec(
            window_candles=64, stride_candles=stride, atr_period=14
        )

        # Convert raw dicts to Codec Candles
        typed_candles = []
        for c in candles:
            if "mid" not in c:
                continue
            mid = c["mid"]
            ts = parse(c["time"])
            typed_candles.append(
                Candle(
                    timestamp_ms=to_epoch_ms(ts),
                    open=float(mid.get("o", 0.0)),
                    high=float(mid.get("h", 0.0)),
                    low=float(mid.get("l", 0.0)),
                    close=float(mid.get("c", 0.0)),
                    volume=1,
                )
            )
        if len(typed_candles) < codec.window_candles:
            return

        logger.info(f"Syncing manifold signatures for {instrument}...")

        atr_series = _ema_true_range(typed_candles, period=codec.atr_period)
        spread_values = [
            c.spread if c.spread and c.spread > 0 else c.high - c.low
            for c in typed_candles
        ]
        spread_median = statistics.median(spread_values) if spread_values else 0.0
        volume_values = [max(1e-12, c.volume) for c in typed_candles]
        volume_median = statistics.median(volume_values) if volume_values else 1.0

        # Naive sliding window for any missing signatures
        new_sigs_count = 0

        # Open in append mode if we have existing, else write
        mode = "a" if existing_sigs else "w"

        with sig_path.open(mode, encoding="utf-8") as out:
            # We slide manually to write out directly avoiding memory bloat
            for i in range(
                codec.window_candles, len(typed_candles), codec.stride_candles
            ):
                start_idx = i - codec.window_candles
                window = typed_candles[start_idx:i]
                anchor_time = datetime.fromtimestamp(
                    window[-1].timestamp_ms / 1000.0, tz=timezone.utc
                )
                time_str = isoformat(anchor_time)

                if time_str in existing_sigs:
                    continue

                subset_atr = atr_series[start_idx:i]
                prev_close = (
                    typed_candles[start_idx - 1].close
                    if start_idx > 0
                    else window[0].open
                )

                bits, meta = WindowBitEncoder.encode_bits(
                    window,
                    subset_atr,
                    volume_median,
                    spread_median,
                    prev_close=prev_close,
                )
                if not bits:
                    continue

                raw_bytes = _bits_to_bytes(bits)
                raw_json = analyze_bytes(raw_bytes, len(raw_bytes), len(raw_bytes), 3)
                parsed = json.loads(raw_json)
                w = parsed.get("windows", [{}])[0]
                metrics = w.get("metrics", {})

                sig_record = {
                    "time": time_str,
                    "signature": w.get("signature", ""),
                    "hazard": w.get("lambda_hazard", 0.0),
                    "entropy": metrics.get("entropy", 0.0),
                    "coherence": metrics.get("coherence", 0.0),
                    "stability": metrics.get("stability", 0.0),
                }
                out.write(json.dumps(sig_record) + "\n")
                new_sigs_count += 1

        if new_sigs_count > 0:
            logger.info(f"Appended {new_sigs_count} new signatures to {sig_path}")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s :: %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruments", nargs="+", required=True)
    parser.add_argument("--lookback-days", type=int, default=14)
    args = parser.parse_args()

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=args.lookback_days)

    store = ManifoldDataStore()
    for inst in args.instruments:
        logger.info(f"Fetching history for {inst}...")
        store.load_candles(inst, start=start_time, end=end_time, granularity="S5")
