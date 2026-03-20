#!/usr/bin/env python3
"""Streaming service that writes regime-aware manifold gates into Valkey."""
from __future__ import annotations


import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

import redis
from prometheus_client import Counter, Gauge, Summary, start_http_server

from scripts.research.regime_manifold.encoder import (
    MarketManifoldEncoder as MarketManifoldCodec,
)
from scripts.trading.candle_utils import to_epoch_ms
from scripts.trading.candle_parser import candle_from_payload
from scripts.trading.market_types import Candle
from scripts.trading.portfolio_manager import StrategyProfile

logger = logging.getLogger("regime-manifold-service")


class _NoopMetric:
    """A minimal stub implementation of Prometheus metric endpoints.
    Allows disabling real metrics without altering code flow."""

    def labels(self, **kwargs):  # pragma: no cover - simple noop
        return self

    def inc(self, *args, **kwargs):  # pragma: no cover
        return None

    def set(self, *args, **kwargs):  # pragma: no cover
        return None

    def time(self):  # pragma: no cover
        class _Timer:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Timer()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _now_ms() -> int:
    return to_epoch_ms(datetime.now(timezone.utc))


class HazardCalibrator:
    """Rolling percentile tracker used to adapt hazard guardrails per instrument."""

    def __init__(self, percentile: float = 0.8, max_samples: int = 2048) -> None:
        from bisect import insort

        self.percentile = min(max(percentile, 0.05), 0.99)
        self.max_samples = max_samples
        self._samples: List[float] = []
        self._insort = insort

    def update(self, value: float) -> None:
        self._insort(self._samples, value)
        if len(self._samples) > self.max_samples:
            self._samples.pop(0)

    def threshold(self) -> float:
        if not self._samples:
            return 1.0
        idx = int(self.percentile * (len(self._samples) - 1))
        return self._samples[idx]


@dataclass
class ServiceConfig:
    instruments: Sequence[str]
    redis_url: str
    lookback_minutes: int
    window_candles: int
    stride_candles: int
    atr_period: int
    loop_seconds: float
    signature_retention_minutes: int
    hazard_percentile: float
    admit_regimes: Sequence[str]
    min_confidence: float
    gate_ttl_seconds: int
    prom_port: int
    lambda_scale: float


class RegimeManifoldService:
    """Daemon responsible for encoding live market feeds into structural gates.

    Reads arriving S5 candles, computes Lyapunov-analog entropy and coherence
    regimes via `MarketManifoldCodec`, and persists streaming evaluation state
    to Valkey keys for synchronous evaluation by execution engines.
    """

    def __init__(self, config: ServiceConfig, profile: StrategyProfile) -> None:
        self.cfg = config
        self.profile = profile
        self.redis = redis.from_url(config.redis_url)
        self.codec = MarketManifoldCodec(
            window_candles=config.window_candles,
            stride_candles=config.stride_candles,
            atr_period=config.atr_period,
        )
        self._signature_history: Dict[str, Dict[str, Deque[int]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        self._hazard_calibrators: Dict[str, HazardCalibrator] = {
            inst: HazardCalibrator(percentile=config.hazard_percentile)
            for inst in config.instruments
        }
        self._last_emitted_ts_ms: Dict[str, int] = {}

        self._stop = False
        disable_metrics = str(os.getenv("DISABLE_REGIME_METRICS", "0")).lower() in (
            "1",
            "true",
        )
        self._metrics_enabled = not disable_metrics
        if self._metrics_enabled:
            self._metrics_setup()
        else:
            noop = _NoopMetric()
            self.metric_payloads = noop
            self.metric_hazard = noop
            self.metric_age = noop
            self.metric_runtime = noop

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def _metrics_setup(self) -> None:
        self.metric_payloads = Counter(
            "regime_payloads_total",
            "Number of gate payloads emitted",
            ["instrument", "regime", "admit"],
        )
        self.metric_hazard = Gauge(
            "regime_latest_hazard",
            "Latest hazard value per instrument",
            ["instrument"],
        )
        self.metric_age = Gauge(
            "regime_candle_age_seconds",
            "Age of last candle used by the encoder",
            ["instrument"],
        )
        self.metric_runtime = Summary(
            "regime_iteration_seconds",
            "Runtime of each processing iteration",
            ["instrument"],
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info(
            "Regime manifold service starting for %s", ", ".join(self.cfg.instruments)
        )
        if self._metrics_enabled:
            start_http_server(self.cfg.prom_port)
        while not self._stop:
            started = time.time()
            for instrument in self.cfg.instruments:
                with self.metric_runtime.labels(instrument=instrument).time():
                    try:
                        self._process_instrument(instrument)
                    except (
                        redis.RedisError,
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                    ):
                        logger.exception("processing failed for %s", instrument)
            elapsed = time.time() - started
            delay = max(0.2, self.cfg.loop_seconds - elapsed)
            time.sleep(delay)

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Instrument handling
    # ------------------------------------------------------------------
    def _process_instrument(self, instrument: str) -> None:
        candles = self._load_recent_candles(instrument)
        if len(candles) < self.codec.window_candles:
            logger.debug("insufficient candles for %s (%d)", instrument, len(candles))
            return
        windows = self.codec.encode(
            candles,
            instrument=instrument,
            return_only_latest=True,
            align_latest_to_stride=False,
        )
        if not windows:
            return
        window = windows[-1]
        hazard_value = window.metrics["hazard"]
        self.metric_hazard.labels(instrument=instrument).set(hazard_value)
        self.metric_age.labels(instrument=instrument).set(
            self._candle_age_seconds(window)
        )

        # Emit at most once per completed S5 candle. The loop runs every 2s, so
        # without this guard the same structural window would be re-counted and
        # re-written several times before the next candle closes.
        window_end_ms = int(window.end_ms)
        if self._last_emitted_ts_ms.get(instrument) == window_end_ms:
            return

        calibrator = self._hazard_calibrators[instrument]
        calibrator.update(hazard_value)
        hazard_threshold = min(
            calibrator.threshold(),
            self.profile.get(instrument).hazard_max or 1.0,
        )

        repetitions = self._update_signature_history(
            instrument, window.signature, window.end_ms
        )
        admit, reasons = self._evaluate_window(
            instrument, window, hazard_value, hazard_threshold
        )

        if not admit and any(reason.startswith("hazard") for reason in reasons):
            logger.warning(
                "Hazard guard tripped for %s (value=%.4f threshold=%.4f)",
                instrument,
                hazard_value,
                hazard_threshold,
            )

        # [Golden Matrix & Trap Door Implementation]
        # Calculate the macro-structure boundary (T-3 to T-0) to attach to payload, regardless of regime.
        # This gives execution_engine.py the explicit Geometric SL limit.
        trap_door_high = 0.0
        trap_door_low = 0.0
        is_armed = False

        if len(candles) >= 4:
            # Last 4 candles (T-3 to T-0)
            t_window = candles[-4:]
            trap_door_high = max(c.high for c in t_window)
            trap_door_low = min(c.low for c in t_window)

            # Temporary mock-up: The actual matrix conditions are verified server side by execution_engine,
            # but we can optionally flag 'ARMED' here.
            is_armed = admit and window.canonical.regime in self.cfg.admit_regimes

        payload = self._build_payload(
            instrument=instrument,
            window=window,
            hazard_value=hazard_value,
            hazard_threshold=hazard_threshold,
            repetitions=repetitions,
            admit=admit,
            reasons=reasons,
        )

        # Hydrate Valkey payload with physical structure
        payload["action"] = "ARMED" if is_armed else "STANDBY"
        payload["trap_door_high"] = trap_door_high
        payload["trap_door_low"] = trap_door_low

        self._write_gate(instrument, payload)
        self._last_emitted_ts_ms[instrument] = window_end_ms
        self.metric_payloads.labels(
            instrument=instrument,
            regime=window.canonical.regime,
            admit=str(bool(admit)),
        ).inc()

    def _load_recent_candles(self, instrument: str) -> List[Candle]:
        key = f"md:candles:{instrument.upper()}:S5"
        # Extract exactly 4 hours of contiguous market ticks regardless of temporal gaps (e.g. weekends)
        rows_rev = self.redis.zrevrange(key, 0, 2880 - 1)
        rows = list(reversed(rows_rev))

        s5_candles = []
        for raw in rows:
            try:
                payload = json.loads(
                    raw if isinstance(raw, str) else raw.decode("utf-8")
                )
                s5_candles.append(candle_from_payload(payload))
            except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                continue

        s5_candles.sort(key=lambda c: c.timestamp_ms)

        s5_candles.sort(key=lambda c: c.timestamp_ms)
        return s5_candles

    def _update_signature_history(
        self, instrument: str, signature: str, ts_ms: int
    ) -> int:
        history = self._signature_history[instrument][signature]
        history.append(ts_ms)
        retention_ms = self.cfg.signature_retention_minutes * 60 * 1000
        while history and ts_ms - history[0] > retention_ms:
            history.popleft()
        return len(history)

    def _evaluate_window(
        self,
        instrument: str,
        window,
        hazard_value: float,
        hazard_threshold: float,
    ) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        if hazard_value > hazard_threshold:
            reasons.append("hazard_exceeds_adaptive_threshold")
            if hazard_value > hazard_threshold * 1.5:
                reasons.append("hazard_fallback_requested")
        if window.canonical.regime not in self.cfg.admit_regimes:
            reasons.append("regime_filtered")
        if window.canonical.regime_confidence < self.cfg.min_confidence:
            reasons.append("regime_confidence_low")
        admit = len(reasons) == 0
        return admit, reasons

    def _build_payload(
        self,
        *,
        instrument: str,
        window,
        hazard_value: float,
        hazard_threshold: float,
        repetitions: int,
        admit: bool,
        reasons: List[str],
    ) -> Dict[str, object]:
        lambda_value = max(0.0, min(1.0, hazard_value * self.cfg.lambda_scale))

        direction = "FLAT"
        if "bull" in window.canonical.regime:
            direction = "BUY"
        elif "bear" in window.canonical.regime:
            direction = "SELL"

        payload = {
            "instrument": instrument,
            "ts_ms": window.end_ms,
            "admit": admit,
            "direction": direction,
            "lambda": lambda_value,
            "hazard": hazard_value,
            "hazard_threshold": hazard_threshold,
            "repetitions": repetitions,
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
                "codec_meta": window.codec_meta,
            },
            "reasons": reasons,
            "source": "regime_manifold",
        }
        if hazard_value > (hazard_threshold * 1.5):
            payload["components"]["fallback_mode"] = "optical_required"
        return payload

    def _write_gate(self, instrument: str, payload: Mapping[str, object]) -> None:
        key_last = f"gate:last:{instrument.upper()}"
        key_index = f"gate:index:{instrument.upper()}"
        blob = json.dumps(payload, separators=(",", ":"))
        pipe = self.redis.pipeline()
        pipe.set(key_last, blob, ex=self.cfg.gate_ttl_seconds)
        pipe.zadd(key_index, {blob: payload.get("ts_ms", _now_ms())})
        max_entries = 5000
        pipe.zremrangebyrank(key_index, 0, -max_entries - 1)
        pipe.execute()

    def _candle_age_seconds(self, window) -> float:
        now_ms = _now_ms()
        return max(0.0, (now_ms - window.end_ms) / 1000.0)


def _parse_instruments(raw: Optional[str], profile: StrategyProfile) -> List[str]:
    if raw:
        items = [item.strip().upper() for item in raw.split(",") if item.strip()]
        if items:
            return items
    return sorted(profile.instruments.keys())


def _build_config(args: argparse.Namespace, profile: StrategyProfile) -> ServiceConfig:
    return ServiceConfig(
        instruments=_parse_instruments(
            args.instruments or os.getenv("HOTBAND_PAIRS"), profile
        ),
        redis_url=args.redis,
        lookback_minutes=args.lookback_minutes,
        window_candles=args.window,
        stride_candles=args.stride,
        atr_period=args.atr_period,
        loop_seconds=args.loop_seconds,
        signature_retention_minutes=args.signature_retention,
        hazard_percentile=args.hazard_percentile,
        admit_regimes=tuple(
            item.strip() for item in args.admit_regimes.split(",") if item.strip()
        ),
        min_confidence=args.min_confidence,
        gate_ttl_seconds=args.gate_ttl,
        prom_port=args.prom_port,
        lambda_scale=args.lambda_scale,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Streaming regime manifold gate writer"
    )
    parser.add_argument(
        "--redis", default=os.getenv("VALKEY_URL", "redis://localhost:6379/0")
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml"),
    )
    parser.add_argument(
        "--instruments",
        help="Comma separated symbol list (default HOTBAND_PAIRS or strategy file)",
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=int(os.getenv("REGIME_LOOKBACK_MINUTES", "240")),
    )
    parser.add_argument(
        "--window", type=int, default=int(os.getenv("REGIME_WINDOW_CANDLES", "64"))
    )
    parser.add_argument(
        "--stride", type=int, default=int(os.getenv("REGIME_STRIDE_CANDLES", "16"))
    )
    parser.add_argument(
        "--atr-period", type=int, default=int(os.getenv("REGIME_ATR_PERIOD", "14"))
    )
    parser.add_argument(
        "--loop-seconds",
        type=float,
        default=float(os.getenv("REGIME_LOOP_SECONDS", "15.0")),
    )
    parser.add_argument(
        "--signature-retention",
        type=int,
        default=int(os.getenv("REGIME_SIGNATURE_MINUTES", "60")),
    )
    parser.add_argument(
        "--hazard-percentile",
        type=float,
        default=float(os.getenv("REGIME_HAZARD_PERCENTILE", "0.8")),
    )
    parser.add_argument(
        "--admit-regimes",
        default=os.getenv("REGIME_ADMIT_REGIMES", "trend_bull,trend_bear"),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=float(os.getenv("REGIME_MIN_CONFIDENCE", "0.55")),
    )
    parser.add_argument(
        "--gate-ttl", type=int, default=int(os.getenv("REGIME_GATE_TTL", str(15 * 60)))
    )
    parser.add_argument(
        "--prom-port", type=int, default=int(os.getenv("REGIME_PROM_PORT", "9105"))
    )
    parser.add_argument(
        "--lambda-scale",
        type=float,
        default=float(os.getenv("REGIME_LAMBDA_SCALE", "0.1")),
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _configure_logging(args.log_level)
    profile = StrategyProfile.load(Path(args.profile))
    config = _build_config(args, profile)
    service = RegimeManifoldService(config, profile)

    def _handle(sig, frame):  # type: ignore[override]
        logger.info("received %s, shutting down", sig)
        service.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    service.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
