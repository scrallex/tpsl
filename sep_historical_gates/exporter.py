"""Build historical SEP-style gate artifacts from Market Data intraday bars."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import pandas as pd

from options_research.data import MarketDataClient
from scripts.research.bundle_rules import BundleCatalog, apply_semantic_tags, get_bundle_hits
from scripts.research.regime_manifold.encoder import MarketManifoldEncoder
from scripts.research.regime_manifold.types import Candle, EncodedWindow


def _ensure_tz_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HistoricalSEPParityGateExportConfig:
    symbol: str
    start: datetime
    end: datetime
    resolution_minutes: int = 1
    request_chunk_days: int = 30
    adjust_splits: bool = False
    extended_hours: bool = False
    window_candles: int = 64
    stride_candles: int = 16
    atr_period: int = 14
    signature_retention_minutes: int = 60
    hazard_percentile: float = 0.8
    hazard_max: float = 1.0
    admit_regimes: tuple[str, ...] = ("trend_bull", "trend_bear")
    min_confidence: float = 0.55
    lambda_scale: float = 0.1
    bundle_config: Path | None = Path("config/bundle_strategy.yaml")
    regime_mapping_path: Path | None = Path("config/regime_mapping.json")

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        _ensure_tz_aware(self.start, "start")
        _ensure_tz_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be positive")
        if self.request_chunk_days <= 0:
            raise ValueError("request_chunk_days must be positive")
        if self.window_candles <= 0 or self.stride_candles <= 0 or self.atr_period <= 0:
            raise ValueError("window_candles, stride_candles, and atr_period must be positive")
        if self.signature_retention_minutes <= 0:
            raise ValueError("signature_retention_minutes must be positive")
        if self.hazard_max <= 0:
            raise ValueError("hazard_max must be positive")
        if self.min_confidence < 0.0 or self.min_confidence > 1.0:
            raise ValueError("min_confidence must be within [0.0, 1.0]")
        if self.lambda_scale <= 0:
            raise ValueError("lambda_scale must be positive")
        if not self.admit_regimes:
            raise ValueError("admit_regimes must not be empty")

    def output_path(self) -> Path:
        return Path("data/options_research/gates") / f"{self.symbol.upper()}.gates.jsonl"


@dataclass(frozen=True, slots=True)
class HistoricalGateExportResult:
    output_path: Path
    bar_count: int
    window_count: int
    gate_count: int
    admitted_gate_count: int
    first_gate_at: datetime | None
    last_gate_at: datetime | None


class HazardCalibrator:
    """Rolling percentile threshold tracker copied from the SEP manifold service."""

    def __init__(self, percentile: float = 0.8, max_samples: int = 2048) -> None:
        from bisect import insort

        self.percentile = min(max(percentile, 0.05), 0.99)
        self.max_samples = max_samples
        self._samples: list[float] = []
        self._insort = insort

    def update(self, value: float) -> None:
        self._insort(self._samples, value)
        if len(self._samples) > self.max_samples:
            self._samples.pop(0)

    def threshold(self) -> float:
        if not self._samples:
            return 1.0
        index = int(self.percentile * (len(self._samples) - 1))
        return self._samples[index]


class HistoricalSEPParityGateExporter:
    """Fetches intraday bars and exports SEP-style historical gate records."""

    def __init__(self, client: MarketDataClient | None = None) -> None:
        self.client = client or MarketDataClient()

    def export(
        self,
        *,
        config: HistoricalSEPParityGateExportConfig,
        output_path: Path | None = None,
    ) -> HistoricalGateExportResult:
        bars_frame = self.fetch_intraday_bars(config=config)
        if bars_frame.empty:
            raise ValueError(
                f"No intraday bars returned for {config.symbol} between {config.start} and {config.end}"
            )
        records = self.build_records(config=config, bars_frame=bars_frame)
        target = output_path or config.output_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

        first_gate_at = None
        last_gate_at = None
        if records:
            first_gate_at = datetime.fromtimestamp(int(records[0]["ts_ms"]) / 1000, tz=config.start.tzinfo)
            last_gate_at = datetime.fromtimestamp(int(records[-1]["ts_ms"]) / 1000, tz=config.start.tzinfo)
        admitted_gate_count = sum(1 for record in records if bool(record.get("admit")))
        return HistoricalGateExportResult(
            output_path=target,
            bar_count=len(bars_frame),
            window_count=len(records),
            gate_count=len(records),
            admitted_gate_count=admitted_gate_count,
            first_gate_at=first_gate_at,
            last_gate_at=last_gate_at,
        )

    def fetch_intraday_bars(self, *, config: HistoricalSEPParityGateExportConfig) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        cursor = config.start
        chunk_span = timedelta(days=config.request_chunk_days)
        step = timedelta(seconds=1)
        while cursor < config.end:
            chunk_end = min(cursor + chunk_span, config.end)
            frame = self.client.fetch_intraday_bars(
                symbol=config.symbol,
                resolution_minutes=config.resolution_minutes,
                start=cursor,
                end=chunk_end,
                adjust_splits=config.adjust_splits,
                extended_hours=config.extended_hours,
            )
            if not frame.empty:
                frames.append(frame)
            if chunk_end >= config.end:
                break
            cursor = chunk_end + step
        if not frames:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
        return combined.reset_index(drop=True)

    def build_records(
        self,
        *,
        config: HistoricalSEPParityGateExportConfig,
        bars_frame: pd.DataFrame,
    ) -> list[dict[str, object]]:
        candles = self._frame_to_candles(bars_frame)
        if len(candles) < config.window_candles:
            return []

        encoder = MarketManifoldEncoder(
            window_candles=config.window_candles,
            stride_candles=config.stride_candles,
            atr_period=config.atr_period,
        )
        windows = encoder.encode(candles, instrument=config.symbol.upper(), return_only_latest=False)
        if not windows:
            return []

        calibrator = HazardCalibrator(percentile=config.hazard_percentile)
        signature_history: dict[str, deque[int]] = defaultdict(deque)
        bundle_catalog = self._bundle_catalog(config.bundle_config)
        regime_mapping = self._regime_mapping(config.regime_mapping_path)
        records: list[dict[str, object]] = []
        window_start_index = 0

        for window in windows:
            hazard_value = float(window.metrics.get("hazard", 0.0))
            calibrator.update(hazard_value)
            hazard_threshold = min(calibrator.threshold(), config.hazard_max)
            repetitions = self._update_signature_history(
                signature_history=signature_history,
                signature=window.signature,
                ts_ms=window.end_ms,
                retention_minutes=config.signature_retention_minutes,
            )
            admit, reasons = self._evaluate_window(
                window=window,
                hazard_value=hazard_value,
                hazard_threshold=hazard_threshold,
                admit_regimes=config.admit_regimes,
                min_confidence=config.min_confidence,
            )
            subset = candles[window_start_index : window_start_index + config.window_candles]
            payload = self._build_payload(
                symbol=config.symbol.upper(),
                window=window,
                hazard_value=hazard_value,
                hazard_threshold=hazard_threshold,
                repetitions=repetitions,
                admit=admit,
                reasons=reasons,
                lambda_scale=config.lambda_scale,
                subset=subset,
                admit_regimes=config.admit_regimes,
                bundle_catalog=bundle_catalog,
                regime_mapping=regime_mapping,
            )
            records.append(payload)
            window_start_index += config.stride_candles
        return records

    @staticmethod
    def _frame_to_candles(frame: pd.DataFrame) -> list[Candle]:
        candles: list[Candle] = []
        for row in frame.itertuples(index=False):
            timestamp_ms = int(pd.Timestamp(row.timestamp).timestamp() * 1000)
            candles.append(
                Candle(
                    timestamp_ms=timestamp_ms,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
            )
        return candles

    @staticmethod
    def _update_signature_history(
        *,
        signature_history: dict[str, deque[int]],
        signature: str,
        ts_ms: int,
        retention_minutes: int,
    ) -> int:
        history = signature_history[signature]
        history.append(ts_ms)
        retention_ms = retention_minutes * 60 * 1000
        while history and ts_ms - history[0] > retention_ms:
            history.popleft()
        return len(history)

    @staticmethod
    def _evaluate_window(
        *,
        window: EncodedWindow,
        hazard_value: float,
        hazard_threshold: float,
        admit_regimes: tuple[str, ...],
        min_confidence: float,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if hazard_value > hazard_threshold:
            reasons.append("hazard_exceeds_adaptive_threshold")
            if hazard_value > hazard_threshold * 1.5:
                reasons.append("hazard_fallback_requested")
        if window.canonical.regime not in admit_regimes:
            reasons.append("regime_filtered")
        if window.canonical.regime_confidence < min_confidence:
            reasons.append("regime_confidence_low")
        return len(reasons) == 0, reasons

    def _build_payload(
        self,
        *,
        symbol: str,
        window: EncodedWindow,
        hazard_value: float,
        hazard_threshold: float,
        repetitions: int,
        admit: bool,
        reasons: list[str],
        lambda_scale: float,
        subset: list[Candle],
        admit_regimes: tuple[str, ...],
        bundle_catalog: BundleCatalog | None,
        regime_mapping: dict[str, str],
    ) -> dict[str, object]:
        lambda_value = max(0.0, min(1.0, hazard_value * lambda_scale))
        direction = "FLAT"
        if "bull" in window.canonical.regime:
            direction = "BUY"
        elif "bear" in window.canonical.regime:
            direction = "SELL"

        payload: dict[str, object] = {
            "instrument": symbol,
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

        if len(subset) >= 4:
            t_window = subset[-4:]
            payload["trap_door_high"] = max(candle.high for candle in t_window)
            payload["trap_door_low"] = min(candle.low for candle in t_window)
        else:
            payload["trap_door_high"] = 0.0
            payload["trap_door_low"] = 0.0
        payload["action"] = "ARMED" if admit and window.canonical.regime in admit_regimes else "STANDBY"

        payload = apply_semantic_tags(payload)
        bundle_hits, bundle_blocks, bundle_readiness = get_bundle_hits(
            payload,
            catalog=bundle_catalog,
            bundle_config=None,
        )

        allowed_bundles = self._allowed_bundle_ids(symbol=symbol, regime_mapping=regime_mapping)
        allowed_hits = [hit for hit in bundle_hits if hit.get("id") in allowed_bundles]
        allowed_blocks = [item for item in bundle_blocks if item in allowed_bundles]
        allowed_readiness = {
            bundle_id: readiness
            for bundle_id, readiness in bundle_readiness.items()
            if bundle_id in allowed_bundles
        }
        if allowed_hits:
            payload["bundle_hits"] = allowed_hits
        if allowed_blocks:
            payload["bundle_blocks"] = allowed_blocks
        if allowed_readiness:
            payload["bundle_readiness"] = allowed_readiness
        return payload

    @staticmethod
    def _bundle_catalog(bundle_config: Path | None) -> BundleCatalog | None:
        if bundle_config is None or not bundle_config.exists():
            return None
        return BundleCatalog.load(bundle_config)

    @staticmethod
    def _regime_mapping(path: Path | None) -> dict[str, str]:
        if path is None or not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("instrument_strategies")
        if not isinstance(raw, dict):
            return {}
        return {str(key).upper(): str(value) for key, value in raw.items()}

    @staticmethod
    def _allowed_bundle_ids(*, symbol: str, regime_mapping: dict[str, str]) -> set[str]:
        strategy = regime_mapping.get(symbol.upper(), "Hybrid Protocol")
        allowed_bundles = {"MB003", "NB001", "CB002"}
        if "Trend Sniper" in strategy:
            return {"MB003", "CB002"}
        if "Mean Reversion" in strategy:
            return {"NB001", "CB002"}
        return allowed_bundles
