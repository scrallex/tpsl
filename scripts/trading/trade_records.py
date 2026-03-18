"""Runtime trade record structures."""

from dataclasses import dataclass
from datetime import datetime

from scripts.tools.serialization_mixin import JsonSerializable


@dataclass
class TPSLTradeRecord(JsonSerializable):
    instrument: str
    entry_time: datetime
    exit_time: datetime
    direction: str
    units: int
    entry_price: float
    exit_price: float
    pnl: float
    commission: float
    mae: float = 0.0
    mfe: float = 0.0
    exit_reason: str = "hold_expiry"
    tpsl_trigger_price: float = 0.0
    is_bundle_trade: bool = False
