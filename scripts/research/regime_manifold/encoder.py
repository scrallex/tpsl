"""Encoder for converting market microstructure windows into structural manifolds."""

import json
import math
import statistics
from typing import Dict, List, Sequence, Tuple

from .types import (
    BITS_PER_CANDLE,
    Candle,
    CanonicalFeatures,
    EncodedWindow,
    MIN_WINDOW_CANDLES,
    MIN_STRIDE_CANDLES,
    EPSILON_ATR,
    EPSILON_VOL,
    EPSILON_ZSCORE,
    MAX_DELTA_BUCKET,
    MAX_ATR_BUCKET,
    BIT_WIDTH_DIR,
    BIT_WIDTH_DELTA,
    BIT_WIDTH_ATR,
    BIT_WIDTH_LIQ,
    BIT_WIDTH_VOL,
)


class FeatureExtractor:
    """Extracts canonical mathematical features from candle windows."""

    @staticmethod
    def extract(
        subset: Sequence[Candle],
        returns: Sequence[float],
        atr_values: Sequence[float],
        volume_split: float,
    ) -> CanonicalFeatures:
        if len(subset) < 2:
            return CanonicalFeatures(0.0, 0.0, 0.0, 0.0, 0.0, "insufficient", 0.0)

        realized_vol = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
        atr_mean = statistics.fmean(atr_values) if atr_values else 0.0
        autocorr = _lag1_autocorr(returns)

        xs = list(range(len(subset)))
        closes = [c.close for c in subset]
        log_closes = [math.log(c) if c > 0 else 0.0 for c in closes]
        slope = _ols_slope(xs, log_closes)
        trend_strength = (slope * len(subset)) / max(EPSILON_ATR, realized_vol)

        volume_avg = statistics.fmean(c.volume for c in subset) if subset else 0.0
        volume_zscore = (volume_avg - volume_split) / max(EPSILON_ZSCORE, volume_split)

        regime, confidence = _classify_regime(
            trend_strength, autocorr, realized_vol, atr_mean
        )

        return CanonicalFeatures(
            realized_vol=realized_vol,
            atr_mean=atr_mean,
            autocorr=autocorr,
            trend_strength=trend_strength,
            volume_zscore=volume_zscore,
            regime=regime,
            regime_confidence=confidence,
        )


class WindowBitEncoder:
    """Encodes a sequence of candles into a bitwise representation."""

    @staticmethod
    def encode_bits(
        subset: Sequence[Candle],
        atr_values: Sequence[float],
        volume_split: float,
        spread_split: float,
        *,
        prev_close: float,
    ) -> Tuple[List[int], Dict[str, float]]:
        bits: List[int] = []
        last_close = prev_close
        for candle, atr in zip(subset, atr_values):
            atr_safe = max(atr, EPSILON_ATR)
            delta = candle.close - last_close
            direction = 1 if delta >= 0 else 0
            abs_ratio = min(1.0, abs(delta) / atr_safe)
            delta_bucket = min(
                MAX_DELTA_BUCKET, int(round(abs_ratio * MAX_DELTA_BUCKET))
            )

            tr = max(
                candle.high - candle.low,
                abs(candle.high - last_close),
                abs(candle.low - last_close),
            )
            tr_ratio = min(1.0, tr / max(atr_safe, EPSILON_ATR))
            atr_bucket = min(MAX_ATR_BUCKET, int(round(tr_ratio * MAX_ATR_BUCKET)))

            spread_value = (
                candle.spread
                if candle.spread and candle.spread > 0
                else candle.high - candle.low
            )
            liquidity_flag = (
                1 if spread_split <= 0 or spread_value <= spread_split else 0
            )

            volume_flag = 1 if candle.volume >= volume_split else 0

            bits.extend(_int_to_bits(direction, BIT_WIDTH_DIR))
            bits.extend(_int_to_bits(delta_bucket, BIT_WIDTH_DELTA))
            bits.extend(_int_to_bits(atr_bucket, BIT_WIDTH_ATR))
            bits.extend(_int_to_bits(liquidity_flag, BIT_WIDTH_LIQ))
            bits.extend(_int_to_bits(volume_flag, BIT_WIDTH_VOL))

            last_close = candle.close

        meta = {
            "delta_scale": float(statistics.fmean(atr_values)) if atr_values else 1.0,
            "atr_scale": float(statistics.fmean(atr_values)) if atr_values else 1.0,
            "volume_split": float(volume_split),
            "spread_split": float(spread_split),
        }
        return bits, meta


class StructuralAnalyzer:
    """Interfaces with the Manifold Engine to analyze bitwise constraints."""

    @staticmethod
    def analyze(bit_bytes: bytes) -> Tuple[str, Dict[str, float]]:
        import manifold_engine

        json_str = manifold_engine.analyze_bytes(
            bit_bytes, len(bit_bytes), len(bit_bytes), 3
        )
        parsed = json.loads(json_str)
        w = parsed.get("windows", [{}])[0]
        metrics = w.get("metrics", {})
        metrics["hazard"] = w.get("lambda_hazard", 0.0)
        metrics["rupture"] = metrics.get("rupture", 0.0)
        signature = w.get("signature", "")
        return signature, metrics


class MarketManifoldEncoder:
    """Convert rolling candle windows into reversible structural manifolds."""

    def __init__(
        self,
        *,
        window_candles: int = 64,
        stride_candles: int = 16,
        atr_period: int = 14,
    ) -> None:
        if window_candles < MIN_WINDOW_CANDLES:
            raise ValueError(f"window_candles must be >= {MIN_WINDOW_CANDLES}")
        if stride_candles < MIN_STRIDE_CANDLES:
            raise ValueError(f"stride_candles must be >= {MIN_STRIDE_CANDLES}")
        self.window_candles = window_candles
        self.stride_candles = stride_candles
        self.atr_period = atr_period

    def encode(
        self,
        candles: Sequence[Candle],
        *,
        instrument: str,
        return_only_latest: bool = False,
        align_latest_to_stride: bool = True,
    ) -> List[EncodedWindow]:
        if len(candles) < self.window_candles:
            return []

        atr_series = _ema_true_range(candles, period=self.atr_period)
        log_returns = _log_returns(candles)
        spread_values = [
            c.spread if c.spread and c.spread > 0 else c.high - c.low for c in candles
        ]
        spread_median = statistics.median(spread_values) if spread_values else 0.0
        volume_values = [max(EPSILON_VOL, c.volume) for c in candles]
        volume_median = statistics.median(volume_values) if volume_values else 1.0

        windows: List[EncodedWindow] = []

        start = 0
        if return_only_latest:
            # Live services may want the most recent rolling window, while
            # historical derivation can still snap to stride boundaries.
            max_start = len(candles) - self.window_candles
            if max_start >= 0:
                if align_latest_to_stride:
                    start = (max_start // self.stride_candles) * self.stride_candles
                else:
                    start = max_start

        while start + self.window_candles <= len(candles):
            end = start + self.window_candles
            subset = candles[start:end]
            subset_atr = atr_series[start:end]
            subset_returns = log_returns[start + 1 : end]

            bits, meta = WindowBitEncoder.encode_bits(
                subset,
                subset_atr,
                volume_median,
                spread_median,
                prev_close=candles[start - 1].close if start > 0 else subset[0].open,
            )

            bit_bytes = _bits_to_bytes(bits)
            signature, metrics = StructuralAnalyzer.analyze(bit_bytes)

            canonical = FeatureExtractor.extract(
                subset, subset_returns, subset_atr, volume_median
            )

            windows.append(
                EncodedWindow(
                    instrument=instrument,
                    start_ms=subset[0].timestamp_ms,
                    end_ms=subset[-1].timestamp_ms,
                    bits=bit_bytes,
                    bit_length=len(bits),
                    signature=signature,
                    metrics=metrics,
                    canonical=canonical,
                    codec_meta=meta,
                )
            )
            start += self.stride_candles
        return windows


def _ema_true_range(candles: Sequence[Candle], *, period: int) -> List[float]:
    if not candles:
        return []
    alpha = 2.0 / (period + 1.0)
    atr: List[float] = []
    prev_close = candles[0].close
    ema = (candles[0].high - candles[0].low) / max(1e-8, prev_close)
    for candle in candles:
        tr = max(
            candle.high - candle.low,
            abs(candle.high - prev_close),
            abs(candle.low - prev_close),
        ) / max(EPSILON_ATR, prev_close)
        ema = (alpha * tr) + (1 - alpha) * ema
        atr.append(max(ema, EPSILON_ATR))
        prev_close = candle.close
    return atr


def _log_returns(candles: Sequence[Candle]) -> List[float]:
    rets: List[float] = [0.0]
    for idx in range(1, len(candles)):
        prev = candles[idx - 1].close
        curr = candles[idx].close
        if prev <= 0 or curr <= 0:
            rets.append(0.0)
            continue
        rets.append(math.log(curr / prev))
    return rets


def _int_to_bits(value: int, width: int) -> List[int]:
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def _bits_to_bytes(bits: Sequence[int]) -> bytes:
    buf = bytearray()
    for idx in range(0, len(bits), 8):
        chunk = bits[idx : idx + 8]
        value = 0
        for bit in chunk:
            value = (value << 1) | (bit & 1)
        value <<= max(0, 8 - len(chunk))
        buf.append(value & 0xFF)
    return bytes(buf)


def _lag1_autocorr(series: Sequence[float]) -> float:
    if len(series) < 2:
        return 0.0
    mean = statistics.fmean(series)
    num = 0.0
    denom = 0.0
    for idx in range(1, len(series)):
        x0 = series[idx - 1] - mean
        x1 = series[idx] - mean
        num += x0 * x1
        denom += x0 * x0
    return num / denom if denom else 0.0


def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom = sum((x - mean_x) ** 2 for x in xs)
    return num / denom if denom else 0.0


def _classify_regime(
    trend_strength: float,
    autocorr: float,
    realized_vol: float,
    atr_mean: float,
) -> Tuple[str, float]:
    if atr_mean <= 0:
        atr_mean = 1e-6
    vol_ratio = realized_vol / max(1e-6, atr_mean)
    if trend_strength >= 1.5 and autocorr >= 0.1:
        return "trend_bull", min(1.0, trend_strength / 3.0)
    if trend_strength <= -1.5 and autocorr >= 0.1:
        return "trend_bear", min(1.0, abs(trend_strength) / 3.0)
    if vol_ratio < 0.75 and abs(autocorr) < 0.25:
        return "mean_revert", 1.0 - vol_ratio
    if vol_ratio >= 1.5 and abs(autocorr) < 0.1:
        return "chaotic", min(1.0, vol_ratio / 3.0)
    return "neutral", 0.5
