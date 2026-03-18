#!/usr/bin/env python3
"""Unified health check and circuit-breaker service for the SEP live trading system.

Combines OANDA connectivity checks, gate data freshness validation, and real-time
circuit breaker logic (kill switch engagement on risk threshold breach).

Can run as:
  1. A standalone daemon (``python -m scripts.tools.health_check_service --daemon``)
  2. A one-shot check dumping a JSON report
"""
from __future__ import annotations


import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


from scripts.tools.cli_runner import CLIRunner
from scripts.trading.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerState,
    BreachEvent,
)

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)
UTC = timezone.utc


# =========================================================================
# Configuration
# =========================================================================


# =========================================================================
# Health Service
# =========================================================================


class HealthCheckService:
    """Evaluate risk conditions, connectivity, data freshness, and engage kill switch."""

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        redis_url: Optional[str] = None,
        instruments: Optional[List[str]] = None,
    ) -> None:
        self.config = config or CircuitBreakerConfig.from_env()
        self.state = CircuitBreakerState()
        self.instruments = instruments or ["EUR_USD"]
        self._redis = None
        if redis and redis_url:
            try:
                self._redis = redis.from_url(redis_url, decode_responses=True)
            except Exception:
                pass

    def check_connectivity(
        self, url_base: str = "https://api-fxtrade.oanda.com"
    ) -> bool:
        """Pings OANDA to check API Key and connectivity."""
        api_key = os.getenv("OANDA_API_KEY")
        if not api_key:
            return False

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(f"{url_base}/v3/accounts", headers=headers, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def get_gate_ages(self) -> Dict[str, float]:
        """Returns age in seconds of the latest gate payload for each instrument."""
        if not self._redis:
            return {}

        ages = {}
        now_ms = time.time() * 1000
        for inst in self.instruments:
            key = f"gate:last:{inst.upper()}"
            try:
                payload_str = self._redis.get(key)
                if payload_str:
                    payload = json.loads(payload_str)
                    ts_ms = payload.get("ts_ms")
                    if ts_ms is not None:
                        ages[inst] = (now_ms - ts_ms) / 1000.0
            except Exception:
                pass
        return ages

    def check(
        self,
        *,
        current_equity: float,
        nav: float,
        open_positions: int,
        total_exposure: float,
        recent_trade_pnls: Optional[List[float]] = None,
        gate_ages_seconds: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Run all safety checks."""
        cfg = self.config
        state = self.state
        now = time.time()

        state.reset_daily(nav)
        state.equity_peak = max(state.equity_peak, current_equity)

        trip_reasons: List[str] = []
        warnings: List[str] = []

        # 1. Daily loss
        if state.day_start_equity > 0:
            daily_loss = (
                state.day_start_equity - current_equity
            ) / state.day_start_equity
            if daily_loss >= cfg.daily_loss_pct:
                detail = f"Daily loss {daily_loss:.2%} >= {cfg.daily_loss_pct:.2%}"
                state.record_breach("daily_loss", detail)
                trip_reasons.append(detail)

        # 2. Max drawdown from peak
        if state.equity_peak > 0:
            dd = (state.equity_peak - current_equity) / state.equity_peak
            if dd >= cfg.max_drawdown_pct:
                detail = f"Drawdown {dd:.2%} >= {cfg.max_drawdown_pct:.2%}"
                state.record_breach("max_drawdown", detail)
                trip_reasons.append(detail)
            elif dd >= cfg.max_drawdown_pct * 0.7:
                warnings.append(f"Drawdown approaching limit: {dd:.2%}")

        # 3. Consecutive losses
        if recent_trade_pnls:
            streak = 0
            for pnl in reversed(recent_trade_pnls):
                if pnl < 0:
                    streak += 1
                else:
                    break
            state.consecutive_loss_count = streak
            if streak >= cfg.consecutive_losses:
                detail = f"Consecutive losses: {streak} >= {cfg.consecutive_losses}"
                state.record_breach("consecutive_losses", detail)
                trip_reasons.append(detail)

        # 4. Rapid exposure change
        if state.last_exposure_time > 0 and (now - state.last_exposure_time) <= 3600:
            if state.last_exposure_snapshot > 0:
                change = (
                    abs(total_exposure - state.last_exposure_snapshot)
                    / state.last_exposure_snapshot
                )
                if change >= cfg.rapid_exposure_change_pct:
                    detail = f"Exposure change {change:.2%} in <1hr >= {cfg.rapid_exposure_change_pct:.2%}"
                    state.record_breach("rapid_exposure", detail)
                    trip_reasons.append(detail)
        state.last_exposure_snapshot = total_exposure
        state.last_exposure_time = now

        # 5. Position count
        if open_positions > cfg.max_open_positions:
            detail = f"Open positions {open_positions} > {cfg.max_open_positions}"
            state.record_breach("max_positions", detail, severity="warning")
            warnings.append(detail)

        # 6. Gate staleness
        if gate_ages_seconds:
            stale_limit = cfg.gate_stale_minutes * 60
            for inst, age in gate_ages_seconds.items():
                if age > stale_limit:
                    detail = f"{inst} gate age {age:.0f}s > {stale_limit}s"
                    state.record_breach("gate_stale", detail, severity="warning")
                    warnings.append(detail)

        # Trip if any critical breaches
        if trip_reasons:
            self.engage_kill_switch("; ".join(trip_reasons))
            state.tripped = True
            state.last_trip_time = now

        in_cooldown = False
        if (
            state.last_trip_time
            and (now - state.last_trip_time) < cfg.cooldown_minutes * 60
        ):
            in_cooldown = True

        return {
            "tripped": state.tripped,
            "trip_reasons": trip_reasons,
            "warnings": warnings,
            "in_cooldown": in_cooldown,
            "equity_peak": state.equity_peak,
            "daily_pnl_pct": (
                (current_equity - state.day_start_equity) / state.day_start_equity
                if state.day_start_equity > 0
                else 0.0
            ),
            "consecutive_losses": state.consecutive_loss_count,
            "gate_ages": gate_ages_seconds,
            "recent_breaches": [b.to_dict() for b in state.breaches[-5:]],
        }

    def engage_kill_switch(self, reason: str) -> None:
        logger.critical("CIRCUIT BREAKER TRIPPED: %s", reason)
        if self._redis:
            try:
                self._redis.set("ops:kill_switch", "1")
                self._redis.set(
                    "ops:circuit_breaker_event",
                    json.dumps(
                        {"timestamp": datetime.now(UTC).isoformat(), "reason": reason}
                    ),
                )
            except Exception as exc:
                logger.error("Failed to set kill switch in Valkey: %s", exc)

    def reset(self) -> None:
        self.state.tripped = False
        self.state.last_trip_time = None
        self.state.breaches.clear()
        logger.info("Circuit breaker reset")

    def run_daemon(self) -> None:
        logger.info(
            "Health service daemon starting (interval=%ds)",
            self.config.check_interval_seconds,
        )
        while True:
            try:
                status = self._gather_and_check()
                if status.get("tripped"):
                    logger.critical("Kill switch engaged by circuit breaker")
            except Exception:
                logger.exception("Health validation check failed")
            time.sleep(self.config.check_interval_seconds)

    def _gather_and_check(self) -> Dict[str, Any]:
        if not self._redis:
            return {"error": "no_redis"}

        risk_raw = None
        positions_raw = None
        try:
            risk_raw = self._redis.get("ops:risk_summary")
            positions_raw = self._redis.get("ops:position_count")
        except Exception:
            pass

        risk = json.loads(risk_raw) if risk_raw else {}
        nav = float(risk.get("nav_snapshot", 0.0))
        exposure = float(risk.get("exposure_usd", 0.0))
        equity = nav

        positions = int(positions_raw) if positions_raw else 0
        gate_ages = self.get_gate_ages()
        connectivity = self.check_connectivity()

        status = self.check(
            current_equity=equity,
            nav=nav,
            open_positions=positions,
            total_exposure=exposure,
            gate_ages_seconds=gate_ages,
        )
        status["connectivity_ok"] = connectivity

        if self._redis:
            try:
                payload = {
                    "overall": (
                        "critical"
                        if status.get("tripped")
                        else (
                            "warning"
                            if status.get("warnings") or not connectivity
                            else "ok"
                        )
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "detail": status,
                }
                self._redis.set(
                    "ops:health",
                    json.dumps(payload, default=str),
                    ex=self.config.check_interval_seconds * 3,
                )
            except Exception as exc:
                logger.warning("Failed to publish health: %s", exc)

        return status


def main(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = (
        CircuitBreakerConfig.from_yaml(Path(args.config))
        if args.config
        else CircuitBreakerConfig.from_env()
    )

    instruments = []
    if args.instruments:
        for item in args.instruments:
            instruments.extend(
                [p.strip().upper() for p in item.split(",") if p.strip()]
            )

    service = HealthCheckService(
        config=config, redis_url=args.redis, instruments=instruments
    )

    if args.daemon:
        service.run_daemon()
    else:
        status = service._gather_and_check()
        print(json.dumps(status, indent=2, default=str))

    return 0


if __name__ == "__main__":
    runner = CLIRunner("SEP Health Check & Circuit Breaker")
    runner.add_redis_arg()
    runner.add_arg("--daemon", action="store_true", help="Run as continuous monitor")
    runner.add_arg("--config", default=None, help="Path to config YAML (optional)")
    runner.add_arg(
        "--instruments", type=str, nargs="+", help="Instruments to check freshness for"
    )
    runner.run(main)
