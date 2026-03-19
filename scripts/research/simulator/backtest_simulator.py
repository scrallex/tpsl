#!/usr/bin/env python3
"""High-fidelity backtest simulator with TP/SL support.

Extends :class:`BacktestSimulator` to:
  1. Load OHLC candle data (not just close/mid).
  2. Check intra-candle TP/SL hits using high/low prices.
  3. Track per-trade TP/SL exit reasons and statistics.
  4. Expose ``TPSLSimulationResult`` with enriched metrics.

The module is designed as a drop-in replacement for ``BacktestSimulator``
when TP/SL analysis is needed, while remaining fully backwards-compatible
(pass ``tpsl_config=None`` to replicate the original behaviour).
"""

from __future__ import annotations


import copy
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from scripts.trading.candle_utils import to_epoch_ms
from scripts.trading.gate_validation import evaluate_gate_and_bundles, structural_metric

logger = logging.getLogger(__name__)
SIM_TRACE_ENABLED = os.getenv("SIM_TRACE", "0") == "1"


from scripts.trading.oanda import OandaConnector
from scripts.trading.portfolio_manager import (
    SessionPolicy,
    StrategyInstrument,
    StrategyProfile,
)
from scripts.trading.risk_calculator import RiskSizer
from scripts.trading.risk_limits import RiskLimits, RiskManager
from scripts.trading.trade_planner import TradePlanner
from scripts.trading.trade_state import TradeStateStore
from scripts.trading.tpsl import (
    TPSLConfig,
    TPSLConfigStore,
)
from scripts.trading.execution_engine import ExecutionEngine
from .models import (
    OHLCCandle,
    TPSLSimulationParams,
    TPSLSimulationResult,
    TPSLTradeRecord,
)
from .tracker import TPSLPositionTracker
from .metrics import TPSLSimulationMetrics
from scripts.research.simulator.data_adapter import BacktestDataAdapter
from scripts.research.simulator.metrics_calculator import compute_tpsl_metrics

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore


try:
    from .pricing_utils import compute_drawdown, compute_sharpe
except ImportError:
    pass

try:
    from .signal_deriver import derive_signals
except ImportError:
    from scripts.research.simulator.signal_deriver import derive_signals

try:
    from .st_filter import STFilterConfig, StructuralTensionFilter
except ImportError:
    from scripts.research.simulator.st_filter import (
        STFilterConfig,
        StructuralTensionFilter,
    )

try:
    from .replay_candle_processor import (
        compute_position_size,
        compute_trade_direction_and_side,
    )
except ImportError:
    from scripts.research.simulator.replay_candle_processor import (
        compute_position_size,
        compute_trade_direction_and_side,
    )


UTC = timezone.utc


# =========================================================================
# High-fidelity simulator
# =========================================================================


@dataclass
class SimulationReplayContext:
    instrument: str
    nav: float
    profile: StrategyInstrument
    params: TPSLSimulationParams
    tpsl_config: TPSLConfig
    tracker: TPSLPositionTracker
    engine: ExecutionEngine
    risk_sizer: RiskSizer
    risk_manager: RiskManager
    session_policy: SessionPolicy
    signatures_sorted: List[Dict[str, Any]]
    gate_sorted: List[Dict[str, Any]]
    tick_secs: int

    gate_idx: int = 0
    sig_idx: int = 0
    current_hazard: float = 999.0
    current_gate: Optional[Dict[str, Any]] = None
    last_mid: Optional[float] = None
    last_entry_time: Optional[datetime] = None
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)

    def align_events(self, candle_ts: int) -> None:
        while (
            self.sig_idx < len(self.signatures_sorted)
            and self.signatures_sorted[self.sig_idx]["ts_ms"] <= candle_ts
        ):
            self.current_hazard = self.signatures_sorted[self.sig_idx]["hazard"]
            self.sig_idx += 1

        self.current_gate = None
        while (
            self.gate_idx < len(self.gate_sorted)
            and self.gate_sorted[self.gate_idx].get("ts_ms", 0) <= candle_ts
        ):
            if candle_ts - self.gate_sorted[self.gate_idx].get("ts_ms", 0) <= 15000:
                self.current_gate = self.gate_sorted[self.gate_idx]
            self.gate_idx += 1

    def process_exits(self, candle: OHLCCandle) -> None:
        if (
            self.params.hazard_exit_threshold is not None
            and self.params.hazard_exit_threshold < 0.999
        ):
            if (
                self.tracker.has_position(self.instrument)
                and self.current_hazard <= self.params.hazard_exit_threshold
            ):
                self.tracker.close_position(
                    self.instrument, candle.close, candle.time, "hazard_exit"
                )

        self.engine.check_tpsl_exit_intra_candle(
            self.instrument,
            candle.high,
            candle.low,
            candle.time,
            self.tpsl_config,
            self.tracker,
        )

    def evaluate_entry(self, candle: OHLCCandle) -> None:
        gate_payload = dict(self.current_gate or {})
        gate_payload.setdefault("instrument", self.profile.symbol)
        gate_payload.setdefault("components", {})

        cooldown_active = False
        if self.last_entry_time is not None:
            if (candle.time - self.last_entry_time).total_seconds() < 60.0:
                cooldown_active = True

        admitted, gate_reasons, is_bundle_entry = evaluate_gate_and_bundles(
            gate_payload, self.profile, self.params, self.current_gate is not None
        )

        if cooldown_active and self.current_gate:
            admitted = False
            if "global_cooldown" not in gate_reasons:
                gate_reasons.append("global_cooldown")

        gate_dir_log = gate_payload.get("direction", "")
        if SIM_TRACE_ENABLED and self.current_gate and gate_dir_log in ("BUY", "SELL"):
            print(
                f"[SIM TRACE] {candle.time} | DIR: {gate_dir_log} | ADMIT: {admitted} | "
                f"Haz={gate_payload.get('hazard', 0):.4f} (Min:{self.profile.hazard_min} Max:{self.profile.hazard_max}) | "
                f"Coh={structural_metric(gate_payload, 'coherence') or 0:.4f} (Min:{self.profile.guards.get('min_coherence')}) | "
                f"Ent={structural_metric(gate_payload, 'entropy') or 0:.4f} (Max:{self.profile.guards.get('max_entropy')}) | "
                f"Reasons: {gate_reasons}"
            )

        session = self.session_policy.evaluate(
            self.profile.symbol, candle.time, self.tracker.has_position(self.instrument)
        )
        hard_blocks: List[str] = []
        if not session.tradable:
            hard_blocks.append(session.reason)

        if cooldown_active:
            admitted = False
            if "cooldown_active" not in gate_reasons:
                gate_reasons.append("cooldown_active")

        direction, requested_side, self.last_mid = compute_trade_direction_and_side(
            gate_payload,
            self.params,
            self.profile,
            self.last_mid,
            candle.close,
            is_bundle_entry,
        )
        gate_entry_ready = admitted and not hard_blocks

        target_units_abs = compute_position_size(
            gate_entry_ready,
            requested_side,
            self.tracker,
            self.instrument,
            self.risk_sizer,
            self.nav,
            self.params,
            candle,
        )

        def execute_cb(inst: str, delta: int, price: float, **_: Any) -> bool:
            if delta != 0 and gate_entry_ready and self.current_gate is not None:
                if (requested_side == 1 and delta > 0) or (
                    requested_side == -1 and delta < 0
                ):
                    self.last_entry_time = candle.time
                    self.last_entry_time = candle.time
            self.risk_manager.record_fill(inst, delta, price)
            return True

        self.engine.execute_allocation(
            instrument=self.instrument,
            now_ts=candle.time.timestamp(),
            gate_entry_ready=gate_entry_ready,
            gate_reasons=list(gate_reasons),
            direction=direction,
            requested_side=requested_side,
            scaled_units_abs=int(target_units_abs),
            hold_secs=max(60, self.params.hold_minutes * 60),
            signal_key=str(gate_payload.get("signal_key") or ""),
            hard_blocks=list(hard_blocks),
            current_price=candle.close,
            timestamp=candle.time,
            is_bundle_entry=is_bundle_entry,
            execute_callback=execute_cb,
            tracker=self.tracker,
            disable_stacking=getattr(self.params, "disable_stacking", False),
            tick_elapsed_secs=self.tick_secs,
        )


class TPSLBacktestSimulator:
    """Replay historical candles with full TP/SL evaluation on each bar."""

    def __init__(
        self,
        *,
        redis_url: Optional[str],
        granularity: str = "S5",
        profile_path: Optional[Path] = None,
        nav: float = 100_000.0,
        nav_risk_pct: float = 0.01,
        per_position_pct_cap: Optional[float] = None,
        cost_bps: float = 1.5,
        tpsl_config_path: Optional[Path] = None,
        oanda: Optional[OandaConnector] = None,
        cache_path: Optional[Path] = None,
    ) -> None:
        self.redis_url = redis_url
        self.granularity = granularity
        self.nav = float(nav)
        self.nav_risk_pct = float(nav_risk_pct)
        self.cache_path = cache_path
        self.per_position_pct_cap = per_position_pct_cap or float(nav_risk_pct)
        self.cost_bps = float(cost_bps)
        self.connector = oanda or OandaConnector(read_only=True)
        self.profile = StrategyProfile.load(
            profile_path or Path("config/mean_reversion_strategy.yaml")
        )
        self.tpsl_store = (
            TPSLConfigStore(tpsl_config_path) if tpsl_config_path else TPSLConfigStore()
        )
        self.data_adapter = BacktestDataAdapter(
            redis_url=redis_url, granularity=granularity
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def simulate(
        self,
        instrument: str,
        *,
        start: datetime,
        end: datetime,
        params: Optional[TPSLSimulationParams] = None,
        tpsl_config: Optional[TPSLConfig] = None,
        instrument_profile: Optional[StrategyInstrument] = None,
    ) -> Optional[TPSLSimulationResult]:
        params = params or TPSLSimulationParams()

        if instrument_profile:
            inst_profile = copy.deepcopy(instrument_profile)
        else:
            profile_obj = copy.deepcopy(self.profile)
            inst_profile = profile_obj.get(instrument)

        # Apply param overrides to profile (always apply, even if provided explicitly, as overrides come from params)
        if params.hazard_override is not None:
            inst_profile.hazard_max = float(params.hazard_override)
        if params.hazard_min is not None:
            inst_profile.hazard_min = float(params.hazard_min)
        elif (
            params.hazard_multiplier is not None and inst_profile.hazard_max is not None
        ):
            inst_profile.hazard_max = (
                inst_profile.hazard_max or 0.0
            ) * params.hazard_multiplier
        inst_profile.min_repetitions = max(1, params.min_repetitions)

        # Resolve TP/SL config: explicit > params > store > empty
        effective_tpsl = tpsl_config or params.to_tpsl_config()
        if not effective_tpsl.active:
            effective_tpsl = self.tpsl_store.get(instrument)

        candles = self.data_adapter.load_ohlc_candles(instrument, start, end)
        if not candles:
            message = (
                f"Failed to load OHLCCandle data for {instrument} from {start} to {end}. "
                "Check cache file existence/date ranges, or OANDA API credentials. "
                "Fetching too many S5 candles at once directly from OANDA results in HTTP 400."
            )
            logger.error(message)
            return None

        gates = self.data_adapter.load_gate_events(
            instrument, start, end, signal_type=params.signal_type
        )
        source = "valkey" if gates else "synthetic"

        if not gates:
            # Build synthetic from close prices
            # Pass raw candles to derive_signals which now handles normalizing
            gates = derive_signals(
                instrument,
                start=start,
                end=end,
                candles=candles,  # pass OHLCCandle list directly
                profile=inst_profile,
                cache_path=self.cache_path,
            )

            # --- START CACHE SAVE ---
            if gates and self.cache_path:
                gate_cache = self.cache_path.with_suffix(".gates.jsonl")
                try:
                    with open(gate_cache, "w") as f:
                        for g in gates:
                            f.write(json.dumps(g) + "\n")
                    logger.info(f"Saved {len(gates)} synthetic gates to {gate_cache}")
                except Exception as e:
                    logger.warning(f"Error writing gate cache: {e}")
            # --- END CACHE SAVE ---

        if not gates:
            return None

        # Apply ST filtering if configured BEFORE filtering sources
        # This is critical because GPU tensor_builder computes ST Peaks identically
        # across ALL gates sequentially, regardless of their source tags.
        st_filter = StructuralTensionFilter(
            STFilterConfig(
                percentile=params.st_percentile, peak_mode=params.st_peak_mode
            )
        )
        st_filter.apply(gates)

        if params.signal_type:
            target_source = params.signal_type.lower()
            
            # To mirror the GPU bypass (target_src_code=0), we allow ANY structured source 
            # if the target_source is one of the core types.
            if target_source in ("trend_sniper", "mean_reversion", "squeeze_breakout"):
                valid_sources = {"structural_extension", "squeeze_breakout", "trend_sniper"}
                gates = [
                    g for g in gates if str(g.get("source", "")).lower() in valid_sources
                ]
            else:
                effective_source = target_source
                gates = [
                    g for g in gates if str(g.get("source", "")).lower() == effective_source
                ]

            if target_source == "mean_reversion":
                for g in gates:
                    current_dir = str(g.get("direction", "")).upper()
                    if current_dir == "BUY":
                        g["direction"] = "SELL"
                    elif current_dir == "SELL":
                        g["direction"] = "BUY"

            if not gates:
                logger.warning(
                    f"No gates remaining after filtering for source='{effective_source}' (mapped from '{target_source}')"
                )
                return None

        tracker, equity_curve = self._replay(
            instrument, candles, gates, inst_profile, params, effective_tpsl
        )

        metrics = compute_tpsl_metrics(equity_curve, tracker.trade_log, self.nav)
        return TPSLSimulationResult(
            instrument=instrument.upper(),
            params=params,
            tpsl_config=effective_tpsl,
            metrics=metrics,
            trades=list(tracker.trade_log),
            equity_curve=equity_curve,
            source=source,
        )

    # ------------------------------------------------------------------
    # Core replay loop
    # ------------------------------------------------------------------
    def _replay(
        self,
        instrument: str,
        candles: Sequence[OHLCCandle],
        gate_events: Sequence[Dict[str, Any]],
        profile: StrategyInstrument,
        params: TPSLSimulationParams,
        tpsl_config: TPSLConfig,
    ) -> Tuple[TPSLPositionTracker, List[Tuple[datetime, float]]]:
        tracker = TPSLPositionTracker(cost_bps=self.cost_bps)
        max_positions_per_pair = max(
            1, int(os.getenv("RISK_MAX_POSITIONS_PER_PAIR", "5") or 5)
        )
        alloc_top_k = max(
            1,
            int(
                os.getenv(
                    "PM_ALLOC_TOP_K",
                    os.getenv("ALLOC_TOP_K", os.getenv("RISK_MAX_TOTAL_POSITIONS", "32")),
                )
                or 32
            ),
        )
        risk_manager = RiskManager(
            RiskLimits(
                max_positions_per_pair=max_positions_per_pair,
                max_total_positions=alloc_top_k,
            )
        )
        risk_manager.set_nav(self.nav)
        risk_sizer = RiskSizer(
            nav_risk_pct=self.nav_risk_pct,
            per_position_pct_cap=self.per_position_pct_cap,
            alloc_top_k=alloc_top_k,
        )
        notional_caps = risk_sizer.compute_notional_caps(
            self.nav,
            exposure_scale=params.exposure_scale,
        )
        risk_manager.configure_dynamic_limits(
            max_position_size=(
                notional_caps.per_position_cap * float(max_positions_per_pair)
            ),
            max_total_exposure=notional_caps.portfolio_cap,
            max_total_positions=alloc_top_k,
        )
        trade_state = TradeStateStore()
        trade_planner = TradePlanner(trade_state)
        engine = ExecutionEngine(
            risk_manager=risk_manager,
            trade_state=trade_state,
            risk_sizer=risk_sizer,
            trade_planner=trade_planner,
            cost_bps=self.cost_bps,
        )
        sessions: Dict[str, Any] = {}
        if profile.session is not None:
            sessions[profile.symbol.upper()] = profile.session
        session_policy = SessionPolicy(sessions, exit_buffer_minutes=5)

        signatures_sorted: List[Dict[str, Any]] = []
        if self.cache_path and params.hazard_exit_threshold is not None:
            sig_cache = self.cache_path.with_suffix(".signatures.jsonl")
            if sig_cache.exists():
                logger.info(
                    "Loading continuous signatures for hazard exit evaluation..."
                )
                with open(sig_cache, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        g = json.loads(line)
                        if "time" in g and "hazard" in g:
                            try:
                                gate_ts = int(
                                    datetime.fromisoformat(
                                        g["time"].replace("Z", "+00:00")
                                    ).timestamp()
                                    * 1000
                                )
                                signatures_sorted.append(
                                    {"ts_ms": gate_ts, "hazard": float(g["hazard"])}
                                )
                            except Exception:
                                pass
                signatures_sorted.sort(key=lambda r: r["ts_ms"])

        gate_sorted = sorted(gate_events, key=lambda r: r.get("ts_ms", 0))

        gran = self.granularity.upper()
        if gran == "S5":
            tick_secs = 5
        elif gran == "M1":
            tick_secs = 60
        elif gran == "M5":
            tick_secs = 300
        elif gran == "M15":
            tick_secs = 900
        elif gran == "H1":
            tick_secs = 3600
        else:
            tick_secs = 5

        ctx = SimulationReplayContext(
            instrument=instrument,
            nav=self.nav,
            profile=profile,
            params=params,
            tpsl_config=tpsl_config,
            tracker=tracker,
            engine=engine,
            risk_sizer=risk_sizer,
            risk_manager=risk_manager,
            session_policy=session_policy,
            signatures_sorted=signatures_sorted,
            gate_sorted=gate_sorted,
            tick_secs=tick_secs,
        )

        for candle in candles:
            candle_ts = to_epoch_ms(candle.time)
            ctx.align_events(candle_ts)
            ctx.process_exits(candle)
            ctx.evaluate_entry(candle)

            # Mark tracking
            ctx.tracker.mark(instrument, candle.close, candle.high, candle.low)

            # Equity snapshot
            unrealized = ctx.tracker.unrealized(instrument, candle.close)
            equity = self.nav + ctx.tracker.realized + unrealized
            ctx.equity_curve.append((candle.time, equity))

        # Force-close any remaining position at end
        if ctx.tracker.has_position(instrument) and candles:
            last = candles[-1]
            ctx.tracker.close_position(
                instrument, last.close, last.time, "simulation_end"
            )

        return ctx.tracker, ctx.equity_curve


__all__ = [
    "OHLCCandle",
    "TPSLBacktestSimulator",
    "TPSLPositionTracker",
    "TPSLSimulationMetrics",
    "TPSLSimulationParams",
    "TPSLSimulationResult",
    "TPSLTradeRecord",
]
