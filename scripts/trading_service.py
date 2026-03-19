#!/usr/bin/env python3
"""Lean bootstrapper for the simplified SEP trading system."""
from __future__ import annotations


import argparse
import json
from datetime import datetime, timezone
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import yaml

try:
    import redis  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    redis = None

from scripts.trading import oanda as oanda_service
from scripts.trading.log_formatters import configure_logging
from scripts.trading.evidence_cache import EvidenceTracker
from scripts.trading.api import start_http_server
from scripts.trading.pricing_cache import PriceHistoryCache
from scripts.trading.backtest_manager import BacktestManager
from scripts.trading.portfolio_manager import (
    PortfolioManager,
    StrategyProfile,
)
from scripts.trading.risk_limits import RiskLimits, RiskManager

MAX_PRICE_HISTORY_POINTS = 500
DEFAULT_CANDLE_FETCH_COUNT = 200
DEFAULT_LIVE_INSTRUMENTS = (
    "AUD_USD",
    "EUR_USD",
    "GBP_USD",
    "NZD_USD",
    "USD_CAD",
    "USD_CHF",
    "USD_JPY",
)


logger = configure_logging(__name__)


class TradingService:
    """Orchestrates the OANDA connector, portfolio manager, and HTTP API."""

    def __init__(
        self, *, read_only: bool = False, enabled_pairs: Optional[Iterable[str]] = None
    ) -> None:
        self.read_only = read_only
        self.trading_active = False
        self.running = False

        self.strategy_profile_path = Path(
            os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml")
        )
        profile = StrategyProfile.load(self.strategy_profile_path)
        default_pairs = sorted(profile.instruments.keys()) or list(DEFAULT_LIVE_INSTRUMENTS)
        self.enabled_pairs = list(
            dict.fromkeys(inst.upper() for inst in (enabled_pairs or default_pairs))
        )

        self.oanda = oanda_service.OandaConnector(read_only=read_only)
        self.risk_manager = RiskManager(RiskLimits.from_env())
        self.portfolio_manager = PortfolioManager(self)

        redis_url = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL")
        from scripts.trading.state_manager import StateManager

        self.state_manager = StateManager(redis_url)
        ttl_seconds = int(os.getenv("PRICING_HISTORY_TTL", "300") or 300)
        max_points = int(
            os.getenv("PRICING_HISTORY_MAX_POINTS", str(MAX_PRICE_HISTORY_POINTS))
            or MAX_PRICE_HISTORY_POINTS
        )
        self.price_history_cache = PriceHistoryCache(
            redis_url, ttl_seconds=ttl_seconds, max_points=max_points
        )
        self.backtest_manager = BacktestManager(
            self.portfolio_manager, self.enabled_pairs
        )

        self.evidence_cache = EvidenceTracker()

        self._api_server = None
        self._shutdown = False

    @property
    def kill_switch_enabled(self) -> bool:
        return (
            self.state_manager.kill_switch_enabled
            if hasattr(self, "state_manager")
            else True
        )

    def _sync_trading_state(self) -> None:
        self.trading_active = bool(
            self.running and not self.read_only and not self.kill_switch_enabled
        )

    def set_kill_switch(self, enabled: bool) -> bool:
        flag = self.state_manager.set_kill_switch(enabled)
        self._sync_trading_state()
        return flag

    # ------------------------------------------------------------------
    # Metrics + diagnostics
    # ------------------------------------------------------------------
    def nav_metrics(self) -> Dict[str, object]:
        from scripts.trading.api_serializers import serialize_nav_metrics

        tracker = None
        coordinator = getattr(self.portfolio_manager, "coordinator", None)
        if coordinator is not None:
            tracker = getattr(coordinator, "exposure_tracker", None)
        return serialize_nav_metrics(
            self.risk_manager,
            self.trading_active,
            self.kill_switch_enabled,
            tracker=tracker,
        )

    def gate_metrics(self) -> Dict[str, object]:
        from scripts.trading.api_serializers import serialize_gate_metrics

        return serialize_gate_metrics(self.enabled_pairs, self.portfolio_manager)

    def price_history(
        self,
        instrument: str,
        *,
        granularity: str = "M5",
        count: int = DEFAULT_CANDLE_FETCH_COUNT,
    ) -> Dict[str, object]:
        instrument_code = (instrument or "").upper()
        if not instrument_code:
            return {"instrument": instrument_code, "points": []}
        count = max(1, min(int(count or 0), MAX_PRICE_HISTORY_POINTS))
        granularity_code = granularity.upper() if granularity else "M5"
        cache: Optional[PriceHistoryCache] = getattr(self, "price_history_cache", None)
        cached_points: List[Dict[str, object]] = []
        cached_ts: Optional[float] = None
        if cache:
            cached_points, cached_ts = cache.get(instrument_code, granularity_code)

        now_ts = time.time()
        if (
            cached_points
            and cached_ts is not None
            and cache
            and (now_ts - cached_ts) < cache.ttl_seconds
        ):
            return {
                "instrument": instrument_code,
                "granularity": granularity_code,
                "points": cached_points[-count:],
                "source": "cache",
            }

        stream_series = self.stream_candle_history(
            instrument_code, granularity=granularity_code, count=count
        )
        if stream_series:
            series = [
                {"time": point["time"], "close": point["close"]}
                for point in stream_series
                if point.get("close") is not None and point.get("time") is not None
            ]
            if series:
                if cache:
                    cache.set(instrument_code, granularity_code, series)
                return {
                    "instrument": instrument_code,
                    "granularity": granularity_code,
                    "points": series[-count:],
                    "source": "valkey",
                }

        candles: List[Dict[str, object]] = []
        connector = getattr(self, "oanda", None)
        if connector:
            try:
                candles = connector.get_candles(
                    instrument_code,
                    granularity=granularity_code,
                    count=max(count, DEFAULT_CANDLE_FETCH_COUNT),
                )
            except (ValueError, RuntimeError, ConnectionError):
                candles = []
        series: List[Dict[str, object]] = []
        for candle in candles or []:
            mid = candle.get("mid") or {}
            close = mid.get("c") or mid.get("close") or mid.get("C")
            try:
                price_val = float(close) if close is not None else None
            except (ValueError, TypeError):
                price_val = None
            time_str = candle.get("time") or None
            if price_val is None or time_str is None:
                continue
            series.append({"time": time_str, "close": price_val})

        if series:
            if cache:
                cache.set(instrument_code, granularity_code, series)
            return {
                "instrument": instrument_code,
                "granularity": granularity_code,
                "points": series[-count:],
                "source": "oanda",
            }

        if cached_points:
            return {
                "instrument": instrument_code,
                "granularity": granularity_code,
                "points": cached_points[-count:],
                "source": "cache_stale",
            }

        return {
            "instrument": instrument_code,
            "granularity": granularity_code,
            "points": [],
            "source": "empty",
        }

    def stream_candle_history(
        self,
        instrument: str,
        *,
        granularity: str = "S5",
        count: int = DEFAULT_CANDLE_FETCH_COUNT,
    ) -> List[Dict[str, object]]:
        instrument_code = (instrument or "").upper()
        if not instrument_code:
            return []
        granularity_code = granularity.upper() if granularity else "S5"
        fetch_count = max(1, int(count or 0))
        client = getattr(self.state_manager, "_valkey_client", None)
        if client is None:
            return []
        key = f"md:candles:{instrument_code}:{granularity_code}"
        try:
            rows = client.zrange(key, -fetch_count, -1)
        except Exception:
            logger.debug("Failed to read %s from Valkey", key, exc_info=True)
            return []

        candles: List[Dict[str, object]] = []
        for raw in rows or []:
            try:
                payload = json.loads(
                    raw if isinstance(raw, str) else raw.decode("utf-8")
                )
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            close = payload.get("c")
            if close is None and isinstance(payload.get("mid"), dict):
                close = payload["mid"].get("c")
            try:
                candle = {
                    "time": payload.get("time"),
                    "t": int(payload.get("t") or 0),
                    "open": float(payload.get("o") or payload.get("open") or 0.0),
                    "high": float(payload.get("h") or payload.get("high") or 0.0),
                    "low": float(payload.get("l") or payload.get("low") or 0.0),
                    "close": float(close or 0.0),
                }
            except (TypeError, ValueError):
                continue
            candles.append(candle)
        return candles

    def latest_backtests(self) -> Dict[str, Any]:
        return self.backtest_manager.latest_results()

    def backtest_status(self) -> Dict[str, Any]:
        return self.backtest_manager.get_status()

    def trigger_backtest(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        instruments: Optional[Iterable[str]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        return self.backtest_manager.trigger(
            start=start, end=end, instruments=instruments
        )

    def strategy_mapping(self) -> Dict[str, Dict[str, str]]:
        mapping: Dict[str, str] = {}
        for symbol, profile in sorted(self.portfolio_manager.strategy.instruments.items()):
            if profile.invert_bundles or (
                profile.hazard_min is not None and profile.hazard_max is None
            ):
                mapping[symbol] = "Mean Reversion"
            elif profile.hazard_max is not None:
                mapping[symbol] = "Momentum"
            else:
                mapping[symbol] = "Unspecified"
        return {"instrument_strategies": mapping}

    # ------------------------------------------------------------------
    # Public API used by HTTP layer and portfolio manager
    # ------------------------------------------------------------------
    def _persist_strategy_bounds(
        self,
        instrument: str,
        bounds: Dict[str, Any],
    ) -> None:
        raw = (
            yaml.safe_load(self.strategy_profile_path.read_text(encoding="utf-8"))
            if self.strategy_profile_path.exists()
            else {}
        )
        if not isinstance(raw, dict):
            raw = {}
        instruments = raw.setdefault("instruments", {})
        if not isinstance(instruments, dict):
            instruments = {}
            raw["instruments"] = instruments
        inst = instrument.upper()
        payload = instruments.setdefault(inst, {})
        if not isinstance(payload, dict):
            payload = {}
            instruments[inst] = payload

        if "hazard_min" in bounds:
            payload["hazard_min"] = (
                float(bounds["hazard_min"])
                if bounds["hazard_min"] is not None
                else None
            )
        if "hazard_max" in bounds:
            payload["hazard_max"] = (
                float(bounds["hazard_max"])
                if bounds["hazard_max"] is not None
                else None
            )
        if "min_repetitions" in bounds:
            payload["min_repetitions"] = int(bounds["min_repetitions"])
        if "SL" in bounds or "sl_margin" in bounds:
            value = bounds["SL"] if "SL" in bounds else bounds.get("sl_margin")
            payload["stop_loss_pct"] = float(value) if value is not None else None
        if "TP" in bounds or "tp_margin" in bounds:
            value = bounds["TP"] if "TP" in bounds else bounds.get("tp_margin")
            payload["take_profit_pct"] = float(value) if value is not None else None
        if "Trail" in bounds:
            payload["trailing_stop_pct"] = (
                float(bounds["Trail"]) if bounds["Trail"] is not None else None
            )
        if "BE" in bounds:
            payload["breakeven_trigger_pct"] = (
                float(bounds["BE"]) if bounds["BE"] is not None else None
            )
        if "hold_minutes" in bounds:
            exit_payload = payload.setdefault("exit", {})
            if not isinstance(exit_payload, dict):
                exit_payload = {}
                payload["exit"] = exit_payload
            exit_payload["max_hold_minutes"] = (
                int(bounds["hold_minutes"])
                if bounds["hold_minutes"] is not None
                else None
            )
        if "guards" in bounds and isinstance(bounds["guards"], dict):
            guards = payload.setdefault("guards", {})
            if not isinstance(guards, dict):
                guards = {}
                payload["guards"] = guards
            for key, value in bounds["guards"].items():
                guards[key] = float(value) if value is not None else None

        self.strategy_profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.strategy_profile_path.write_text(
            yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _canonical_live_bounds(
        profile: Any,
        bounds: Dict[str, Any],
    ) -> Dict[str, Any]:
        canonical: Dict[str, Any] = {}
        is_mean_reversion = bool(
            getattr(profile, "invert_bundles", False)
            or (
                getattr(profile, "hazard_min", None) is not None
                and getattr(profile, "hazard_max", None) is None
            )
        )

        if "hazard_min" in bounds:
            canonical["hazard_min"] = (
                float(bounds["hazard_min"])
                if bounds["hazard_min"] is not None
                else None
            )
        elif "hazard_max" in bounds:
            canonical["hazard_max"] = (
                float(bounds["hazard_max"])
                if bounds["hazard_max"] is not None
                else None
            )
        elif "Haz" in bounds:
            key = "hazard_min" if is_mean_reversion else "hazard_max"
            canonical[key] = (
                float(bounds["Haz"]) if bounds["Haz"] is not None else None
            )

        if "min_repetitions" in bounds:
            canonical["min_repetitions"] = int(bounds["min_repetitions"])
        elif "Reps" in bounds:
            canonical["min_repetitions"] = int(bounds["Reps"])

        if "SL" in bounds or "sl_margin" in bounds:
            value = bounds["SL"] if "SL" in bounds else bounds.get("sl_margin")
            canonical["stop_loss_pct"] = float(value) if value is not None else None
        if "TP" in bounds or "tp_margin" in bounds:
            value = bounds["TP"] if "TP" in bounds else bounds.get("tp_margin")
            canonical["take_profit_pct"] = float(value) if value is not None else None
        if "Trail" in bounds:
            canonical["trailing_stop_pct"] = (
                float(bounds["Trail"]) if bounds["Trail"] is not None else None
            )
        if "BE" in bounds:
            canonical["breakeven_trigger_pct"] = (
                float(bounds["BE"]) if bounds["BE"] is not None else None
            )
        if "Hold" in bounds:
            canonical["hold_minutes"] = (
                int(float(bounds["Hold"])) if bounds["Hold"] is not None else None
            )
        elif "hold_minutes" in bounds:
            canonical["hold_minutes"] = (
                int(bounds["hold_minutes"])
                if bounds["hold_minutes"] is not None
                else None
            )

        guards: Dict[str, Any] = {}
        if "guards" in bounds and isinstance(bounds["guards"], dict):
            for key, value in bounds["guards"].items():
                guards[key] = float(value) if value is not None else None
        for raw_key, guard_key in (
            ("Coh", "min_coherence"),
            ("Ent", "max_entropy"),
            ("Stab", "min_stability"),
        ):
            if raw_key in bounds:
                value = bounds[raw_key]
                guards[guard_key] = float(value) if value is not None else None
        if guards:
            canonical["guards"] = guards

        return canonical

    def update_strategy_bounds(self, instrument: str, bounds: Dict[str, Any]) -> bool:
        """Dynamically apply optimal GPU parameters to the live strategy profile."""
        try:
            profile = self.portfolio_manager.strategy.get(instrument.upper())
            if profile is None:
                return False

            canonical = self._canonical_live_bounds(profile, bounds)
            self._persist_strategy_bounds(instrument, canonical)

            if "hazard_min" in canonical:
                profile.hazard_min = canonical["hazard_min"]
            if "hazard_max" in canonical:
                profile.hazard_max = canonical["hazard_max"]
            if "min_repetitions" in canonical:
                profile.min_repetitions = canonical["min_repetitions"]
            if "stop_loss_pct" in canonical:
                profile.stop_loss_pct = canonical["stop_loss_pct"]
            if "take_profit_pct" in canonical:
                profile.take_profit_pct = canonical["take_profit_pct"]
            if "trailing_stop_pct" in canonical:
                profile.trailing_stop_pct = canonical["trailing_stop_pct"]
            if "breakeven_trigger_pct" in canonical:
                profile.breakeven_trigger_pct = canonical["breakeven_trigger_pct"]
            if "hold_minutes" in canonical:
                profile.hold_minutes = canonical["hold_minutes"]
            if "guards" in canonical:
                for key, value in canonical["guards"].items():
                    if key in profile.guards:
                        profile.guards[key] = value

            logger.info("Applied live GPU bounds to %s: %s", instrument, bounds)
            return True
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.error("Failed to update strategy bounds for %s: %s", instrument, e)
            return False

    def get_pricing(self, instruments: Iterable[str]) -> Dict[str, Dict[str, float]]:
        return oanda_service.pricing(self, list(instruments or []))

    def get_oanda_positions(self) -> List[Dict[str, object]]:
        return oanda_service.positions(self)

    def get_oanda_open_trades(
        self, instruments: Optional[Iterable[str]] = None
    ) -> List[Dict[str, object]]:
        return oanda_service.open_trades(self, instruments)

    def get_oanda_account_info(self) -> Dict[str, object]:
        return oanda_service.account_info(self)

    def fetch_and_store_candles(
        self,
        instrument: str,
        granularity: str = "M5",
        count: int = DEFAULT_CANDLE_FETCH_COUNT,
    ) -> bool:
        # Manual HTTP/API diagnostic path; the live trading loop reads streamed S5 candles.
        return oanda_service.fetch_and_store_candles(
            self, instrument, granularity, count
        )

    def fetch_candles_for_enabled_pairs(
        self, granularity: str = "M5", count: int = DEFAULT_CANDLE_FETCH_COUNT
    ) -> None:
        # Manual HTTP/API diagnostic path; the live trading loop reads streamed S5 candles.
        oanda_service.fetch_candles_for_enabled_pairs(self, granularity, count)

    def place_order(
        self,
        instrument: str,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Dict[str, object]:
        if self.kill_switch_enabled:
            return {"ok": False, "error": "kill_switch"}
        if self.read_only or self.oanda.read_only:
            return {"ok": False, "error": "read_only"}
        response = (
            oanda_service.submit_market_order(
                self, instrument, units, stop_loss, take_profit
            )
            or {}
        )
        if current_price is not None:
            self.risk_manager.record_fill(instrument, units, current_price)
        return {"ok": bool(response), "response": response}

    def close_position(
        self, instrument: str, units: Optional[str] = None
    ) -> Dict[str, object]:
        if self.kill_switch_enabled:
            return {"ok": False, "error": "kill_switch"}
        response = oanda_service.close_position(self, instrument, units)
        return {"ok": bool(response), "response": response}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, *, start_api: bool = True) -> None:
        if self.running:
            return

        if self.read_only and not self.kill_switch_enabled:
            logger.info(
                "READ_ONLY mode active; kill switch cleared but trading loop remains disabled"
            )
        elif self.read_only and self.kill_switch_enabled:
            logger.info("READ_ONLY mode active; kill switch engaged")
        self.running = True
        try:
            self.portfolio_manager.reconcile_portfolio()
        except (ValueError, TypeError, RuntimeError):
            logger.warning(
                "Portfolio reconciliation failed during startup", exc_info=True
            )
        self.portfolio_manager.start()
        self._sync_trading_state()
        if start_api:
            host = os.getenv("HTTP_HOST", "0.0.0.0")
            port = int(os.getenv("HTTP_PORT", "8000") or 8000)
            self._api_server = start_http_server(self, host, port)
        logger.info("Trading service started (API=%s)", bool(start_api))

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.set_kill_switch(True)
        self.portfolio_manager.stop()
        if self._api_server:
            try:
                self._api_server.shutdown()
            except (OSError, RuntimeError):
                pass
        logger.info("Trading service stopped")

    def signal_outcomes(self) -> Dict[str, Any]:
        return self.evidence_cache.get_signal_outcomes()

    def regime_roc_summary(self) -> Dict[str, Any]:
        return self.evidence_cache.get_regime_roc_summary()


# =============================================================================
# CLI entry point
# =============================================================================


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SEP trading service")
    parser.add_argument(
        "--read-only", action="store_true", help="disable live order placement"
    )
    parser.add_argument(
        "--pairs", type=str, help="comma separated instrument whitelist"
    )
    parser.add_argument(
        "--no-api", action="store_true", help="do not start HTTP API server"
    )
    return parser.parse_args()


def _install_signal_handlers(service: TradingService) -> None:
    def _shutdown(signum, frame):  # type: ignore[override]
        logger.info("Signal %s received; shutting down", signum)
        service.stop()
        service._shutdown = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (OSError, ValueError):  # pragma: no cover
            pass


def main() -> int:
    args = _parse_cli()
    pairs = args.pairs.split(",") if args.pairs else None
    service = TradingService(read_only=args.read_only, enabled_pairs=pairs)
    _install_signal_handlers(service)
    service.start(start_api=not args.no_api)
    logger.info("Service running. Press Ctrl+C to stop.")
    try:
        while not service._shutdown:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
