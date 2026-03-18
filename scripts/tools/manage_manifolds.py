#!/usr/bin/env python3
"""
Chronos Manager: The Centralized Manifold Data Orchestrator.
Maintains perfect geometric time series alignment using zero-tick padding.
Also validates cache integrity to guarantee sequential timeseries.
"""

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from scripts.research.data_store import ManifoldDataStore, parse

logger = logging.getLogger("chronos-manager")


def sync_all(
    instruments: list[str], lookback_days: int, granularity: str = "S5"
) -> None:
    store = ManifoldDataStore()
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=lookback_days)

    for inst in instruments:
        logger.info(f"Syncing and padding {granularity} history for {inst}...")
        # load_candles internally leverages the new padding logic in iter_candles
        store.load_candles(
            inst, start=start_time, end=end_time, granularity=granularity
        )

        # Verify integrity
        candle_path, sig_path = store._get_paths(inst, granularity)
        verify_integrity(inst, candle_path, sig_path, granularity)


def verify_integrity(
    instrument: str, candle_path: Path, sig_path: Path, granularity: str = "S5"
) -> None:
    logger.info(f"Verifying cache integrity for {instrument}...")
    if not candle_path.exists():
        logger.warning(f"No candle data found for {instrument}")
        return

    # Check for gaps in candles
    last_time = None
    step_seconds = 60 if granularity == "M1" else 5
    expected_step = timedelta(seconds=step_seconds)
    gaps_found = 0
    total_candles = 0

    with candle_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total_candles += 1
            c = json.loads(line)
            t = parse(c["time"])
            if last_time is not None:
                diff = t - last_time
                if diff != expected_step:
                    logger.debug(f"Gap detected or misalignment at {t} (diff {diff})")
                    gaps_found += 1
            last_time = t

    if gaps_found > 0:
        logger.warning(
            f"Integrity check failed: {gaps_found} gaps or misalignments found in {candle_path}"
        )
    else:
        logger.info(
            f"Integrity check passed: {total_candles} perfectly sequenced {granularity} candles."
        )

    if not sig_path.exists():
        logger.warning(f"No signature data found for {instrument}")
        return

    # Check for gaps in signatures (48 candle stride)
    last_sig_time = None
    expected_stride = timedelta(seconds=step_seconds * 48)
    sig_gaps = 0
    total_sigs = 0

    with sig_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total_sigs += 1
            c = json.loads(line)
            t = parse(c["time"])
            if last_sig_time is not None:
                diff = t - last_sig_time
                if diff != expected_stride:
                    sig_gaps += 1
            last_sig_time = t

    if sig_gaps > 0:
        logger.warning(
            f"Integrity check failed: {sig_gaps} signature stride misalignments found in {sig_path}"
        )
    else:
        logger.info(
            f"Integrity check passed: {total_sigs} perfectly sequenced manifolds."
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s :: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Centralized Manifold Chronos Manager")
    parser.add_argument(
        "--sync-all", action="store_true", help="Sync all active instruments"
    )
    parser.add_argument("--instruments", nargs="+", help="Specific instruments to sync")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=180,
        help="Days of history to manage",
    )
    parser.add_argument(
        "--granularity",
        type=str,
        default="S5",
        help="Data granularity to ingest (S5 or M1)",
    )
    args = parser.parse_args()

    active_instruments = []
    if args.instruments:
        active_instruments = args.instruments
    elif args.sync_all:
        active_instruments = [
            "EUR_USD",
            "GBP_USD",
            "USD_JPY",
            "USD_CAD",
            "USD_CHF",
            "NZD_USD",
            "AUD_USD",
        ]
    else:
        parser.error("Must specify either --sync-all or --instruments")

    sync_all(active_instruments, args.lookback_days, args.granularity)
