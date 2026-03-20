#!/usr/bin/env python3
"""Lean portfolio manager that unifies sessions, gate loading, and execution."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .gate_loader import (
    StrategyProfile,
    GateLoader,
    StrategyInstrument,
    BundleDirective,
)
from .exposure_tracker import ExposureTracker
from .trade_stack import TradeStackProcessor
from .risk_limits import RiskManager
from .trade_state import ActiveTrade, TradeStateStore
from .risk_calculator import RiskSizer
from .execution_engine import ExecutionEngine
from .session_policy import SessionPolicy
from .gate_validation import evaluate_gate_and_bundles, structural_metric
from .tpsl import TPSLConfig

logger = logging.getLogger(__name__)


def _has_bundle_hit(gate_payload: Mapping[str, Any]) -> bool:
    if not isinstance(gate_payload, Mapping):
        return False
    hits = gate_payload.get("bundle_hits")
    if not isinstance(hits, Sequence):
        return False
    for entry in hits:
        if isinstance(entry, Mapping) and str(entry.get("id") or "").strip():
            return True
    return False


def _live_ml_gate_enabled() -> bool:
    raw = str(os.getenv("LIVE_ENABLE_ML_GATE", "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class PortfolioConfig:
    profile_path: Path
    exit_buffer_minutes: int
    nav_risk_pct: float
    per_pos_pct: float
    alloc_top_k: int
    redis_url: Optional[str]
    hold_seconds: int
    loop_seconds: float
    reconcile_seconds: float

    @classmethod
    def from_env(cls) -> "PortfolioConfig":
        return cls(
            profile_path=Path(
                os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml")
            ),
            exit_buffer_minutes=int(os.getenv("SESSION_EXIT_MINUTES", "5") or 5),
            nav_risk_pct=float(os.getenv("PORTFOLIO_NAV_RISK_PCT", "0.01") or 0.01),
            per_pos_pct=float(os.getenv("PM_MAX_PER_POS_PCT", "0.01") or 0.01),
            alloc_top_k=int(os.getenv("PM_ALLOC_TOP_K", "3") or 3),
            redis_url=os.getenv("VALKEY_URL") or os.getenv("REDIS_URL"),
            hold_seconds=int(os.getenv("PM_DEFAULT_HOLD_SECONDS", "1800") or 1800),
            loop_seconds=float(os.getenv("PORTFOLIO_LOOP_SECONDS", "2.0") or 2.0),
            reconcile_seconds=float(
                os.getenv("PORTFOLIO_RECONCILE_SECONDS", "0") or 0.0
            ),
        )


class PortfolioLoopCoordinator:
    """Manages the lifecycle loop of the portfolio manager."""

    def __init__(self, service: Any, config: PortfolioConfig) -> None:
        self.svc = service
        self.config = config
        self.strategy = StrategyProfile.load(self.config.profile_path)

        enabled_pairs = sorted(
            {inst.upper() for inst in getattr(service, "enabled_pairs", [])}
            or list(self.strategy.instruments)
        )
        self.enabled_instruments: List[str] = enabled_pairs

        sessions = {
            symbol: inst.session
            for symbol, inst in self.strategy.instruments.items()
            if inst.session is not None
        }
        self.session_policy = SessionPolicy(
            sessions,
            exit_buffer_minutes=self.config.exit_buffer_minutes,
        )

        self.trade_state = TradeStateStore()
        from .trade_planner import TradePlanner

        self.trade_planner = TradePlanner(self.trade_state)

        self.risk_manager: RiskManager = service.risk_manager
        self.risk_sizer = RiskSizer(
            nav_risk_pct=self.config.nav_risk_pct,
            per_position_pct_cap=self.config.per_pos_pct,
            alloc_top_k=self.config.alloc_top_k,
        )
        self.execution_engine = ExecutionEngine(
            risk_manager=self.risk_manager,
            trade_state=self.trade_state,
            risk_sizer=self.risk_sizer,
            trade_planner=self.trade_planner,
            cost_bps=1.5,
        )

        self.gate_loader = GateLoader(self.config.redis_url)
        self.exposure_tracker = ExposureTracker(service, self.risk_manager)

        self.trade_stack = TradeStackProcessor(
            self.strategy,
            self.session_policy,
            self.risk_manager,
            self.trade_state,
            self.risk_sizer,
            self.execution_engine,
            self.config.hold_seconds,
        )

        from .ml_evaluator import MLEvaluator

        self.ml_gate_enabled = _live_ml_gate_enabled()
        self.ml_evaluator = MLEvaluator(
            self.enabled_instruments,
            enabled=self.ml_gate_enabled,
        )

        self.loop_seconds = self.config.loop_seconds
        self.exposure_scale = float(os.getenv("EXPOSURE_SCALE", "0.02") or 0.02)
        self._reconcile_interval = (
            self.config.reconcile_seconds if self.config.reconcile_seconds > 0 else 0.0
        )
        self._last_reconcile = 0.0
        self._last_gate_payloads: Dict[str, Dict[str, Any]] = {}
        self._previous_active_st: Dict[str, float] = {}
        self._last_gate_ts: Dict[str, int] = {}
        self._last_peak_eval: Dict[str, bool] = {}
        self._regime_reps: Dict[str, int] = {}
        self._last_regime_label: Dict[str, str] = {}
        self._regime_start_ts: Dict[str, int] = {}
        self._sma_cache_ts: Dict[str, float] = {}
        self._sma_cache_val: Dict[str, tuple] = {}

    def _publish_risk_snapshot(self) -> None:
        client = getattr(getattr(self.svc, "state_manager", None), "_valkey_client", None)
        if client is None:
            return
        ttl_seconds = max(10, int(self.loop_seconds * 5))
        summary = self.risk_manager.get_risk_summary()
        try:
            client.set("ops:risk_summary", json.dumps(summary), ex=ttl_seconds)
            client.set(
                "ops:position_count",
                str(len(self.risk_manager.positions())),
                ex=ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Failed to publish risk telemetry: %s", exc)

    def _latest_stream_candle(
        self, instrument: str, granularity: str = "S5"
    ) -> Optional[Dict[str, Any]]:
        history = getattr(self.svc, "stream_candle_history", None)
        if not callable(history):
            return None
        candles = history(instrument, granularity=granularity, count=1)
        if not candles:
            return None
        return dict(candles[-1])

    def _regime_label_from_stream(
        self, instrument: str, profile: StrategyInstrument
    ) -> Optional[str]:
        history = getattr(self.svc, "stream_candle_history", None)
        if not callable(history):
            return None
        candles = history(instrument, granularity="S5", count=8640)
        if not candles:
            return None
        candle_ts = int(candles[-1].get("t") or 0)
        cached_ts = int(self._sma_cache_ts.get(instrument, 0.0) or 0.0)
        if candle_ts and candle_ts == cached_ts and instrument in self._sma_cache_val:
            return str(self._sma_cache_val[instrument][0])

        closes = [
            float(c.get("close", 0.0))
            for c in candles
            if c.get("close") is not None
        ]
        if len(closes) < 2:
            return None
        window_ticks = min(8640, max(100, len(closes) // 4))
        if len(closes) < window_ticks + 1:
            return None
        sma = sum(closes[-window_ticks:]) / float(window_ticks)
        is_above = closes[-1] >= sma
        invert = getattr(profile, "invert_bundles", False)
        label = (
            "short_ok" if is_above else "long_ok"
        ) if invert else ("long_ok" if is_above else "short_ok")
        self._sma_cache_val[instrument] = (label, sma)
        self._sma_cache_ts[instrument] = float(candle_ts or time.time())
        return label

    @staticmethod
    def _apply_regime_label(payload: Dict[str, Any], label: str) -> None:
        if "regime" not in payload:
            payload["regime"] = {"label": label}
        elif isinstance(payload["regime"], str):
            payload["regime"] = {"label": label}
        else:
            payload["regime"]["label"] = label

    @staticmethod
    def _tpsl_config_for(profile: Optional[StrategyInstrument]) -> TPSLConfig:
        if profile is None:
            return TPSLConfig()
        return TPSLConfig(
            stop_loss_pct=profile.stop_loss_pct,
            take_profit_pct=profile.take_profit_pct,
            trailing_stop_pct=profile.trailing_stop_pct,
            breakeven_trigger_pct=profile.breakeven_trigger_pct,
        )

    def loop_once(self) -> None:
        if not self.enabled_instruments:
            return

        loop_started = time.time()
        if (
            self._reconcile_interval > 0
            and (loop_started - self._last_reconcile) >= self._reconcile_interval
        ):
            try:
                self.reconcile_portfolio()
            except Exception:
                logger.warning("Periodic reconcile failed", exc_info=True)
            else:
                self._last_reconcile = loop_started

        gate_payloads = self.gate_loader.load(self.enabled_instruments)

        # Inject Structural Tension Peak tracking into payloads
        if gate_payloads:
            for inst, payload in gate_payloads.items():
                if not payload:
                    continue
                regime_label = payload.get("regime", {}).get("label")
                gate_ts_ms = int(payload.get("ts_ms", 0))
                raw_reps = payload.get("repetitions")
                try:
                    reps = max(1.0, float(raw_reps))
                except (TypeError, ValueError):
                    if regime_label == self._last_regime_label.get(inst):
                        elapsed_ms = gate_ts_ms - self._regime_start_ts.get(
                            inst, gate_ts_ms
                        )
                        reps = 1.0 + (float(elapsed_ms) / 80000.0)
                    else:
                        self._regime_start_ts[inst] = gate_ts_ms
                        self._last_regime_label[inst] = regime_label
                        reps = 1.0
                haz = float(payload.get("hazard", 0.0))

                coh = 0.0
                comps = payload.get("structure") or payload.get("components") or {}
                if isinstance(comps, dict):
                    coh = float(comps.get("coherence", 0.0))
                    if haz == 0.0:
                        haz = float(comps.get("hazard", 0.0))

                current_st = reps * coh * math.exp(-1.0 * haz)
                prev_st = self._previous_active_st.get(inst, -1.0)

                dir_str = str(payload.get("direction", "FLAT")).upper()
                is_active = dir_str != "FLAT"

                gate_ts_ms = int(payload.get("ts_ms", 0))
                last_ts_ms = self._last_gate_ts.get(inst, 0)

                if gate_ts_ms != last_ts_ms:
                    is_st_peak = False
                    if is_active:
                        if prev_st > 0.0 and current_st < prev_st:
                            is_st_peak = True
                        self._previous_active_st[inst] = current_st
                    self._last_peak_eval[inst] = is_st_peak
                    self._last_gate_ts[inst] = gate_ts_ms

                payload["st_peak"] = self._last_peak_eval.get(inst, False)

                # Phase 9: Western Macro-Alignment Overwrite
                # The live stream generates Manifold regimes ('trend_bull'), but the backtest
                # exported configurations that evaluate 'long_ok'/'short_ok' based on a 200 SMA.
                # Here we compute the SMA natively and overwrite the generic regime label
                # so the downstream trade stack evaluates the exact same logic constraint.
                profile = self.strategy.get(inst)
                try:
                    if getattr(profile, "regime_filter", None) and not getattr(
                        profile, "ml_primary_gate", False
                    ):
                        label_str = self._regime_label_from_stream(inst, profile)
                        if label_str:
                            self._apply_regime_label(payload, label_str)
                except Exception as e:
                    logger.warning("Failed to map SMA regime for %s: %s", inst, e)

                # Phase 5: P98 Adaptive Live ML Evaluator Inference
                if self.ml_gate_enabled:
                    ml_admit, ml_reason = self.ml_evaluator.evaluate_gate(
                        inst,
                        payload,
                        self.svc,
                        current_st,
                        reps,
                        ml_primary_gate=bool(
                            getattr(profile, "ml_primary_gate", False)
                        ),
                    )
                    if not ml_admit and ml_reason:
                        payload["admit"] = 0
                        reasons = payload.get("reasons", [])
                        if isinstance(reasons, list):
                            if ml_reason not in reasons:
                                reasons.append(ml_reason)
                        elif reasons:
                            payload["reasons"] = [reasons, ml_reason]
                        else:
                            payload["reasons"] = [ml_reason]

            self._last_gate_payloads = gate_payloads

        prices = self.exposure_tracker.fetch_prices(self.enabled_instruments)
        nav_snapshot = self.exposure_tracker.nav_snapshot()
        notional_caps = self.risk_sizer.compute_notional_caps(
            nav_snapshot, exposure_scale=self.exposure_scale
        )
        per_pair_stack_cap = (
            notional_caps.per_position_cap
            * max(1, int(self.risk_manager.limits.max_positions_per_pair))
        )
        self.risk_manager.configure_dynamic_limits(
            max_position_size=per_pair_stack_cap,
            max_total_exposure=notional_caps.portfolio_cap,
            max_total_positions=min(
                max(1, int(self.risk_manager.limits.max_total_positions)),
                self.config.alloc_top_k,
            ),
        )
        self._publish_risk_snapshot()

        if getattr(self.svc, "kill_switch_enabled", False):
            logger.debug("Kill switch engaged; execution loop paused")
            return

        now_ts = time.time()
        self.enforce_time_exits(prices, now_ts)
        self.process_trade_stack(gate_payloads, prices, nav_snapshot, notional_caps)

    def enforce_time_exits(self, prices: Dict[str, Any], now_ts: float) -> None:
        """Phase 9: The Enforcer (Background Time Exits)"""
        for instrument in self.enabled_instruments:
            current_price_data = prices.get(instrument, {})
            current_mid = float(current_price_data.get("mid") or 0.0)
            profile = self.strategy.get(instrument)
            tpsl_config = self._tpsl_config_for(profile)
            latest_candle = self._latest_stream_candle(instrument, "S5")
            if latest_candle and tpsl_config.active:
                candle_close = float(latest_candle.get("close") or 0.0)
                candle_high = float(latest_candle.get("high") or candle_close)
                candle_low = float(latest_candle.get("low") or candle_close)
                current_mid = current_mid or candle_close
                if self.execution_engine.check_tpsl_exit_intra_candle(
                    instrument=instrument,
                    high=candle_high,
                    low=candle_low,
                    timestamp=now_ts,
                    tpsl_config=tpsl_config,
                    tracker=self.exposure_tracker,
                ):
                    logger.info(
                        "Enforcer executed TPSL flush for %s at %.5f/%.5f",
                        instrument,
                        candle_low,
                        candle_high,
                    )

            if self.execution_engine.check_time_expiry(
                instrument=instrument,
                now_ts=now_ts,
                current_price=current_mid,
                timestamp=now_ts,
                tracker=self.exposure_tracker,
                tick_elapsed_secs=int(self.loop_seconds),
            ):
                logger.info(
                    "Enforcer successfully executed time/drawdown flush for %s",
                    instrument,
                )

    def process_trade_stack(
        self,
        gate_payloads: Dict[str, Dict[str, Any]],
        prices: Dict[str, Any],
        nav_snapshot: Any,
        notional_caps: Any,
    ) -> None:
        # `target_units()` expects a scaled exposure budget, not raw gross notional.
        # Convert the broker-aligned per-trade notional cap back into the runtime
        # sizing basis so live `EXPOSURE_SCALE=1.0` yields true gross notional,
        # while any smaller scale still preserves the same resulting unit size.
        per_trade = float(notional_caps.per_position_cap or 0.0) * self.exposure_scale

        for instrument in self.enabled_instruments:
            self.trade_stack.process_instrument(
                instrument,
                gate_payloads.get(instrument, {}),
                prices.get(instrument, {}),
                per_trade,
                nav_snapshot,
                self.exposure_tracker.price_cache,
                self.exposure_tracker,
            )

    def latest_gate_payloads(self) -> Dict[str, Dict[str, Any]]:
        return {key: dict(value) for key, value in self._last_gate_payloads.items()}

    def reconcile_portfolio(self) -> None:
        broker_trades = self.exposure_tracker.reconcile_portfolio(
            self.enabled_instruments
        )
        gate_payloads = self.gate_loader.load(self.enabled_instruments)
        now_ts = time.time()

        for instrument in self.enabled_instruments:
            trades = list(broker_trades.get(instrument, []))
            if not trades:
                self.trade_state.remove_trades(instrument)
                self.trade_stack.clear_entry_cooldown(instrument)
                continue

            profile = self.strategy.get(instrument)
            hold_secs = self.config.hold_seconds
            if profile and profile.hold_minutes is not None:
                hold_secs = max(60, int(profile.hold_minutes) * 60)

            rebuilt: List[ActiveTrade] = []
            latest_entry_ts = 0.0
            for trade in trades:
                entry_ts = float(trade.entry_time.timestamp())
                latest_entry_ts = max(latest_entry_ts, entry_ts)
                rebuilt.append(
                    ActiveTrade(
                        direction=1 if int(trade.units) > 0 else -1,
                        units=abs(int(trade.units)),
                        entry_ts=entry_ts,
                        hold_secs=hold_secs,
                        max_hold_secs=None,
                        elapsed_secs=max(0, int(now_ts - entry_ts)),
                        entry_price=float(trade.entry_price),
                    )
                )

            self.trade_state.replace_trades(instrument, rebuilt)
            self.trade_state.clear_pending_close(instrument)
            self.trade_stack.restore_entry_cooldown(instrument, latest_entry_ts)

            gate_ts = int((gate_payloads.get(instrument, {}) or {}).get("ts_ms") or 0)
            if gate_ts > 0:
                self.trade_state.set_last_signal(instrument, f"gate:{gate_ts}")
            else:
                self.trade_state.clear_last_signal(instrument)


class PortfolioManager(threading.Thread):
    """Threaded execution loop that reconciles gates and broker state."""

    def __init__(self, service: Any) -> None:
        super().__init__(name="PortfolioManager", daemon=True)
        self.config = PortfolioConfig.from_env()
        self._stop_event = threading.Event()
        self.coordinator = PortfolioLoopCoordinator(service, self.config)
        self.strategy = self.coordinator.strategy

        # Delegate backwards compatibility methods
        self.latest_gate_payloads = self.coordinator.latest_gate_payloads
        self.reconcile_portfolio = self.coordinator.reconcile_portfolio

        logger.info(
            "PortfolioManager online with %d instruments",
            len(self.coordinator.enabled_instruments),
        )

    def start(self) -> None:  # type: ignore[override]
        if self.is_alive():
            return
        self._stop_event.clear()
        super().start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        while not self._stop_event.is_set():
            started = time.time()
            try:
                self.coordinator.loop_once()
            except Exception:
                logger.exception("PortfolioManager cycle failed")
            delay = max(0.2, self.coordinator.loop_seconds - (time.time() - started))
            self._stop_event.wait(delay)


__all__ = [
    "PortfolioManager",
    "PortfolioLoopCoordinator",
    "StrategyProfile",
    "StrategyInstrument",
    "BundleDirective",
    "evaluate_gate_and_bundles",
    "structural_metric",
]
