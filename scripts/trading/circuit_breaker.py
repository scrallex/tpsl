import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from scripts.tools.serialization_mixin import JsonSerializable

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

logger = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Thresholds that trigger an automatic kill-switch engagement."""

    daily_loss_pct: float = 0.03  # 3% of NAV
    consecutive_losses: int = 5  # N consecutive losing trades
    max_drawdown_pct: float = 0.10  # 10% from equity peak
    rapid_exposure_change_pct: float = 0.50  # 50% exposure shift in 1 hour
    max_open_positions: int = 7  # hard position count cap
    gate_stale_minutes: int = 10  # gate older than N minutes → warning
    check_interval_seconds: int = 60  # daemon loop sleep
    cooldown_minutes: int = 30  # wait after tripping before resuming

    @classmethod
    def from_yaml(cls, path: Path) -> "CircuitBreakerConfig":
        if yaml is None or not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cb = raw.get("circuit_breaker") or raw
        kwargs = {}
        for fld in cls.__dataclass_fields__:
            if fld in cb:
                try:
                    kwargs[fld] = type(cls.__dataclass_fields__[fld].default)(cb[fld])
                except Exception:
                    pass
        return cls(**kwargs)

    @classmethod
    def from_env(cls) -> "CircuitBreakerConfig":
        return cls(
            daily_loss_pct=float(os.getenv("CB_DAILY_LOSS_PCT", "0.03")),
            consecutive_losses=int(os.getenv("CB_CONSECUTIVE_LOSSES", "5")),
            max_drawdown_pct=float(os.getenv("CB_MAX_DRAWDOWN_PCT", "0.10")),
            rapid_exposure_change_pct=float(os.getenv("CB_RAPID_EXPOSURE_PCT", "0.50")),
            max_open_positions=int(os.getenv("CB_MAX_POSITIONS", "7")),
            gate_stale_minutes=int(os.getenv("CB_GATE_STALE_MIN", "10")),
            check_interval_seconds=int(os.getenv("CB_CHECK_INTERVAL", "60")),
            cooldown_minutes=int(os.getenv("CB_COOLDOWN_MIN", "30")),
        )


@dataclass
class BreachEvent(JsonSerializable):
    timestamp: datetime
    rule: str
    detail: str
    severity: str = "warning"


@dataclass
class CircuitBreakerState:
    """Mutable runtime state tracked between check cycles."""

    equity_peak: float = 0.0
    day_start_equity: float = 0.0
    day_start_date: Optional[str] = None
    consecutive_loss_count: int = 0
    last_exposure_snapshot: float = 0.0
    last_exposure_time: float = 0.0
    last_trip_time: Optional[float] = None
    breaches: List[BreachEvent] = field(default_factory=list)
    tripped: bool = False

    def reset_daily(self, equity: float) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self.day_start_date != today:
            self.day_start_date = today
            self.day_start_equity = equity
            self.consecutive_loss_count = 0

    def record_breach(self, rule: str, detail: str, severity: str = "critical") -> None:
        event = BreachEvent(
            timestamp=datetime.now(UTC),
            rule=rule,
            detail=detail,
            severity=severity,
        )
        self.breaches.append(event)
        if len(self.breaches) > 100:
            self.breaches = self.breaches[-100:]
        logger.warning("Circuit breaker breach: %s – %s", rule, detail)
