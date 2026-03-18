import sys
from pathlib import Path

from scripts.research.regime_manifold.encoder import (
    MarketManifoldEncoder as MarketManifoldCodec,
)
from scripts.research.regime_manifold.types import Candle

codec = MarketManifoldCodec(window_candles=8, stride_candles=1)
candles = [
    Candle(timestamp_ms=i * 1000, open=1.0, high=1.1, low=0.9, close=1.05, volume=100)
    for i in range(10)
]

try:
    print("Testing Codec Encode...")
    windows = codec.encode(candles, instrument="EUR_USD")
    for w in windows:
        print(
            f"Signature: {w.signature} | Hazard: {w.metrics.get('hazard')} | Entropy: {w.metrics.get('entropy')}"
        )
except Exception as e:
    import traceback

    traceback.print_exc()
