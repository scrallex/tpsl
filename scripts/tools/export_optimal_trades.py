#!/usr/bin/env python3
"""
Batch export the exact chronological simulated trade logs and detailed
metrics (Sharpe, Profit Factor, Drawdown, etc.) for all 7 optimal configurations
found during the recent global sweep.

Outputs are written to: output/market_data/<instrument>.trades.json
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import sys
from copy import deepcopy


from scripts.research.simulator.gate_cache import (
    ensure_historical_gate_cache,
    gate_cache_path_for,
)
from scripts.research.simulator.gpu_parity_replay import run_gpu_parity_replay
from scripts.research.simulator.models import (
    TPSLSimulationParams,
    TPSLSimulationResult,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("export_optimal_trades")


def _gpu_parity_pnl_bps(trades: list[object]) -> float:
    total_bps = 0.0
    for trade in trades:
        direction = getattr(trade, "direction", None)
        entry_price = getattr(trade, "entry_price", None)
        exit_price = getattr(trade, "exit_price", None)
        if isinstance(trade, dict):
            direction = trade.get("direction")
            entry_price = trade.get("entry_price")
            exit_price = trade.get("exit_price")

        if not direction or not entry_price or not exit_price:
            continue

        entry = float(entry_price)
        exit_ = float(exit_price)
        gross_return = (
            (exit_ - entry) / entry
            if str(direction).upper() == "LONG"
            else (entry - exit_) / entry
        )
        total_bps += gross_return * 10000.0
        total_bps -= 1.5

    return total_bps


def _build_simulation_params(
    p: dict,
    signal_type: str,
    *,
    ml_primary_gate: bool,
    exposure_scale: float,
    require_st_peak: bool,
) -> TPSLSimulationParams:
    hazard = p.get("hazard_max") if "hazard_max" in p else p.get("Haz")
    reps = p.get("min_reps") if "min_reps" in p else p.get("Reps")
    hold = p.get("hold_minutes") if "hold_minutes" in p else p.get("Hold")
    sl = p.get("sl_margin") if "sl_margin" in p else p.get("SL")
    tp = p.get("tp_margin") if "tp_margin" in p else p.get("TP")
    trail = p.get("trailing_stop_pct") if "trailing_stop_pct" in p else p.get("Trail")
    haz_ex = p.get("hazard_exit") if "hazard_exit" in p else p.get("HazEx")
    be = p.get("breakeven") if "breakeven" in p else p.get("BE")
    coh = p.get("Coh")
    ent = p.get("Ent")
    stab = p.get("Stab")

    return TPSLSimulationParams(
        ml_primary_gate=ml_primary_gate,
        disable_bundle_overrides=True,
        allow_fallback=False,
        exposure_scale=exposure_scale,
        hazard_override=float(hazard) if hazard and signal_type != "mean_reversion" else None,
        hazard_min=float(hazard) if hazard and signal_type == "mean_reversion" else None,
        min_repetitions=int(reps) if reps else 1,
        hold_minutes=int(float(hold)) if hold else 24 * 60,
        stop_loss_pct=float(sl) if sl else None,
        take_profit_pct=float(tp) if tp else None,
        trailing_stop_pct=float(trail) if trail else None,
        hazard_exit_threshold=float(haz_ex) if haz_ex else None,
        breakeven_trigger_pct=float(be) if be else None,
        coherence_threshold=float(coh) if coh else None,
        entropy_threshold=float(ent) if ent else None,
        stability_threshold=float(stab) if stab else None,
        signal_type=signal_type,
        st_peak_mode=bool(signal_type == "mean_reversion" and require_st_peak),
        disable_stacking=p.get("disable_stacking", False),
    )


def export_single_trade_history(
    instrument: str,
    start_dt: datetime,
    end_dt: datetime,
    p: dict,
    signal_type: str = "mean_reversion",
    use_regime: bool = False,
    ml_primary_gate: bool = False,
    profile_path: Path | None = None,
    exposure_scale: float | None = None,
    per_position_pct_cap: float | None = None,
    require_st_peak: bool = False,
) -> bool:
    logger.info(f"Initializing simulation export for {instrument}...")

    resolved_profile = profile_path or Path(
        os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml")
    )
    resolved_nav_risk_pct = float(os.getenv("PORTFOLIO_NAV_RISK_PCT", "0.01") or 0.01)
    resolved_per_pos_pct = (
        float(per_position_pct_cap)
        if per_position_pct_cap is not None
        else float(
            os.getenv("PM_MAX_PER_POS_PCT", str(resolved_nav_risk_pct))
            or resolved_nav_risk_pct
        )
    )
    resolved_exposure_scale = (
        float(exposure_scale)
        if exposure_scale is not None
        else float(os.getenv("EXPOSURE_SCALE", "0.02") or 0.02)
    )
    ensure_historical_gate_cache(
        instrument,
        start_dt,
        end_dt,
        signal_type=signal_type,
    )
    sim_params = _build_simulation_params(
        p,
        signal_type,
        ml_primary_gate=ml_primary_gate,
        exposure_scale=resolved_exposure_scale,
        require_st_peak=require_st_peak,
    )

    res = run_gpu_parity_replay(
        instrument=instrument,
        start=start_dt,
        end=end_dt,
        params=sim_params,
        nav=100_000.0,
        nav_risk_pct=resolved_nav_risk_pct,
        per_position_pct_cap=resolved_per_pos_pct,
        cost_bps=1.5,
        granularity="S5",
    )

    if not res:
        logger.error(f"Failed to generate backtest simulation for {instrument}.")
        return False

    metrics = res.metrics.to_dict()
    trades = [t.to_dict() for t in res.trades]

    live_sized_bps = metrics.get("return_pct", 0) * 10000
    gpu_parity_bps = _gpu_parity_pnl_bps(res.trades)
    metrics["gpu_parity_pnl_bps"] = gpu_parity_bps

    # Calculate R-Multiples
    sl_val = p.get("sl_margin") if "sl_margin" in p else p.get("SL")
    sl_pct = float(sl_val) if sl_val else 0.001

    from scripts.research.simulator.metrics import compute_r_multiples

    pf_r, expected_r = compute_r_multiples(trades, sl_pct)
    metrics["profit_factor_r"] = pf_r
    metrics["expected_r"] = expected_r

    logger.info(
        f"[{instrument}] Completed. Trades: {metrics['trades']}, GPU Parity PnL (bps): {gpu_parity_bps:.1f}, "
        f"Live-Sized Return (bps): {live_sized_bps:.1f}, "
        f"Win Rate: {metrics['win_rate']:.1%}, Sharpe: {metrics['sharpe']:.2f}, "
        f"Max DD ($): {metrics['max_drawdown']:.2f}, PF: {metrics['profit_factor']:.2f}, "
        f"PF_R: {pf_r:.2f}"
    )

    output_payload = {
        "instrument": instrument,
        "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "parameters": p,
        "metrics": metrics,
        "trades": trades,
    }

    out_path = Path(f"output/market_data/{instrument}.trades.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as f:
        json.dump(output_payload, f, indent=2)

    logger.info(f"Exported detailed trace data -> {out_path}\n")
    return True


from typing import Dict, Tuple

from scripts.research.regime_manifold.regime_math import compute_regime_filtering


def main():
    import argparse

    def parse_iso8601(value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    parser = argparse.ArgumentParser()
    parser.add_argument("--use-regime", action="store_true", help="Filter by 200 SMA")
    parser.add_argument(
        "--instrument",
        nargs="+",
        default=[],
        help="Run for specific instrument(s) only",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=100, help="Days to look back"
    )
    parser.add_argument(
        "--signal-type",
        type=str,
        default="mean_reversion",
        help="Target signal to export",
    )
    parser.add_argument(
        "--params-file",
        type=str,
        default="output/live_params.json",
        help="Path to the JSON params file produced by gpu_optimizer.py",
    )
    parser.add_argument(
        "--use-ml",
        action="store_true",
        help="Filter gates using trained HistGBM model (>0.80 target)",
    )
    parser.add_argument(
        "--ml-primary-gate",
        action="store_true",
        help="Relax structural gates and let ML act as the primary admission layer.",
    )
    parser.add_argument(
        "--profile-path",
        default=os.getenv("STRATEGY_PROFILE", "config/mean_reversion_strategy.yaml"),
        help="Strategy profile used for the high-fidelity simulator.",
    )
    parser.add_argument(
        "--require-st-peak",
        action="store_true",
        help="Require structural-tension peak reversals during mean-reversion replay/export.",
    )
    parser.add_argument(
        "--exposure-scale",
        type=float,
        default=float(os.getenv("EXPOSURE_SCALE", "0.02") or 0.02),
        help="Scalar exposure multiplier used by the live-aligned sizing path.",
    )
    parser.add_argument(
        "--per-position-pct-cap",
        type=float,
        default=float(
            os.getenv(
                "PM_MAX_PER_POS_PCT",
                os.getenv("PORTFOLIO_NAV_RISK_PCT", "0.01"),
            )
            or os.getenv("PORTFOLIO_NAV_RISK_PCT", "0.01")
        ),
        help="Per-position NAV cap passed into the simulator risk sizer.",
    )
    parser.add_argument(
        "--end-time",
        type=parse_iso8601,
        help="Optional UTC end time for a reproducible export window (ISO-8601).",
    )
    args = parser.parse_args()

    params_file = Path(args.params_file)
    if not params_file.exists():
        logger.error(f"Cannot find {params_file}")
        return 1

    with open(params_file, "r") as f:
        live_params = json.load(f)

    # The 7 instruments optimized in the GPU sweep
    instruments = (
        args.instrument if args.instrument else [
            "EUR_USD",
            "GBP_USD",
            "USD_JPY",
            "USD_CAD",
            "USD_CHF",
            "NZD_USD",
            "AUD_USD",
        ]
    )

    end_dt = args.end_time or datetime.now(timezone.utc).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=args.lookback_days)

    for inst in instruments:
        base_p = live_params.get(inst)
        if not base_p:
            logger.warning(f"No optimal parameters found for {inst}. Skipping.")
            continue

        p = (
            base_p.get(args.signal_type)
            if isinstance(base_p, dict) and args.signal_type in base_p
            else base_p
        )

        if not p:
            logger.warning(
                f"No {args.signal_type} parameters found for {inst}. Skipping."
            )
            continue

        if not isinstance(p, dict):
            logger.warning(f"Unexpected parameter payload for {inst}. Skipping.")
            continue

        p = deepcopy(p)

        gate_cache = gate_cache_path_for(inst, args.signal_type)
        ensure_historical_gate_cache(
            inst,
            start_dt,
            end_dt,
            signal_type=args.signal_type,
            gate_cache_path=gate_cache,
        )

        effective_use_regime = args.use_regime and not args.ml_primary_gate
        ml_map = {}
        ml_threshold = 0.50
        if args.use_ml:
            model_path = Path(f"output/models/{inst}_histgbm.pkl")
            features_path = Path(f"output/ml_data/{inst}_features.parquet")
            
            if model_path.exists() and features_path.exists():
                logger.info(f"[{inst}] Loading ML Model {model_path.name}...")
                import pandas as pd
                import pickle
                
                with open(model_path, "rb") as f:
                    model = pickle.load(f)

                df = pd.read_parquet(features_path)
                features = [
                    "lambda_hazard",
                    "coherence",
                    "entropy",
                    "stability",
                    "reps",
                    "st",
                    "st_peak",
                    "rsi",
                    "volatility",
                    "dist_ema60",
                ]
                
                probs = model.predict_proba(df[features])[:, 1]
                
                # Adaptive Threshold: Execute strictly the Top 2% of probability setups natively
                # This accounts for the fact that USD_JPY peaks max at 0.31 while EUR_USD hits 0.66
                p98_threshold = pd.Series(probs).quantile(0.98)
                logger.info(f"[{inst}] Adaptive Execution Threshold (P98): {p98_threshold:.4f}")
                ml_threshold = p98_threshold
                
                for ts, prob in zip(df["ts_ms"], probs):
                    ml_map[ts] = prob
            else:
                logger.warning(f"[{inst}] Missing model or Parquet features. ML filtering skipped.")

        # Optionally pre-filter gates based on SMA regime or ML Model
        if effective_use_regime or args.use_ml:
            regime_map = {}
            if effective_use_regime:
                is_pacific = any(px in inst.upper() for px in ["JPY", "AUD", "NZD"])
                if args.signal_type == "mean_reversion" and is_pacific:
                    logger.info(f"[{inst}] Skipping regime filter for Pacific Mean Reversion pair...")
                else:
                    logger.info(
                        f"[{inst}] Pre-computing native 200-SMA regime matrix to mirror GPU logic..."
                    )
                    regime_map = compute_regime_filtering(inst, start_dt, end_dt)
            elif args.use_regime and args.ml_primary_gate:
                logger.info(
                    f"[{inst}] ML-primary gate active; skipping regime filter to mirror archived strategy."
                )

            cache_path = gate_cache
            if cache_path.exists() and (regime_map or ml_map):
                import tempfile
                import shutil

                logger.info(f"Applying filters (Regime: {bool(regime_map)}, ML: {bool(ml_map)}) to {cache_path}...")
                dropped = 0
                kept = 0
                fd, tmp_path = tempfile.mkstemp()
                with open(cache_path, "r") as f, open(fd, "w") as out:
                    for line in f:

                        if not line.strip():
                            continue
                        g = json.loads(line)
                        ts_ms = g.get("ts_ms", 0)
                        dir_str = str(g.get("direction", "")).upper()

                        admit = True
                        reasons = g.get("reasons", [])
                        if isinstance(reasons, str):
                            reasons = [reasons]
                        elif not isinstance(reasons, list):
                            reasons = []
                            
                        # Strip previous script modifications to ensure idempotency
                        reasons = [r for r in reasons if not str(r).startswith("ml_") and not str(r).startswith("regime_")]

                        if regime_map and ts_ms in regime_map:
                            long_ok, short_ok = regime_map[ts_ms]
                            effective_dir = dir_str
                            if args.signal_type == "mean_reversion":
                                if dir_str == "BUY":
                                    effective_dir = "SELL"
                                elif dir_str == "SELL":
                                    effective_dir = "BUY"

                            if effective_dir == "BUY" and not long_ok:
                                admit = False
                                reasons.append("regime_filtered")
                            elif effective_dir == "SELL" and not short_ok:
                                admit = False
                                reasons.append("regime_filtered")
                                
                        if admit and ml_map and dir_str in ("BUY", "SELL"):
                            if ts_ms in ml_map:
                                prob = ml_map[ts_ms]
                                g["ml_prob"] = prob
                                if prob < ml_threshold:
                                    admit = False
                                    reasons.append(f"ml_confidence_low:{prob:.2f}")
                            else:
                                admit = False
                                reasons.append("ml_features_missing")

                        if not admit:
                            g["admit"] = 0
                            g["reasons"] = reasons
                            dropped += 1
                        else:
                            kept += 1

                        out.write(json.dumps(g) + "\n")
                logger.info(
                    f"Filters applied -> Kept: {kept}, Dropped: {dropped}"
                )
                backup_path = cache_path.with_suffix(".gates.backup.jsonl")
                shutil.copy2(cache_path, backup_path)
                try:
                    shutil.move(tmp_path, cache_path)
                    export_single_trade_history(
                        inst,
                        start_dt,
                        end_dt,
                        p,
                        args.signal_type,
                        ml_primary_gate=args.ml_primary_gate,
                        profile_path=Path(args.profile_path),
                        exposure_scale=args.exposure_scale,
                        per_position_pct_cap=args.per_position_pct_cap,
                        require_st_peak=args.require_st_peak,
                    )
                finally:
                    shutil.move(backup_path, cache_path)
            else:
                export_single_trade_history(
                    inst,
                    start_dt,
                    end_dt,
                    p,
                    args.signal_type,
                    ml_primary_gate=args.ml_primary_gate,
                    profile_path=Path(args.profile_path),
                    exposure_scale=args.exposure_scale,
                    per_position_pct_cap=args.per_position_pct_cap,
                    require_st_peak=args.require_st_peak,
                )
        else:
            export_single_trade_history(
                inst,
                start_dt,
                end_dt,
                p,
                args.signal_type,
                ml_primary_gate=args.ml_primary_gate,
                profile_path=Path(args.profile_path),
                exposure_scale=args.exposure_scale,
                per_position_pct_cap=args.per_position_pct_cap,
                require_st_peak=args.require_st_peak,
            )

    logger.info("All 7 assets successfully mapped and exported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
