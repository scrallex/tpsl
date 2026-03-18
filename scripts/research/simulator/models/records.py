import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple

from scripts.tools.serialization_mixin import JsonSerializable
from scripts.trading.tpsl import TPSLConfig
from scripts.research.simulator.metrics import TPSLSimulationMetrics
from .params import TPSLSimulationParams


@dataclass
class TPSLTradeRecord(JsonSerializable):
    """Extended trade record with TP/SL exit metadata."""

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


@dataclass
class TPSLSimulationResult:
    instrument: str
    params: TPSLSimulationParams
    tpsl_config: TPSLConfig
    metrics: TPSLSimulationMetrics
    trades: List[TPSLTradeRecord]
    equity_curve: List[Tuple[datetime, float]]
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument": self.instrument,
            "params": dataclasses.asdict(self.params),
            "tpsl_config": dataclasses.asdict(self.tpsl_config),
            "metrics": self.metrics.to_dict(),
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [
                {"time": ts.isoformat(), "equity": float(v)}
                for ts, v in self.equity_curve
            ],
            "source": self.source,
        }
