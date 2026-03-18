#!/usr/bin/env python3
import json
import logging
from pathlib import Path
import sys


from scripts.research.simulator.synthetic_m1 import stream_synthetic_m1
from scripts.research.regime_manifold.encoder import MarketManifoldEncoder
from scripts.research.regime_manifold.types import Candle
from scripts.trading.candle_utils import to_epoch_ms

logging.basicConfig(level=logging.INFO, format="%(levelname)s :: %(message)s")
logger = logging.getLogger("synthetic-m1-builder")

PAIRS = ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY", "USD_CAD", "NZD_USD", "USD_CHF"]


def build_dataset_for_pair(pair: str):
    s5_path = Path(f"output/market_data/{pair}.jsonl")
    sig_out_path = Path(f"output/market_data/{pair}_Synthetic_M1.signatures.jsonl")
    candle_out_path = Path(f"output/market_data/{pair}_Synthetic_M1.jsonl")

    if not s5_path.exists():
        logger.warning(f"S5 source {s5_path} not found. Skipping {pair}.")
        return

    logger.info(f"[{pair}] Generating Synthetic M1 Topology from S5 stream...")

    # Step 1: Write the synthetic candles to a new JSONL, while collecting into memory for encoder
    synthetic_count = 0
    synthetic_candles = []

    with candle_out_path.open("w", encoding="utf-8") as out_f:
        for synth_candle in stream_synthetic_m1(s5_path):
            out_f.write(json.dumps(synth_candle) + "\n")

            # Map into the native code Candle object
            c = Candle(
                timestamp_ms=to_epoch_ms(synth_candle["time"]),
                open=float(synth_candle["mid"]["o"]),
                high=float(synth_candle["mid"]["h"]),
                low=float(synth_candle["mid"]["l"]),
                close=float(synth_candle["mid"]["c"]),
                volume=float(synth_candle["volume"]),
            )
            synthetic_candles.append(c)

            synthetic_count += 1
            if synthetic_count % 100000 == 0:
                logger.info(
                    f"[{pair}] Generated {synthetic_count} synthetic M1 structures..."
                )

    logger.info(
        f"[{pair}] Completed generating {synthetic_count} continuous M1 candles."
    )

    # Step 2: Push the new synthetic structures through the Manifold Engine Encoder
    logger.info(
        f"[{pair}] Encoding Synthetic Signatures (c/s/e) via Manifold Engine..."
    )
    encoder = MarketManifoldEncoder(window_candles=64, stride_candles=1)

    # Process the entire stream into windows
    windows = encoder.encode(synthetic_candles, instrument=pair)

    with sig_out_path.open("w", encoding="utf-8") as f:
        for w in windows:
            f.write(json.dumps(w.to_json()) + "\n")

    logger.info(
        f"[{pair}] Signature Encoding complete: {len(windows)} structural windows generated.\n"
    )


if __name__ == "__main__":
    print("=========================================================")
    print("--- BUILDING SYNTHETIC M1 TOPOLOGY & MANIFOLD SIGNATURES ---")
    print("=========================================================")

    for p in PAIRS:
        build_dataset_for_pair(p)
