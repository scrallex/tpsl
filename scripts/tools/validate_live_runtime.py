#!/usr/bin/env python3
"""Validate that the live runtime has the expected strategy, candles, and gates."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

import redis

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.trading.gate_loader import StrategyProfile


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the live strategy profile and Valkey runtime state."
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0",
        help="Valkey/Redis URL.",
    )
    parser.add_argument(
        "--strategy-path",
        default=os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml"),
        help="Path to the active strategy profile YAML.",
    )
    parser.add_argument(
        "--instruments",
        nargs="*",
        help="Instrument subset to validate. Defaults to the strategy profile instruments.",
    )
    parser.add_argument(
        "--granularity",
        default="S5",
        help="Candle granularity key to validate.",
    )
    parser.add_argument(
        "--min-candles",
        type=int,
        default=180,
        help="Minimum retained candles required per instrument.",
    )
    parser.add_argument(
        "--max-gate-age-seconds",
        type=int,
        default=180,
        help="Maximum allowed age for gate:last payloads.",
    )
    return parser.parse_args()


def _structural_metric(payload: Mapping[str, Any], key: str) -> Optional[float]:
    for source_key in ("components", "structure", "metrics"):
        source = payload.get(source_key)
        if isinstance(source, Mapping) and key in source:
            try:
                return float(source[key])
            except (TypeError, ValueError):
                return None
    if key in payload:
        try:
            return float(payload[key])
        except (TypeError, ValueError):
            return None
    return None


def _expected_instruments(raw: Optional[Iterable[str]], profile: StrategyProfile) -> List[str]:
    if raw:
        items = [str(item or "").strip().upper() for item in raw if str(item or "").strip()]
        if items:
            return items
    return sorted(profile.instruments.keys())


def main() -> int:
    args = _parse_args()
    strategy_path = Path(args.strategy_path)
    if not strategy_path.exists():
        print(f"Missing strategy profile: {strategy_path}", file=sys.stderr)
        return 1

    profile = StrategyProfile.load(strategy_path)
    instruments = _expected_instruments(args.instruments, profile)
    if not instruments:
        print("No instruments available for validation.", file=sys.stderr)
        return 1

    missing_from_strategy = [inst for inst in instruments if inst not in profile.instruments]
    if missing_from_strategy:
        print(
            "Strategy profile is missing instruments: "
            + ", ".join(sorted(missing_from_strategy)),
            file=sys.stderr,
        )
        return 1

    client = redis.from_url(args.redis_url)
    failures: List[str] = []
    now_ms = int(time.time() * 1000)

    for instrument in instruments:
        candle_key = f"md:candles:{instrument}:{args.granularity.upper()}"
        gate_key = f"gate:last:{instrument}"

        try:
            candle_count = int(client.zcard(candle_key) or 0)
        except Exception as exc:  # pragma: no cover - runtime connectivity failure
            failures.append(f"{instrument}: failed to read {candle_key}: {exc}")
            continue

        if candle_count < args.min_candles:
            failures.append(
                f"{instrument}: candle count {candle_count} below required {args.min_candles}"
            )

        try:
            raw_gate = client.get(gate_key)
        except Exception as exc:  # pragma: no cover - runtime connectivity failure
            failures.append(f"{instrument}: failed to read {gate_key}: {exc}")
            continue

        if not raw_gate:
            failures.append(f"{instrument}: missing gate payload")
            continue

        try:
            payload = json.loads(
                raw_gate if isinstance(raw_gate, str) else raw_gate.decode("utf-8")
            )
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            failures.append(f"{instrument}: invalid gate payload: {exc}")
            continue

        gate_ts = int(payload.get("ts_ms") or 0)
        if gate_ts <= 0:
            failures.append(f"{instrument}: gate payload missing ts_ms")
        else:
            age_seconds = max(0.0, (now_ms - gate_ts) / 1000.0)
            if age_seconds > args.max_gate_age_seconds:
                failures.append(
                    f"{instrument}: gate age {age_seconds:.1f}s exceeds {args.max_gate_age_seconds}s"
                )

        if payload.get("hazard") is None:
            failures.append(f"{instrument}: gate payload missing hazard")

        direction = str(payload.get("direction") or "").upper()
        if direction not in {"BUY", "SELL", "FLAT"}:
            failures.append(f"{instrument}: invalid gate direction {direction!r}")

        if _structural_metric(payload, "coherence") is None:
            failures.append(f"{instrument}: gate payload missing coherence metric")
        if _structural_metric(payload, "entropy") is None:
            failures.append(f"{instrument}: gate payload missing entropy metric")

    if failures:
        print("LIVE RUNTIME VALIDATION FAILED", file=sys.stderr)
        for line in failures:
            print(f"- {line}", file=sys.stderr)
        return 1

    print(
        "LIVE RUNTIME VALIDATION OK "
        f"({len(instruments)} instruments, strategy={strategy_path}, redis={args.redis_url})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
