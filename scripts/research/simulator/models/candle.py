from dataclasses import dataclass
from datetime import datetime


@dataclass
class OHLCCandle:
    """Candle with full OHLC data for intra-bar TP/SL."""

    time: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def mid(self) -> float:
        return self.close
