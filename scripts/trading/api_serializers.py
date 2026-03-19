#!/usr/bin/env python3
"""REST API Payload serializers for the trading system."""

import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from scripts.trading.gate_validation import (
    gate_evaluation,
    relaxed_gate_profile,
    structural_metric,
)


def serialize_nav_metrics(
    risk_manager: Any,
    trading_active: bool,
    kill_switch_enabled: bool,
    tracker: Any = None,
) -> Dict[str, Any]:
    """Serializes the current NAV and risk summary into a REST-friendly dictionary.

    Args:
        risk_manager: The active RiskManager instance.
        trading_active: Active status of trading.
        kill_switch_enabled: Current kill switch status.

    Returns:
        A dictionary containing the NAV metrics.
    """
    summary = risk_manager.get_risk_summary()
    read_only = str(os.getenv("READ_ONLY", "1")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    show_positions = trading_active and not kill_switch_enabled and not read_only
    positions = risk_manager.position_breakdown() if show_positions else []
    if show_positions:
        enriched_positions = []
        total_tickets = 0
        for position in positions:
            instrument = str(position.get("instrument") or "").upper()
            raw_tickets = []
            if tracker is not None and hasattr(tracker, "get_tickets"):
                try:
                    raw_tickets = list(tracker.get_tickets(instrument) or [])
                except Exception:
                    raw_tickets = []
            ticket_payloads = []
            for ticket in raw_tickets:
                ticket_payloads.append(
                    {
                        "units": int(getattr(ticket, "units", 0) or 0),
                        "entry_price": float(getattr(ticket, "entry_price", 0.0) or 0.0),
                        "entry_time": getattr(ticket, "entry_time", None).isoformat()
                        if getattr(ticket, "entry_time", None) is not None
                        else None,
                    }
                )
            total_tickets += len(ticket_payloads)
            enriched_positions.append(
                {
                    **position,
                    "ticket_count": len(ticket_payloads),
                    "tickets": ticket_payloads,
                }
            )
        positions = enriched_positions
    else:
        total_tickets = 0

    if not show_positions:
        summary["total_units"] = 0.0
        summary["exposure_usd"] = 0.0

    summary.update(
        {
            "positions": positions,
            "active_ticket_count": total_tickets,
            "kill_switch": kill_switch_enabled,
            "trading_active": trading_active,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return summary


def serialize_gate_metrics(
    enabled_pairs: List[str],
    portfolio_manager: Any,
) -> Dict[str, Any]:
    """Serializes the current gate and manifold metrics into a REST-friendly dictionary.

    Args:
        enabled_pairs: List of active instruments.
        portfolio_manager: The active PortfolioManager instance.

    Returns:
        A dictionary containing the gate metrics.
    """
    instruments = list(enabled_pairs or [])
    payloads: Dict[str, Dict[str, object]] = {}
    try:
        payloads = portfolio_manager.latest_gate_payloads()
        if not payloads and getattr(portfolio_manager, "gate_reader", None):
            payloads = portfolio_manager.gate_reader.load(instruments)
    except (ValueError, OSError, TypeError):
        payloads = {}

    entries: List[Dict[str, object]] = []
    reason_counts: Dict[str, int] = {}
    now = datetime.now(timezone.utc)
    for inst in instruments:
        payload = payloads.get(inst.upper()) or {}
        ts_raw = payload.get("ts_ms") or payload.get("ts")
        updated_at = None
        age = None
        if ts_raw is not None:
            updated: Optional[datetime] = None
            try:
                if isinstance(ts_raw, (int, float)) or (
                    isinstance(ts_raw, str) and ts_raw.replace(".", "", 1).isdigit()
                ):
                    ts_val = float(ts_raw)
                    if ts_val > 10_000:
                        ts_val /= 1000.0
                    updated = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                elif isinstance(ts_raw, str):
                    updated = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                updated = None
            if updated:
                updated_at = updated.isoformat()
                age = max(0.0, (now - updated).total_seconds())
        # Handle both flat mock and nested coordinator architecture
        pm_strategy = getattr(portfolio_manager, "strategy", None)
        if pm_strategy is None and hasattr(portfolio_manager, "coordinator"):
            pm_strategy = getattr(portfolio_manager.coordinator, "strategy", None)

        strategy_profile = pm_strategy.get(inst) if pm_strategy else None

        effective_profile = strategy_profile
        if strategy_profile and getattr(strategy_profile, "ml_primary_gate", False):
            effective_profile = relaxed_gate_profile(strategy_profile)

        if effective_profile is None:
            admitted = False
            reason_details = ["missing_strategy_profile"]
        else:
            admitted, reason_details = gate_evaluation(payload, effective_profile)

        reasons = [_reason_code(reason) for reason in reason_details]
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        coh_tau_slope = structural_metric(payload, "coherence_tau_slope")
        domain_wall_slope = structural_metric(payload, "domain_wall_slope")
        spectral_lowf_share = structural_metric(payload, "spectral_lowf_share")
        entries.append(
            {
                "instrument": inst,
                "admit": admitted,
                "direction": str(payload.get("direction") or "FLAT").upper(),
                "st_peak": bool(payload.get("st_peak", False)),
                "ml_probability": _maybe_float(payload.get("ml_prob")),
                "age_seconds": age,
                "updated_at": updated_at,
                "hazard": _maybe_float(payload.get("hazard")),
                "hazard_threshold": (
                    _maybe_float((payload.get("structure") or {}).get("hazard_threshold"))
                    or _maybe_float(payload.get("hazard_threshold"))
                ),
                "repetitions": payload.get("repetitions"),
                "reasons": reasons,
                "reason_details": [str(reason) for reason in reason_details],
                "regime": payload.get("regime"),
                "structure": payload.get("structure"),
                "guards": {
                    "coherence_tau_slope": coh_tau_slope,
                    "domain_wall_slope": domain_wall_slope,
                    "spectral_lowf_share": spectral_lowf_share,
                },
                "source": payload.get("source"),
                "action": payload.get("action"),
            }
        )

    sorted_counts = dict(
        sorted(reason_counts.items(), key=lambda item: (-int(item[1]), item[0]))
    )
    return {"as_of": now.isoformat(), "gates": entries, "reason_counts": sorted_counts}


def _reason_code(reason: object) -> str:
    text = str(reason or "").strip()
    if not text:
        return "unknown_reason"
    return text.split(":", 1)[0]


def _maybe_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
