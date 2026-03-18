"""Runtime pricing helpers shared by execution-side trackers."""

from __future__ import annotations


import math


def calculate_commission(
    instrument: str, price: float, units: int, cost_bps: float
) -> float:
    if cost_bps <= 0 or units <= 0:
        return 0.0
    notional = abs(float(price) * units)
    raw = notional * (cost_bps / 10_000.0)
    return convert_to_usd(instrument, raw, price)


def convert_to_usd(instrument: str, raw: float, price: float) -> float:
    inst = instrument.upper()
    if inst.endswith("_USD"):
        return raw
    if inst.startswith("USD_"):
        if math.isclose(price, 0.0):
            return 0.0
        return raw / price
    return raw
