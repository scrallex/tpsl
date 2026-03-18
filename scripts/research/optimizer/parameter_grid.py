#!/usr/bin/env python3
"""Parameter generation and space bounds for GPU optimizer."""

import random
from typing import Any, Dict, List, Optional


class ParameterGrid:
    @staticmethod
    def generate_combos(
        signal_type: str,
        num: int = 5000,
        anchors: Optional[List[Dict[str, Any]]] = None,
        instrument: str = "",
    ) -> List[Dict[str, Any]]:
        bounds = {}
        if signal_type == "squeeze_breakout":
            # Squeeze bounds
            bounds = {
                "Haz": (0.35, 0.60),
                "Coh": (0.05, 0.20),
                "Hold": (60, 2000),  # 5 mins to ~2.5 hours
                "SL": (0.0010, 0.0050),
                "TP": (0.0020, 0.0060),
                "Trail": (0.0, 0.0025),  # <=0 means None
                "Ent": (1.0, 1.6),
                "HazEx": (-1.0, -1.0),  # <0 means None
                "BE": (0.0005, 0.0020),
                "Reps": (1, 1),
                "Stab": (0.0, 0.0),
            }
        elif signal_type == "mean_reversion":
            is_pacific = any(p in instrument.upper() for p in ["JPY", "AUD", "NZD"])
            is_eur_usd = "EUR_USD" in instrument.upper()

            if is_pacific:
                # Pacific bounds (Unrestricted Horizon)
                bounds = {
                    "Haz": (0.60, 0.99),  # Looser entry criteria
                    "Coh": (0.01, 0.50),
                    "Hold": (30, 4000),
                    "SL": (0.0010, 0.0080),
                    "TP": (0.0010, 0.0100),
                    "Trail": (-1.0, 0.0050),
                    "Ent": (0.80, 2.50),
                    "HazEx": (-1.0, -1.0),
                    "BE": (0.0005, 0.0025),
                    "Reps": (1, 1),
                    "Stab": (0.0, 0.0),
                }
            elif is_eur_usd:
                # EUR_USD bounds (Highly Restricted to limit negative PnL / drawdowns)
                bounds = {
                    "Haz": (0.85, 0.99),  # Extremely tight entry criteria
                    "Coh": (0.05, 0.50),  # Require minimum coherence
                    "Hold": (30, 2000),  # Cap hold time to avoid long drawdowns
                    "SL": (0.0010, 0.0050),  # Tighter stop loss
                    "TP": (0.0010, 0.0100),
                    "Trail": (-1.0, 0.0050),
                    "Ent": (0.80, 1.80),  # Lower max entropy limits noise
                    "HazEx": (-1.0, -1.0),
                    "BE": (0.0005, 0.0025),
                    "Reps": (1, 1),
                    "Stab": (0.0, 0.0),
                }
            else:
                # Western bounds (Macro-Horizon with High Exhaustion)
                bounds = {
                    "Haz": (0.75, 0.99),  # Tight entry criteria
                    "Coh": (0.01, 0.50),
                    "Hold": (30, 4000),
                    "SL": (0.0010, 0.0080),
                    "TP": (0.0010, 0.0100),
                    "Trail": (-1.0, 0.0050),
                    "Ent": (0.80, 2.50),
                    "HazEx": (-1.0, -1.0),
                    "BE": (0.0005, 0.0025),
                    "Reps": (1, 1),
                    "Stab": (0.0, 0.0),
                }
        else:  # trend_sniper (Default)
            # Trend bounds
            bounds = {
                "Haz": (0.60, 0.99),  # Full usable hazard range
                "Coh": (0.01, 0.50),  # Allow lower coherence to yield more trades
                "Hold": (30, 4000),  # 2.5m to ~5.5 hours
                "SL": (0.0010, 0.0080),  # Wider stops
                "TP": (0.0030, 0.0100),  # Bigger targets
                "Trail": (0.0, 0.0050),
                "Ent": (
                    0.80,
                    2.50,
                ),  # Increased max entropy to allow more gate admissions
                "HazEx": (0.0, 0.50),
                "BE": (0.0010, 0.0050),
                "Reps": (1, 1),
                "Stab": (0.0, 0.0),
            }

        all_combos: List[Dict[str, Any]] = []
        min_rr = 0.5 if signal_type == "mean_reversion" else 1.5

        while len(all_combos) < num:
            combo: Dict[str, Any] = {}
            if anchors and random.random() < 0.80:
                anchor = random.choice(anchors)
                for k, (b_min, b_max) in bounds.items():
                    val = anchor.get(k)
                    if val is None:
                        val = -1.0 if k in ["Trail", "HazEx"] else b_min
                    range_span = b_max - b_min
                    perturb = (random.random() * 0.2 - 0.1) * range_span  # +/- 10%
                    new_val = max(b_min, min(b_max, val + perturb))

                    if k in ["Hold", "Reps"]:
                        combo[k] = int(round(new_val))
                    else:
                        combo[k] = round(new_val, 5)
            else:
                for k, (b_min, b_max) in bounds.items():
                    if k in ["Hold", "Reps"]:
                        combo[k] = random.randint(int(b_min), int(b_max))
                    else:
                        combo[k] = round(random.uniform(b_min, b_max), 5)

            if combo["Trail"] <= 0.0:
                combo["Trail"] = None
            if combo["HazEx"] < 0.0:
                combo["HazEx"] = None

            if combo["TP"] < min_rr * combo["SL"]:
                continue

            all_combos.append(combo)

        return all_combos
