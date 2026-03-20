"""Helpers for selecting and materializing historical gate caches."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from scripts.research.simulator.signal_deriver import (
    derive_regime_manifold_gates,
    derive_signals,
)

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_CANDLES = 64
# Live now emits the most recent 64-candle manifold on every completed S5
# candle. Historical mean-reversion caches must therefore materialize rolling
# windows instead of 16-candle boundary snapshots.
_DEFAULT_STRIDE_CANDLES = 1


def gate_cache_path_for(
    instrument: str,
    signal_type: Optional[str] = None,
    *,
    base_dir: Path | str = Path("output/market_data"),
) -> Path:
    root = Path(base_dir)
    inst = instrument.upper()
    normalized = str(signal_type or "").strip().lower()
    if normalized == "mean_reversion":
        return root / f"{inst}.mean_reversion.gates.jsonl"
    return root / f"{inst}.gates.jsonl"


def _write_gate_cache(path: Path, gates: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for gate in gates:
            handle.write(json.dumps(gate) + "\n")


def _mean_reversion_cache_compatible(
    path: Path,
    *,
    window_candles: int = _DEFAULT_WINDOW_CANDLES,
    stride_candles: int = _DEFAULT_STRIDE_CANDLES,
    sample_size: int = 8,
) -> bool:
    if not path.exists():
        return False

    seen = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if str(payload.get("source", "")).lower() != "regime_manifold":
                    return False

                components = payload.get("components") or {}
                codec_meta = components.get("codec_meta") if isinstance(components, dict) else {}
                if not isinstance(codec_meta, dict):
                    return False

                try:
                    cached_window = int(codec_meta.get("window_candles"))
                    cached_stride = int(codec_meta.get("stride_candles"))
                except (TypeError, ValueError):
                    return False

                if cached_window != int(window_candles):
                    return False
                if cached_stride != int(stride_candles):
                    return False

                seen += 1
                if seen >= sample_size:
                    return True
    except Exception:
        return False

    return seen > 0


def ensure_historical_gate_cache(
    instrument: str,
    start: datetime,
    end: datetime,
    *,
    signal_type: Optional[str] = None,
    granularity: str = "S5",
    base_dir: Path | str = Path("output/market_data"),
    candle_cache_path: Optional[Path] = None,
    gate_cache_path: Optional[Path] = None,
) -> Path:
    root = Path(base_dir)
    cache_path = gate_cache_path or gate_cache_path_for(
        instrument, signal_type, base_dir=root
    )
    normalized = str(signal_type or "").strip().lower()

    if candle_cache_path is None:
        inferred = root / f"{instrument.upper()}.jsonl"
        candle_cache_path = inferred if inferred.exists() else None

    if normalized == "mean_reversion":
        if _mean_reversion_cache_compatible(cache_path):
            return cache_path
        gates = derive_regime_manifold_gates(
            instrument,
            start=start,
            end=end,
            granularity=granularity,
            cache_path=candle_cache_path,
            window_candles=_DEFAULT_WINDOW_CANDLES,
            stride_candles=_DEFAULT_STRIDE_CANDLES,
        )
        if not gates:
            logger.warning(
                "Failed to materialize live-parity mean-reversion gate cache for %s",
                instrument,
            )
            return cache_path
        _write_gate_cache(cache_path, gates)
        logger.info(
            "Saved %d live-parity mean-reversion gates to %s",
            len(gates),
            cache_path,
        )
        return cache_path

    if cache_path.exists():
        return cache_path

    gates = derive_signals(
        instrument,
        start=start,
        end=end,
        granularity=granularity,
        cache_path=candle_cache_path,
    )
    if not gates:
        logger.warning("Failed to derive synthetic gate cache for %s", instrument)
        return cache_path
    _write_gate_cache(cache_path, gates)
    logger.info("Saved %d synthetic gates to %s", len(gates), cache_path)
    return cache_path
