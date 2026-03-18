#!/usr/bin/env python3
"""Structural Tension (ST) filtering for gate signals.

This module provides configurable filtering of gate events based on structural
tension metrics, with support for percentile thresholds and peak detection modes.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class STFilterConfig:
    """Configuration for ST filtering."""

    percentile: Optional[float] = None  # e.g., 0.90 for 90th percentile
    peak_mode: bool = False  # If True, detect reversals from peaks


@dataclass
class STFilterResult:
    """Result of applying ST filter to gates."""

    gates: List[Dict[str, Any]]  # Modified gate list
    filtered_count: int  # Number of gates filtered out
    threshold: float  # Calculated threshold value


class StructuralTensionFilter:
    """Filter gates based on structural tension metrics."""

    def __init__(self, config: STFilterConfig):
        self.config = config

    def apply(self, gates: List[Dict[str, Any]]) -> STFilterResult:
        """Apply ST filtering with percentile and peak detection.

        Args:
            gates: List of gate dicts to filter

        Returns:
            STFilterResult with modified gates and stats
        """
        if self.config.percentile is None and not self.config.peak_mode:
            return STFilterResult(gates=gates, filtered_count=0, threshold=0.0)

        # Ensure all gates have ST values
        self._ensure_st_values(gates)

        # Calculate threshold
        st_values = [g["structural_tension"] for g in gates]
        threshold = (
            self._calculate_threshold(st_values)
            if self.config.percentile is not None
            else 0.0
        )

        # Apply filtering
        filtered_count = self._filter_gates(gates, threshold)

        pct_str = (
            f"{self.config.percentile*100:.1f}%"
            if self.config.percentile is not None
            else "PeakOnly"
        )
        logger.info(
            f"ST Filter ({pct_str}): "
            f"Threshold={threshold:.4f}, Filtered={filtered_count} gates"
        )

        return STFilterResult(
            gates=gates, filtered_count=filtered_count, threshold=threshold
        )

    def _ensure_st_values(self, gates: List[Dict[str, Any]]) -> None:
        """Ensure all gates have structural_tension calculated."""
        for g in gates:
            if "structural_tension" not in g:
                # Backfill if missing (e.g. from cache)
                reps = float(g.get("repetitions", 0))
                coh = 0.0
                haz = float(g.get("hazard", 0.0))
                comps = g.get("components") or {}
                if isinstance(comps, dict):
                    coh = float(comps.get("coherence", 0.0))
                    # If hazard not at top level, try components
                    if haz == 0.0:
                        haz = float(comps.get("hazard", 0.0))

                # ST = reps * coherence * exp(-1.0 * hazard)
                g["structural_tension"] = self._calculate_st(reps, coh, haz)

    @staticmethod
    def _calculate_st(repetitions: float, coherence: float, hazard: float) -> float:
        """Calculate structural tension metric.

        Formula: ST = repetitions × coherence × exp(-hazard)
        """
        return repetitions * coherence * math.exp(-1.0 * hazard)

    def _calculate_threshold(self, st_values: List[float]) -> float:
        """Calculate percentile threshold from ST values."""
        if not st_values or self.config.percentile is None:
            return 0.0

        sorted_values = sorted(st_values)
        idx = int(self.config.percentile * (len(sorted_values) - 1))
        return sorted_values[idx]

    def _filter_gates(self, gates: List[Dict[str, Any]], threshold: float) -> int:
        """Filter gates based on threshold and mode.

        Returns:
            Number of gates filtered out
        """
        filtered_count = 0
        prev_st = -1.0

        for g in gates:
            curr_st = g.get("structural_tension", 0.0)
            dir_str = str(g.get("direction", "FLAT")).upper()
            is_active = dir_str != "FLAT"

            # Ensure reasons is a list
            reasons = g.get("reasons", [])
            if not isinstance(reasons, list):
                reasons = [reasons] if reasons else []
                g["reasons"] = reasons

            if self.config.peak_mode:
                if is_active:
                    # Peak Mode: transition from above threshold to lower value
                    if prev_st > threshold and curr_st < prev_st:
                        pass  # Valid peak, keep admitted
                    else:
                        if g.get("admit") == 1:
                            g["admit"] = 0
                            reasons.append("st_no_peak_reversal")
                            filtered_count += 1
                            if filtered_count == 1:
                                logger.debug("ST Filter first drop: %s", g)
                    prev_st = curr_st
            else:
                # Base threshold mode
                if curr_st < threshold:
                    if g.get("admit") == 1:
                        g["admit"] = 0
                        reasons.append(f"st_below_percentile:{threshold:.4f}")
                        filtered_count += 1
                prev_st = curr_st

        return filtered_count
