#!/usr/bin/env python3
"""Audit cross-instrument overlap and NAV utilization from exported trade logs."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


INSTRUMENTS = [
    "EUR_USD",
    "USD_CAD",
    "GBP_USD",
    "NZD_USD",
    "USD_CHF",
    "AUD_USD",
    "USD_JPY",
]


@dataclass
class WindowAudit:
    window: str
    total_trades: int
    max_concurrent: int
    avg_concurrent: float
    pct_flat: float
    peak_notional_usd: float
    peak_notional_pct_nav: float
    peak_margin_pct_nav: float
    peak_timestamp: str | None
    max_by_instrument: Dict[str, int]


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _usd_notional(instrument: str, units: int | float, price: int | float) -> float:
    base = instrument.upper().split("_", 1)[0]
    if base == "USD":
        return abs(float(units))
    return abs(float(units)) * float(price)


def _window_audit(
    window_dir: Path,
    *,
    nav: float,
    exposure_scale: float,
    instruments: Iterable[str],
) -> WindowAudit:
    events: List[Tuple[datetime, int, str, float]] = []
    total_trades = 0

    for instrument in instruments:
        trade_path = window_dir / f"{instrument}.trades.json"
        if not trade_path.exists():
            continue
        payload = json.loads(trade_path.read_text(encoding="utf-8"))
        for trade in payload.get("trades", []):
            total_trades += 1
            notional_usd = _usd_notional(
                instrument,
                trade.get("units", 0),
                trade.get("entry_price", 0.0),
            )
            events.append((_parse_dt(trade["entry_time"]), 1, instrument, notional_usd))
            events.append((_parse_dt(trade["exit_time"]), -1, instrument, notional_usd))

    events.sort(key=lambda item: (item[0], item[1]))

    current_concurrent = 0
    max_concurrent = 0
    current_notional = 0.0
    peak_notional = 0.0
    peak_ts: datetime | None = None
    current_by_instrument: Dict[str, int] = {}
    max_by_instrument: Dict[str, int] = {}

    previous_time: datetime | None = None
    weighted_sum = 0.0
    total_duration = 0.0
    flat_duration = 0.0

    for event_time, delta, instrument, notional in events:
        if previous_time is not None:
            duration = (event_time - previous_time).total_seconds()
            weighted_sum += current_concurrent * duration
            total_duration += duration
            if current_concurrent == 0:
                flat_duration += duration

        current_concurrent += delta
        current_notional += delta * notional
        current_by_instrument[instrument] = current_by_instrument.get(instrument, 0) + delta

        max_concurrent = max(max_concurrent, current_concurrent)
        max_by_instrument[instrument] = max(
            max_by_instrument.get(instrument, 0),
            current_by_instrument[instrument],
        )
        if current_notional > peak_notional:
            peak_notional = current_notional
            peak_ts = event_time

        previous_time = event_time

    avg_concurrent = (weighted_sum / total_duration) if total_duration > 0 else 0.0
    pct_flat = (flat_duration / total_duration * 100.0) if total_duration > 0 else 0.0
    peak_notional_pct_nav = (peak_notional / nav * 100.0) if nav > 0 else 0.0
    peak_margin_pct_nav = (
        peak_notional * exposure_scale / nav * 100.0 if nav > 0 else 0.0
    )

    return WindowAudit(
        window=window_dir.name,
        total_trades=total_trades,
        max_concurrent=max_concurrent,
        avg_concurrent=avg_concurrent,
        pct_flat=pct_flat,
        peak_notional_usd=peak_notional,
        peak_notional_pct_nav=peak_notional_pct_nav,
        peak_margin_pct_nav=peak_margin_pct_nav,
        peak_timestamp=peak_ts.isoformat() if peak_ts is not None else None,
        max_by_instrument=dict(sorted(max_by_instrument.items())),
    )


def _recommendations(
    *,
    observed_peak_concurrent: int,
    alloc_top_k: int,
    target_peak_utilization_pct: float,
    hard_cap_utilization_pct: float,
) -> Dict[str, float]:
    if observed_peak_concurrent <= 0 or alloc_top_k <= 0:
        return {
            "gross_pct_for_target_peak": 0.0,
            "gross_pct_hard_cap_limit": 0.0,
            "recommended_gross_pct": 0.0,
        }

    gross_pct_for_target_peak = target_peak_utilization_pct / float(observed_peak_concurrent)
    gross_pct_hard_cap_limit = hard_cap_utilization_pct / float(alloc_top_k)
    recommended_gross_pct = min(gross_pct_for_target_peak, gross_pct_hard_cap_limit)
    return {
        "gross_pct_for_target_peak": gross_pct_for_target_peak * 100.0,
        "gross_pct_hard_cap_limit": gross_pct_hard_cap_limit * 100.0,
        "recommended_gross_pct": recommended_gross_pct * 100.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit overlap, gross notional, and implied NAV utilization across exported trade logs."
    )
    parser.add_argument(
        "--window-dir",
        action="append",
        required=True,
        help="Window directory containing <INSTRUMENT>.trades.json files. Repeat for multiple windows.",
    )
    parser.add_argument("--nav", type=float, default=100_000.0)
    parser.add_argument(
        "--exposure-scale",
        type=float,
        default=0.02,
        help="Current exposure scale used to translate gross notional into margin-style usage.",
    )
    parser.add_argument(
        "--alloc-top-k",
        type=int,
        default=32,
        help="Configured total trade cap used by live risk limits.",
    )
    parser.add_argument(
        "--target-peak-utilization-pct",
        type=float,
        default=75.0,
        help="Desired utilization at the observed historical peak overlap.",
    )
    parser.add_argument(
        "--hard-cap-utilization-pct",
        type=float,
        default=100.0,
        help="Absolute maximum allowed utilization at the configured alloc_top_k ceiling.",
    )
    parser.add_argument(
        "--projected-gross-pct",
        type=float,
        default=None,
        help="Optional projected gross %% NAV per trade for a candidate live sizing policy.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text report.",
    )
    args = parser.parse_args()

    audits = [
        _window_audit(
            Path(window_dir),
            nav=float(args.nav),
            exposure_scale=float(args.exposure_scale),
            instruments=INSTRUMENTS,
        )
        for window_dir in args.window_dir
    ]

    observed_peak = max((audit.max_concurrent for audit in audits), default=0)
    recs = _recommendations(
        observed_peak_concurrent=observed_peak,
        alloc_top_k=int(args.alloc_top_k),
        target_peak_utilization_pct=float(args.target_peak_utilization_pct) / 100.0,
        hard_cap_utilization_pct=float(args.hard_cap_utilization_pct) / 100.0,
    )

    if args.json:
        payload = {
            "nav": float(args.nav),
            "exposure_scale": float(args.exposure_scale),
            "alloc_top_k": int(args.alloc_top_k),
            "observed_peak_concurrent": observed_peak,
            "windows": [audit.__dict__ for audit in audits],
            "recommendations": recs,
        }
        if args.projected_gross_pct is not None:
            projected = float(args.projected_gross_pct)
            payload["projected_gross_pct"] = projected
            payload["projected_peak_utilization_by_window"] = {
                audit.window: audit.max_concurrent * projected for audit in audits
            }
            payload["projected_hard_cap_utilization"] = int(args.alloc_top_k) * projected
        print(json.dumps(payload, indent=2))
        return 0

    print("=" * 80)
    print("PORTFOLIO OVERLAP AUDIT")
    print("=" * 80)
    print(f"NAV: ${float(args.nav):,.2f}")
    print(f"Exposure scale: {float(args.exposure_scale):.6f}")
    print(f"Configured total trade cap (alloc_top_k): {int(args.alloc_top_k)}")
    print("")

    for audit in audits:
        print(f"[{audit.window}]")
        print(f"  Total trades: {audit.total_trades}")
        print(f"  Max concurrent trades: {audit.max_concurrent}")
        print(f"  Time-weighted average concurrent trades: {audit.avg_concurrent:.2f}")
        print(f"  Time flat: {audit.pct_flat:.2f}%")
        print(
            "  Peak gross notional: "
            f"${audit.peak_notional_usd:,.2f} "
            f"({audit.peak_notional_pct_nav:.2f}% NAV)"
        )
        print(
            "  Peak margin-style utilization at current exposure_scale: "
            f"{audit.peak_margin_pct_nav:.2f}% NAV"
        )
        print(f"  Peak timestamp: {audit.peak_timestamp}")
        print(f"  Max per-instrument stacking: {audit.max_by_instrument}")
        print("")

    print("Recommendations")
    print(
        f"  Gross %/trade for target peak utilization: {recs['gross_pct_for_target_peak']:.4f}%"
    )
    print(
        f"  Gross %/trade hard-cap limit at alloc_top_k={int(args.alloc_top_k)}: "
        f"{recs['gross_pct_hard_cap_limit']:.4f}%"
    )
    print(f"  Recommended gross %/trade: {recs['recommended_gross_pct']:.4f}%")
    if args.projected_gross_pct is not None:
        projected = float(args.projected_gross_pct)
        print("")
        print(f"Projected utilization at {projected:.4f}% gross NAV per trade")
        for audit in audits:
            print(
                f"  {audit.window}: {audit.max_concurrent * projected:.2f}% NAV "
                f"at peak overlap ({audit.max_concurrent} trades)"
            )
        print(
            f"  Hard cap at alloc_top_k={int(args.alloc_top_k)}: "
            f"{int(args.alloc_top_k) * projected:.2f}% NAV"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
