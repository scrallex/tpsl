#!/usr/bin/env python3
"""Gate loading and strategy profile parsing."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import yaml

try:
    import redis  # type: ignore
except ImportError:
    redis = None

from scripts.trading.session_policy import SessionWindow

logger = logging.getLogger(__name__)

_GUARD_KEYS = (
    "min_coherence",
    "min_stability",
    "max_entropy",
    "max_coherence_tau_slope",
    "max_domain_wall_slope",
    "min_low_freq_share",
    "max_reynolds_ratio",
    "min_temporal_half_life",
    "min_spatial_corr_length",
    "min_pinned_alignment",
)


@dataclass
class StrategyInstrument:
    symbol: str
    hazard_max: Optional[float]
    min_repetitions: int
    guards: Dict[str, Optional[float]]
    session: Optional[SessionWindow]
    hazard_min: Optional[float] = None
    semantic_filter: List[str] = field(default_factory=list)
    regime_filter: List[str] = field(default_factory=list)
    min_regime_confidence: float = 0.0
    invert_bundles: bool = False
    ml_primary_gate: bool = False
    allow_fallback: bool = True
    disable_bundle_overrides: bool = False
    bundle_overrides: Dict[str, "BundleDirective"] = field(default_factory=dict)
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    breakeven_trigger_pct: Optional[float] = None
    hold_minutes: Optional[int] = None


@dataclass
class BundleDirective:
    bundle_id: str
    enabled: bool = True
    min_score: float = 0.0
    exposure_multiplier: float = 1.0
    hold_minutes: Optional[int] = None


@dataclass
class StrategyProfile:
    instruments: Dict[str, StrategyInstrument]
    global_defaults: Dict[str, Any]
    bundle_defaults: Dict[str, BundleDirective]

    @classmethod
    def load(cls, path: Path) -> "StrategyProfile":
        """Load the strategy profile configuration from a YAML file.

        Args:
            path: Path to the configuration file.

        Returns:
            A populated StrategyProfile wrapper enclosing the rules.
        """
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        bundle_defaults = _bundle_directives_from_spec(data.get("bundles"), base=None)
        instruments: Dict[str, StrategyInstrument] = {}
        for symbol, payload in (data.get("instruments") or {}).items():
            session = (
                SessionWindow.from_spec(payload.get("session"))
                if isinstance(payload, dict)
                else None
            )
            guard_spec = payload.get("guards", {}) if isinstance(payload, dict) else {}
            bundle_overrides = _bundle_directives_from_spec(
                payload.get("bundles"), base=bundle_defaults
            )
            instruments[symbol.upper()] = StrategyInstrument(
                symbol=symbol.upper(),
                hazard_max=_maybe_float(payload, "hazard_max"),
                hazard_min=_maybe_float(payload, "hazard_min"),
                min_repetitions=int(
                    payload.get(
                        "min_repetitions",
                        data.get("global", {}).get("min_repetitions", 1),
                    )
                ),
                guards=_guard_values(guard_spec),
                session=session,
                semantic_filter=_normalise_semantic_filter(
                    payload.get("semantic_filter")
                ),
                regime_filter=_normalise_semantic_filter(
                    payload.get("regime_filter")
                    or data.get("global", {}).get("regime_filter")
                ),
                min_regime_confidence=float(
                    payload.get(
                        "min_regime_confidence",
                        data.get("global", {}).get("min_regime_confidence", 0.0),
                    )
                    or 0.0
                ),
                invert_bundles=bool(payload.get("invert_bundles", False)),
                ml_primary_gate=bool(
                    payload.get(
                        "ml_primary_gate",
                        data.get("global", {}).get("ml_primary_gate", False),
                    )
                ),
                allow_fallback=payload.get("allow_fallback", True),
                disable_bundle_overrides=bool(
                    payload.get("disable_bundle_overrides", False)
                ),
                bundle_overrides=bundle_overrides,
                stop_loss_pct=_maybe_float(payload, "stop_loss_pct"),
                take_profit_pct=_maybe_float(payload, "take_profit_pct"),
                trailing_stop_pct=_maybe_float(payload, "trailing_stop_pct"),
                breakeven_trigger_pct=_maybe_float(payload, "breakeven_trigger_pct"),
                hold_minutes=_maybe_int_value(
                    (payload.get("exit", {}) if isinstance(payload, dict) else {}).get(
                        "max_hold_minutes"
                    )
                ),
            )
        return cls(
            instruments=instruments,
            global_defaults=data.get("global", {}),
            bundle_defaults=bundle_defaults,
        )

    def get(self, symbol: str) -> StrategyInstrument:
        """Retrieve instrument-specific strategy bounds and session parameters.

        Creates and caches a default profile if one does not exist.

        Args:
            symbol: The instrument symbol.

        Returns:
            The specific configuration payload for the requested instrument.
        """
        key = symbol.upper()
        if key not in self.instruments:
            guard_defaults = self.global_defaults.get("guard_thresholds", {})
            self.instruments[key] = StrategyInstrument(
                symbol=key,
                hazard_max=_maybe_float(self.global_defaults, "hazard_max"),
                hazard_min=_maybe_float(self.global_defaults, "hazard_min"),
                min_repetitions=int(self.global_defaults.get("min_repetitions", 1)),
                guards=_guard_values(guard_defaults),
                session=None,
                semantic_filter=_normalise_semantic_filter(
                    self.global_defaults.get("semantic_filter")
                ),
                regime_filter=_normalise_semantic_filter(
                    self.global_defaults.get("regime_filter")
                ),
                min_regime_confidence=float(
                    self.global_defaults.get("min_regime_confidence", 0.0) or 0.0
                ),
                invert_bundles=bool(self.global_defaults.get("invert_bundles", False)),
                ml_primary_gate=bool(
                    self.global_defaults.get("ml_primary_gate", False)
                ),
                allow_fallback=self.global_defaults.get("allow_fallback", True),
                disable_bundle_overrides=bool(
                    self.global_defaults.get("disable_bundle_overrides", False)
                ),
                bundle_overrides={},
            )
        return self.instruments[key]

    def bundle_directive(
        self, symbol: str, bundle_id: str
    ) -> Optional[BundleDirective]:
        """Fetch the strategy bundle logic constraints for a given instrument.

        Resolves overrides applicable to the strategy instrument fallback chain.

        Args:
            symbol: The instrument symbol.
            bundle_id: The specific bundle identifier.

        Returns:
            BundleDirective rules if enabled, None otherwise.
        """
        key = bundle_id.upper()
        inst = self.get(symbol)
        override = inst.bundle_overrides.get(key)
        base = self.bundle_defaults.get(key)
        directive = override or base
        if directive and directive.enabled:
            return directive
        return None


def _maybe_float(payload: Any, key: str) -> Optional[float]:
    if isinstance(payload, dict) and payload.get(key) is not None:
        try:
            return float(payload[key])
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict) and payload is not None and key == "hazard_max":
        try:
            return float(payload)
        except (ValueError, TypeError):
            return None
    return None


def _guard_values(source: Any) -> Dict[str, Optional[float]]:
    mapping: Mapping[str, Any] = source if isinstance(source, Mapping) else {}
    return {key: _maybe_float(mapping, key) for key in _GUARD_KEYS}


def _normalise_semantic_filter(payload: Any) -> List[str]:
    if payload is None:
        return []
    items = (
        [payload]
        if isinstance(payload, str)
        else list(payload) if isinstance(payload, Sequence) else []
    )
    tags: List[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        tag = item.strip()
        if tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags


def _bundle_directives_from_spec(
    spec: Any,
    *,
    base: Optional[Mapping[str, BundleDirective]] = None,
) -> Dict[str, BundleDirective]:
    directives: Dict[str, BundleDirective] = {}
    if not isinstance(spec, Mapping):
        return directives
    for raw_id, payload in spec.items():
        bundle_id = str(raw_id).strip().upper()
        if not bundle_id:
            continue
        base_directive = base.get(bundle_id) if base else None
        directives[bundle_id] = _bundle_directive_from_payload(
            bundle_id, payload, base_directive
        )
    return directives


def _bundle_directive_from_payload(
    bundle_id: str,
    payload: Any,
    base: Optional[BundleDirective],
) -> BundleDirective:
    directive = base or BundleDirective(bundle_id=bundle_id)
    if payload is None:
        return directive
    if not isinstance(payload, Mapping):
        return replace(directive, bundle_id=bundle_id, enabled=bool(payload))
    enabled_raw = payload.get("enabled")
    min_score_raw = payload.get("min_score")
    exposure_raw = payload.get("exposure_multiplier")
    hold_raw = payload.get("hold_minutes")
    enabled = directive.enabled if enabled_raw is None else bool(enabled_raw)
    min_score = (
        directive.min_score
        if min_score_raw is None
        else _coerce_float(min_score_raw, directive.min_score)
    )
    exposure_multiplier = (
        directive.exposure_multiplier
        if exposure_raw is None
        else _coerce_float(exposure_raw, directive.exposure_multiplier)
    )
    hold_override = _maybe_int_value(hold_raw)
    hold_minutes = (
        hold_override if hold_override is not None else directive.hold_minutes
    )
    return BundleDirective(
        bundle_id=bundle_id,
        enabled=enabled,
        min_score=min_score,
        exposure_multiplier=exposure_multiplier,
        hold_minutes=hold_minutes,
    )


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _maybe_int_value(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class GateLoader:
    """Thin Valkey loader for gate payloads combined with profile loader."""

    def __init__(self, redis_url: Optional[str]) -> None:
        self._client = None
        if redis and redis_url:
            try:
                self._client = redis.from_url(redis_url)
            except Exception:
                self._client = None

    def load(self, instruments: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch the latest manifold gates for multiple instruments from Valkey cache.

        Args:
            instruments: Collection of instrument symbols.

        Returns:
            A dictionary mapping instruments to their encoded gate properties.
        """
        if not self._client:
            return {}
        pipe = self._client.pipeline()
        keys = [f"gate:last:{inst.upper()}" for inst in instruments]
        for key in keys:
            pipe.get(key)
        results = pipe.execute()
        payloads: Dict[str, Dict[str, Any]] = {}
        for inst, raw in zip(instruments, results):
            try:
                if not raw:
                    continue
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                payloads[inst.upper()] = data
            except Exception:
                continue
        return payloads
