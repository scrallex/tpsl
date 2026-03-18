"""Decoder for reconstructing signals from structural manifolds."""

from typing import Dict, List, Sequence
from .types import (
    BITS_PER_CANDLE,
    EncodedWindow,
    DELTA_BUCKET_DIVISOR,
    ATR_BUCKET_DIVISOR,
    VOLUME_MULTIPLIER_HIGH,
    VOLUME_MULTIPLIER_LOW,
)


class MarketManifoldDecoder:
    """Reconstruct bucket-level signals for inspection."""

    @staticmethod
    def decode_window_bits(window: EncodedWindow) -> List[Dict[str, float]]:
        bit_values = _bytes_to_bits(window.bits, window.bit_length)
        records: List[Dict[str, float]] = []
        idx = 0
        delta_scale = window.codec_meta.get("delta_scale", 1.0)
        atr_scale = window.codec_meta.get("atr_scale", 1.0)
        volume_split = window.codec_meta.get("volume_split", 1.0)

        while idx + BITS_PER_CANDLE <= len(bit_values):
            direction = bit_values[idx]
            delta_bucket = _bits_to_int(bit_values[idx + 1 : idx + 4])
            atr_bucket = _bits_to_int(bit_values[idx + 4 : idx + 6])
            liquidity_flag = bit_values[idx + 6]
            volume_flag = bit_values[idx + 7]
            idx += BITS_PER_CANDLE

            reconstructed_delta = (
                (delta_bucket + 0.5) / DELTA_BUCKET_DIVISOR
            ) * delta_scale
            reconstructed_range = ((atr_bucket + 0.5) / ATR_BUCKET_DIVISOR) * atr_scale
            reconstructed_volume = volume_split * (
                VOLUME_MULTIPLIER_HIGH if volume_flag else VOLUME_MULTIPLIER_LOW
            )

            records.append(
                {
                    "direction": direction,
                    "abs_delta_est": reconstructed_delta,
                    "atr_ratio_est": reconstructed_range,
                    "liquidity_flag": liquidity_flag,
                    "volume_est": reconstructed_volume,
                }
            )
        return records


def _bits_to_int(bits: Sequence[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | (bit & 1)
    return value


def _bytes_to_bits(data: bytes, bit_length: int) -> List[int]:
    bits: List[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
            if len(bits) == bit_length:
                return bits
    return bits[:bit_length]
