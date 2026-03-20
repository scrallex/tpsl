"""Synthetic gate derivation using MarketManifoldCodec for backtest fidelity."""

from __future__ import annotations

from scripts.trading.candle_utils import to_epoch_ms

import json
import math
from bisect import insort
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ...tools.time_utils import parse_utc_time
from scripts.research.regime_manifold.encoder import (
    MarketManifoldEncoder as MarketManifoldCodec,
)
from scripts.research.regime_manifold.types import Candle
from scripts.trading.portfolio_manager import StrategyInstrument

UTC = timezone.utc

import logging

logger = logging.getLogger(__name__)

SQUEEZE_ENTROPY_CEILING = 1.1
SQUEEZE_HAZARD_CEILING = 0.25
DEFAULT_REGIME_ADMIT_REGIMES = ("trend_bull", "trend_bear")


class HazardCalibrator:
    """Rolling percentile tracker used to adapt hazard guardrails per instrument."""

    def __init__(self, percentile: float = 0.8, max_samples: int = 2048) -> None:
        self.percentile = min(max(percentile, 0.05), 0.99)
        self.max_samples = max_samples
        self._samples: List[float] = []

    def update(self, value: float) -> None:
        insort(self._samples, value)
        if len(self._samples) > self.max_samples:
            self._samples.pop(0)

    def threshold(self) -> float:
        if not self._samples:
            return 1.0
        idx = int(self.percentile * (len(self._samples) - 1))
        return self._samples[idx]


def _regime_direction_for(regime: str) -> str:
    if "bull" in regime:
        return "BUY"
    if "bear" in regime:
        return "SELL"
    return "FLAT"


def _normalize_to_codec_candles(candles: Sequence[Any]) -> List[Candle]:
    out: List[Candle] = []

    for row in candles:
        ts = None
        o = h = l = c = v = 0.0

        # Determine internal structure
        if hasattr(row, "time") and hasattr(row, "mid"):
            ts = getattr(row, "time")
            try:
                mid = getattr(row, "mid") or {}
                if isinstance(mid, dict):
                    o = float(mid.get("o", 0))
                    h = float(mid.get("h", 0))
                    l = float(mid.get("l", 0))
                    c = float(mid.get("c", 0))
                else:
                    o = float(getattr(row, "open", 0))
                    h = float(getattr(row, "high", 0))
                    l = float(getattr(row, "low", 0))
                    c = float(getattr(row, "close", 0))
                v = float(getattr(row, "volume", 1))
            except Exception:
                continue

        elif isinstance(row, dict):
            ts = row.get("time")
            mid = row.get("mid", {})
            if not mid and "open" in row:
                mid = row
            try:
                o = float(mid.get("o") or mid.get("open") or 0)
                h = float(mid.get("h") or mid.get("high") or 0)
                l = float(mid.get("l") or mid.get("low") or 0)
                c = float(mid.get("c") or mid.get("close") or 0)
                v = float(row.get("volume") or row.get("v") or 1)
            except (ValueError, TypeError):
                continue

        if ts is None:
            continue

        if not isinstance(ts, datetime):
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except Exception:
                continue

        if h < l:
            h, l = l, h

        out.append(
            Candle(
                timestamp_ms=to_epoch_ms(ts),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
            )
        )

    out.sort(key=lambda x: x.timestamp_ms)
    return out


def _load_candles(
    instrument: str,
    *,
    start: datetime,
    end: datetime,
    granularity: str = "S5",
    cache_path: Optional[Path] = None,
) -> List[Candle]:
    if cache_path and cache_path.exists():
        cached_rows: List[Dict[str, Any]] = []
        try:
            with cache_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    ts_raw = row.get("time")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except Exception:
                        continue
                    if start <= ts <= end:
                        cached_rows.append(row)
        except Exception:
            cached_rows = []

        if cached_rows:
            return _normalize_to_codec_candles(cached_rows)

    payload = []
    if cache_path:
        try:
            from scripts.research.data_store import ManifoldDataStore

            store = ManifoldDataStore()
            # The cache_path argument is not supported by load_candles directly in the same way, but DataStore handles caching internally.
            payload = store.load_candles(instrument, start, end, granularity)
        except Exception:
            pass

    if not payload:
        try:
            from scripts.research.data_store import ManifoldDataStore

            store = ManifoldDataStore()
            payload = store.load_candles(instrument, start, end, granularity)
        except Exception:
            payload = []

    return _normalize_to_codec_candles(payload)


def derive_signals(
    instrument: str,
    start: datetime | str,
    end: datetime | str,
    *,
    candles: Optional[Sequence[Any]] = None,
    profile: Optional[StrategyInstrument] = None,
    granularity: str = "S5",
    cache_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Generate Multi-Modal Gates:
    1. Trend Sniper (High Haz Breakout)
    2. Squeeze Alpha (Low Ent -> Haz Expansion)
    3. Mean Reversion (Extreme Haz Exhaustion)
    """

    start_dt = parse_utc_time(start)
    end_dt = parse_utc_time(end)

    if candles is None:
        codec_candles = _load_candles(
            instrument,
            start=start_dt,
            end=end_dt,
            granularity=granularity,
            cache_path=cache_path,
        )
    else:
        codec_candles = _normalize_to_codec_candles(candles)

    if not codec_candles or len(codec_candles) < 64:
        logger.warning("No codec candles normalized!")
        return []

    # Initialize Codec & Calibrator
    codec = MarketManifoldCodec(window_candles=64, stride_candles=16, atr_period=14)
    calibrator = HazardCalibrator(percentile=0.8)

    encoded_windows = codec.encode(codec_candles, instrument=instrument)
    events: List[Dict[str, Any]] = []

    # State for Squeeze Tracking
    squeeze_active = False
    squeeze_start_ts = 0

    # Repetition tracking
    last_regime: Optional[str] = None
    repetitions: int = 0
    prev_st: float = -1.0

    for window in encoded_windows:
        regime = window.canonical.regime

        if regime == last_regime:
            repetitions += 1
        else:
            repetitions = 1
            last_regime = regime

        hazard_value = window.metrics["hazard"]
        coherence = window.metrics["coherence"]
        stability = window.metrics["stability"]
        entropy = window.metrics["entropy"]

        calibrator.update(hazard_value)

        # --- LOGIC ENGINE ---

        signal_type = None
        direction = "FLAT"
        reasons = []

        # 1. Update Squeeze State
        # Squeeze starts when market is dead silent
        if not squeeze_active:
            if (
                entropy < SQUEEZE_ENTROPY_CEILING
                and hazard_value < SQUEEZE_HAZARD_CEILING
            ):
                squeeze_active = True
                squeeze_start_ts = window.end_ms
        else:
            # Squeeze breaks if Entropy spikes too high without a move (False Signal)
            if entropy > 1.6 and hazard_value < 0.3:
                squeeze_active = False

            # Squeeze FIRES if Hazard expands aggressively (Expansion)
            elif hazard_value >= 0.40:
                signal_type = "SQUEEZE_BREAKOUT"
                # Direction follows the regime breakout
                if "bull" in regime:
                    direction = "BUY"
                elif "bear" in regime:
                    direction = "SELL"
                squeeze_active = False  # Reset after fire

        # 2. Structural Extension (Replaces strict Sniper/Reversion)
        if not signal_type and hazard_value >= 0.70:
            if "bull" in regime:
                direction = "BUY"
            elif "bear" in regime:
                direction = "SELL"
            signal_type = "STRUCTURAL_EXTENSION"

        # --- GATE VALIDATION ---

        admit = False

        if signal_type:
            # Apply filters based on Signal Type
            if signal_type == "SQUEEZE_BREAKOUT":
                # Squeezes need low entropy validation to ensure it's not just noise
                if entropy <= 1.5:
                    admit = True

            elif signal_type == "STRUCTURAL_EXTENSION":
                # Provide a loose filter so GPU param sweep can explore boundaries
                admit = True

        if admit:
            # Calculate Structural Tension for sizing
            k_decay = 1.0
            st_score = repetitions * coherence * math.exp(-k_decay * hazard_value)

            is_st_peak = False
            if prev_st > 0.0 and st_score < prev_st:
                is_st_peak = True
            prev_st = st_score

            events.append(
                {
                    "instrument": instrument.upper(),
                    "admit": 1,
                    "direction": direction,
                    "lambda": round(max(0.0, min(1.0, hazard_value * 0.1)), 6),
                    "hazard": hazard_value,
                    "structural_tension": st_score,
                    "st_peak": is_st_peak,
                    "regime": regime,
                    "regime_confidence": window.canonical.regime_confidence,
                    "components": window.metrics,
                    "repetitions": repetitions,
                    "repetition_count": repetitions,
                    "ts_ms": window.end_ms,
                    "source": (
                        signal_type.lower() if signal_type else "unknown"
                    ),  # Tag the source!
                    "reasons": [],
                    "status": "active",
                    "bundle_hits": [],
                }
            )

    # Logging summary
    logger.info(
        f"Signal derivation complete. Generated {len(events)} valid multi-modal signals."
    )
    return events


def derive_regime_manifold_gates(
    instrument: str,
    start: datetime | str,
    end: datetime | str,
    *,
    candles: Optional[Sequence[Any]] = None,
    granularity: str = "S5",
    cache_path: Optional[Path] = None,
    window_candles: int = 64,
    stride_candles: int = 1,
    atr_period: int = 14,
    hazard_percentile: float = 0.8,
    signature_retention_minutes: int = 60,
    admit_regimes: Sequence[str] = DEFAULT_REGIME_ADMIT_REGIMES,
    min_confidence: float = 0.55,
    lambda_scale: float = 0.1,
    hazard_cap: Optional[float] = 1.0,
) -> List[Dict[str, Any]]:
    """Derive dense historical gates that mirror the live rolling regime service."""

    start_dt = parse_utc_time(start)
    end_dt = parse_utc_time(end)

    if candles is None:
        codec_candles = _load_candles(
            instrument,
            start=start_dt,
            end=end_dt,
            granularity=granularity,
            cache_path=cache_path,
        )
    else:
        codec_candles = _normalize_to_codec_candles(candles)

    if not codec_candles or len(codec_candles) < window_candles:
        logger.warning("No live-parity regime manifold candles normalized!")
        return []

    codec = MarketManifoldCodec(
        window_candles=window_candles,
        stride_candles=stride_candles,
        atr_period=atr_period,
    )
    calibrator = HazardCalibrator(percentile=hazard_percentile)
    encoded_windows = codec.encode(codec_candles, instrument=instrument)
    signature_history: dict[str, deque[int]] = defaultdict(deque)
    retention_ms = max(1, int(signature_retention_minutes)) * 60 * 1000
    admit_labels = {str(label).strip() for label in admit_regimes if str(label).strip()}

    events: List[Dict[str, Any]] = []
    for window in encoded_windows:
        hazard_value = float(window.metrics["hazard"])
        calibrator.update(hazard_value)

        hazard_threshold = calibrator.threshold()
        if hazard_cap is not None:
            hazard_threshold = min(hazard_threshold, float(hazard_cap))

        history = signature_history[window.signature]
        history.append(window.end_ms)
        while history and (window.end_ms - history[0]) > retention_ms:
            history.popleft()
        repetitions = len(history)

        reasons: List[str] = []
        if hazard_value > hazard_threshold:
            reasons.append("hazard_exceeds_adaptive_threshold")
            if hazard_value > hazard_threshold * 1.5:
                reasons.append("hazard_fallback_requested")
        if admit_labels and window.canonical.regime not in admit_labels:
            reasons.append("regime_filtered")
        if min_confidence > 0.0 and window.canonical.regime_confidence < min_confidence:
            reasons.append("regime_confidence_low")

        lambda_value = max(0.0, min(1.0, hazard_value * lambda_scale))
        codec_meta = dict(window.codec_meta)
        codec_meta["window_candles"] = int(window_candles)
        codec_meta["stride_candles"] = int(stride_candles)
        codec_meta["atr_period"] = int(atr_period)

        events.append(
            {
                "instrument": instrument.upper(),
                "ts_ms": window.end_ms,
                "admit": int(len(reasons) == 0),
                "direction": _regime_direction_for(window.canonical.regime),
                "lambda": lambda_value,
                "hazard": hazard_value,
                "hazard_threshold": hazard_threshold,
                "repetitions": repetitions,
                "repetition_count": repetitions,
                "structure": {
                    **window.metrics,
                    "signature": window.signature,
                    "hazard": hazard_value,
                    "lambda_scaled": lambda_value,
                    "hazard_threshold": hazard_threshold,
                },
                "regime": {
                    "label": window.canonical.regime,
                    "confidence": window.canonical.regime_confidence,
                    "realized_vol": window.canonical.realized_vol,
                    "atr_mean": window.canonical.atr_mean,
                    "autocorr": window.canonical.autocorr,
                    "trend_strength": window.canonical.trend_strength,
                    "volume_zscore": window.canonical.volume_zscore,
                },
                "components": {
                    "bits_b64": window.bits_b64(),
                    "codec_meta": codec_meta,
                },
                "reasons": reasons,
                "status": "active",
                "bundle_hits": [],
                "source": "regime_manifold",
            }
        )

    logger.info(
        "Live-parity regime gate derivation complete. Generated %d windows.",
        len(events),
    )
    return events


__all__ = ["derive_signals", "derive_regime_manifold_gates"]
