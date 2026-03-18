"""Data types for regime manifold encoding and decoding."""

import base64
from dataclasses import dataclass
from typing import Dict, Optional

BITS_PER_CANDLE = 8

MIN_WINDOW_CANDLES = 8
MIN_STRIDE_CANDLES = 1
EPSILON_ATR = 1e-8
EPSILON_VOL = 1e-12
EPSILON_ZSCORE = 1e-6

BIT_WIDTH_DIR = 1
BIT_WIDTH_DELTA = 3
BIT_WIDTH_ATR = 2
BIT_WIDTH_LIQ = 1
BIT_WIDTH_VOL = 1

MAX_DELTA_BUCKET = (1 << BIT_WIDTH_DELTA) - 1
MAX_ATR_BUCKET = (1 << BIT_WIDTH_ATR) - 1

DELTA_BUCKET_DIVISOR = 8.0
ATR_BUCKET_DIVISOR = 4.0

VOLUME_MULTIPLIER_HIGH = 1.25
VOLUME_MULTIPLIER_LOW = 0.75


@dataclass
class Candle:
    """Minimal candle representation used by the codec."""

    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: Optional[float] = None


@dataclass
class CanonicalFeatures:
    realized_vol: float
    atr_mean: float
    autocorr: float
    trend_strength: float
    volume_zscore: float
    regime: str
    regime_confidence: float


@dataclass
class EncodedWindow:
    instrument: str
    start_ms: int
    end_ms: int
    bits: bytes
    bit_length: int
    signature: str
    metrics: Dict[str, float]
    canonical: CanonicalFeatures
    codec_meta: Dict[str, float]

    def bits_b64(self) -> str:
        return base64.b64encode(self.bits).decode("ascii")

    def to_json(self) -> Dict[str, object]:
        return {
            "instrument": self.instrument,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "bits_b64": self.bits_b64(),
            "bit_length": self.bit_length,
            "signature": self.signature,
            "metrics": self.metrics,
            "canonical": self.canonical.__dict__,
            "codec_meta": self.codec_meta,
        }
