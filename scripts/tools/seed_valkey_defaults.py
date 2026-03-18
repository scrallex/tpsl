#!/usr/bin/env python3
"""
Seed Valkey Defaults
Initializes the Valkey cache with default values (like the kill switch)
and prefetches historical candles from OANDA so the frontend charts
and Backend Regime Engine are populated immediately, especially useful during weekend restarts.
"""

import logging
import json
import os
import sys
from pathlib import Path

# Add the project root to the Python path if necessary
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from scripts.trading_service import TradingService
from scripts.tools.stream_candles import _normalise_candle
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    logger.info("Seeding Valkey defaults...")
    
    # Get enabled pairs from environment
    pairs_env = os.getenv("HOTBAND_PAIRS", "EUR_USD,USD_JPY,AUD_USD,USD_CHF,NZD_USD,GBP_USD,USD_CAD")
    enabled_pairs = [p.strip() for p in pairs_env.split(",") if p.strip()]
    
    redis_url = os.getenv("VALKEY_URL", "redis://valkey:6379/0")
    client = redis.from_url(redis_url)
    
    try:
        # Initialize the TradingService in read-only mode to access OANDA
        logger.info(f"Initializing TradingService for pairs: {enabled_pairs}")
        service = TradingService(read_only=True, enabled_pairs=enabled_pairs)
        connector = service.oanda
        
        # 1. Ensure kill switch is engaged by default for safety
        service.set_kill_switch(True)
        logger.info("Kill switch seeded to True (Engaged) for safety.")
        
        # Helper to fetch and seed
        def fetch_and_seed(granularity, count):
            logger.info(f"Fetching {count} {granularity} candles from OANDA to seed Valkey...")
            for instrument in enabled_pairs:
                try:
                    candles = connector.get_candles(instrument, granularity=granularity, count=count)
                    if not candles:
                        logger.warning(f"No {granularity} candles returned for {instrument}")
                        continue
                        
                    key = f"md:candles:{instrument}:{granularity}"
                    pipe = client.pipeline()
                    inserted = 0
                    for candle in candles:
                        record = _normalise_candle(candle)
                        if not record:
                            continue
                        ts_ms = record["t"]
                        blob = json.dumps(record, separators=(",", ":"))
                        pipe.zadd(key, {blob: ts_ms})
                        inserted += 1
                        
                    pipe.execute()
                    logger.info(f"Seeded {inserted} {granularity} candles for {instrument} into Valkey.")
                except Exception as e:
                    logger.error(f"Failed to seed {granularity} candles for {instrument}: {e}")

        # 2. Fetch M5 candles to seed the frontend charts natively
        fetch_and_seed("M5", 200)
        
        # 3. Fetch S5 candles for the Regime Structural Engine (4 hours)
        fetch_and_seed("S5", 2880)
        
        logger.info("Historical candles seeded successfully.")
        logger.info("Valkey seeding completed.")
        
    except Exception as e:
        logger.error(f"Failed to seed Valkey defaults: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
