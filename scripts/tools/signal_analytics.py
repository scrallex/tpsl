#!/usr/bin/env python3
"""Summarise gate signals to showcase trading-ready opportunities."""
from __future__ import annotations


import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scripts.tools.cli_runner import CLIRunner

try:
    import redis  # type: ignore
except Exception as exc:  # pragma: no cover - dependency issue
    redis = None
    _IMPORT_ERROR = exc
    _IMPORT_ERROR = None

from scripts.trading.candle_utils import to_epoch_ms
from scripts.trading.portfolio_manager import structural_metric, StrategyProfile

REPETITION_BONUS_WEIGHT = 0.02
ENTROPY_PENALTY_THRESHOLD = 0.6


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _event_timestamp(payload: Dict[str, Any]) -> Optional[float]:
    for key in ("ts_ms", "ts", "timestamp", "time"):
        if key in payload:
            value = payload.get(key)
            if isinstance(value, (int, float)):
                if value > 10_000_000_000:
                    return float(value) / 1000.0
                return float(value)
            text = str(value).strip()
            if not text:
                continue
            if text.isdigit():
                number = float(text)
                if len(text) >= 13:
                    return number / 1000.0
                return number
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    structure = payload.get("structure")
    if isinstance(structure, dict):
        return _event_timestamp(structure)
    return None


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _hazard(payload: Dict[str, Any]) -> Optional[float]:
    struct = payload.get("structure")
    if isinstance(struct, dict):
        return _as_float(struct.get("hazard"))
    return None


def _lambda(payload: Dict[str, Any]) -> Optional[float]:
    return _as_float(payload.get("lambda"))


def _signal_score(payload: Dict[str, Any]) -> float:
    coherence = structural_metric(payload, "coherence") or 0.0
    stability = structural_metric(payload, "stability") or 0.0
    hazard_value = _hazard(payload) or 0.0
    entropy = structural_metric(payload, "entropy") or 0.0
    repetitions = _as_float(payload.get("repetitions")) or 0.0
    admit_bonus = 0.15 if payload.get("admit") else -0.15
    return (
        (coherence + stability)
        - hazard_value
        - max(0.0, entropy - ENTROPY_PENALTY_THRESHOLD)
        + (repetitions * REPETITION_BONUS_WEIGHT)
        + admit_bonus
    )


def _collect_events(
    client, instrument: str, *, lookback_ms: Optional[int], limit: Optional[int]
) -> List[Dict[str, Any]]:
    inst = instrument.upper()
    now_ms = to_epoch_ms(datetime.now(timezone.utc))
    min_score = now_ms - lookback_ms if lookback_ms is not None else "-inf"
    key_index = f"gate:index:{inst}"
    events: List[Dict[str, Any]] = []
    if client.exists(key_index):
        entries = client.zrevrangebyscore(key_index, "+inf", min_score, withscores=True)
        for blob, score in entries:
            try:
                payload = json.loads(
                    blob if isinstance(blob, str) else blob.decode("utf-8")
                )
            except Exception:
                continue
            if isinstance(score, (int, float)):
                payload.setdefault("ts_ms", float(score))
            events.append(payload)
            if limit and len(events) >= limit:
                break
    if not events:
        raw = client.get(f"gate:last:{inst}")
        if raw:
            try:
                payload = json.loads(
                    raw if isinstance(raw, str) else raw.decode("utf-8")
                )
            except Exception:
                payload = None
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _mean(values: List[Optional[float]]) -> Optional[float]:
    filtered = [v for v in values if v is not None]
    if not filtered:
        return None
    return sum(filtered) / len(filtered)


def _event_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = _event_timestamp(payload)
    return {
        "ts": _iso(ts),
        "age_seconds": None if ts is None else max(0.0, time.time() - ts),
        "admit": bool(payload.get("admit")),
        "lambda": _lambda(payload),
        "hazard": _hazard(payload),
        "coherence": structural_metric(payload, "coherence"),
        "stability": structural_metric(payload, "stability"),
        "entropy": structural_metric(payload, "entropy"),
        "repetitions": _as_float(payload.get("repetitions")),
        "reasons": list(payload.get("reasons") or []),
        "score": round(_signal_score(payload), 5),
    }


def _summarise_instrument(
    instrument: str, events: List[Dict[str, Any]], *, top_n: int
) -> Dict[str, Any]:
    total = len(events)
    admits = sum(1 for event in events if event.get("admit"))
    hazard_values = [_hazard(event) for event in events]
    lambda_values = [_lambda(event) for event in events]
    coherence_values = [structural_metric(event, "coherence") for event in events]
    stability_values = [structural_metric(event, "stability") for event in events]
    entropy_values = [structural_metric(event, "entropy") for event in events]

    reasons = Counter()
    for event in events:
        reasons.update(event.get("reasons") or [])

    scored_events = sorted(events, key=_signal_score, reverse=True)
    highlights = [_event_view(event) for event in scored_events[:top_n]]
    recent = [
        _event_view(event)
        for event in sorted(
            events, key=lambda item: _event_timestamp(item) or 0.0, reverse=True
        )[:top_n]
    ]

    return {
        "instrument": instrument,
        "counts": {
            "total": total,
            "admit": admits,
            "reject": total - admits,
            "admit_rate": (admits / total) if total else 0.0,
        },
        "averages": {
            "hazard": _mean(hazard_values),
            "lambda": _mean(lambda_values),
            "coherence": _mean(coherence_values),
            "stability": _mean(stability_values),
            "entropy": _mean(entropy_values),
        },
        "top_reasons": reasons.most_common(5),
        "recent_signals": recent,
        "highlight_signals": highlights,
    }


def _print_human_summary(snapshots: Dict[str, Any]) -> None:
    total_signals = sum(data["counts"]["total"] for data in snapshots.values())
    total_admits = sum(data["counts"]["admit"] for data in snapshots.values())
    print(f"\nSignal analytics across {len(snapshots)} instrument(s):")
    print(
        f"- Total signals: {total_signals} | Admits: {total_admits} ({(total_admits / total_signals * 100):.1f}% )"
        if total_signals
        else "- No signals found"
    )

    for instrument, data in snapshots.items():
        counts = data["counts"]
        avgs = data["averages"]
        admit_pct = counts["admit_rate"] * 100 if counts["total"] else 0.0
        print(f"\n=== {instrument} ===")
        print(
            f"Signals: {counts['total']}  |  Admit rate: {admit_pct:.1f}%  |  Avg hazard: {avgs['hazard'] or 0:.3f}  |  Avg λ: {avgs['lambda'] or 0:.3f}"
        )
        print(
            f"Avg coherence: {(avgs['coherence'] or 0):.3f}  |  Avg stability: {(avgs['stability'] or 0):.3f}"
        )
        if data["top_reasons"]:
            reason_snippets = ", ".join(
                f"{name} ({count})" for name, count in data["top_reasons"]
            )
            print(f"Top rejection reasons: {reason_snippets}")
        else:
            print("Top rejection reasons: —")
        print("Recent signals:")
        if not data["recent_signals"]:
            print("  (no signals)")
        else:
            for entry in data["recent_signals"]:
                status = "✓" if entry["admit"] else "×"
                hazard = entry["hazard"]
                coh = entry["coherence"]
                ts = entry["ts"] or "unknown"
                reasons = ", ".join(entry["reasons"]) if entry["reasons"] else "—"
                print(
                    f"  {ts}  {status}  λ={entry['lambda'] or 0:.3f}  hazard={hazard or 0:.3f}  coh={coh or 0:.3f}  reasons: {reasons}"
                )
        if data["highlight_signals"]:
            best = data["highlight_signals"][0]
            ts = best["ts"] or "unknown"
            print(
                f"Top setup: {ts} | score {best['score']:.3f} | admit={'yes' if best['admit'] else 'no'} | hazard={best['hazard'] or 0:.3f} | coherence={best['coherence'] or 0:.3f}"
            )
        else:
            print("Top setup: —")


def run_signal_analytics(
    redis_url: str,
    instruments: Sequence[str],
    *,
    lookback_minutes: Optional[int],
    limit: Optional[int],
    top_n: int,
) -> Dict[str, Any]:
    if not redis:
        raise RuntimeError(f"redis dependency unavailable: {_IMPORT_ERROR}")
    client = redis.from_url(redis_url)
    lookback_ms = (
        None if lookback_minutes is None else max(1, lookback_minutes) * 60 * 1000
    )
    snapshots: Dict[str, Any] = {}
    for instrument in instruments:
        events = _collect_events(
            client, instrument, lookback_ms=lookback_ms, limit=limit
        )
        if not events:
            snapshots[instrument.upper()] = {
                "instrument": instrument.upper(),
                "counts": {"total": 0, "admit": 0, "reject": 0, "admit_rate": 0.0},
                "averages": {
                    "hazard": None,
                    "lambda": None,
                    "coherence": None,
                    "stability": None,
                    "entropy": None,
                },
                "top_reasons": [],
                "recent_signals": [],
                "highlight_signals": [],
            }
            continue
        snapshots[instrument.upper()] = _summarise_instrument(
            instrument.upper(), events, top_n=top_n
        )
    return snapshots


def _resolve_instruments(
    client, raw: Optional[str], default_profile: Path
) -> List[str]:
    if raw:
        return [item.strip().upper() for item in raw.split(",") if item.strip()]
    if default_profile.exists():
        profile = StrategyProfile.load(default_profile)
        if profile.instruments:
            return sorted(profile.instruments.keys())
    instruments: List[str] = []
    for key in client.scan_iter(match="gate:last:*"):
        name = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        parts = name.split(":")
        if parts:
            instruments.append(parts[-1].upper())
    return sorted({inst for inst in instruments if inst})


def main(args) -> int:
    if not redis:
        print(f"redis dependency unavailable: {_IMPORT_ERROR}", file=sys.stderr)
        return 2

    client = redis.from_url(args.redis)
    instruments = _resolve_instruments(client, args.instruments, Path(args.profile))
    if not instruments:
        print(
            "No instruments available (provide --instruments or seed gates).",
            file=sys.stderr,
        )
        return 2

    lookback = None if args.lookback_minutes == 0 else args.lookback_minutes
    try:
        snapshots = run_signal_analytics(
            args.redis,
            instruments,
            lookback_minutes=lookback,
            limit=max(1, args.max_signals),
            top_n=max(1, args.top_count),
        )
    except Exception as exc:
        print(f"Failed to compute signal analytics: {exc}", file=sys.stderr)
        return 2

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_minutes": lookback,
        "instruments": snapshots,
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=lambda x: x))
    else:
        _print_human_summary(snapshots)

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI hydration
    runner = CLIRunner("Summarise gate signals for trading analytics")
    runner.add_redis_arg()
    runner.add_arg(
        "--instruments",
        nargs="*",
        help="Comma separated instrument list; defaults to strategy profile or discovered keys",
    )
    runner.add_arg(
        "--profile",
        default="config/mean_reversion_strategy.yaml",
        help="Strategy profile used when inferring instruments",
    )
    runner.add_arg(
        "--lookback-minutes",
        type=int,
        default=360,
        help="Lookback window in minutes (set 0 to disable)",
    )
    runner.add_arg(
        "--max-signals",
        type=int,
        default=250,
        help="Maximum signals per instrument to analyse",
    )
    runner.add_arg(
        "--top-count",
        type=int,
        default=3,
        help="Number of recent/highlight signals to show",
    )
    runner.add_arg(
        "--json",
        action="store_true",
        help="Emit JSON payload instead of formatted text",
    )
    sys.exit(runner.run(main))
