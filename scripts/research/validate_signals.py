#!/usr/bin/env python3
"""
Intra-Instrument Signal Validation Engine: V4 Dynamic Percentile Engine
Evaluates deterministic topological "Arm and Fire" Edge Triggers.
Uses Pandas Rolling Z-scores (Percentile Rank) to auto-normalize metrics across assets/sessions.
"""

import argparse
import logging
from collections import deque
import numpy as np
import pandas as pd

from scripts.research.simulator.dataset_loader import load_dataset_in_memory
from scripts.research.simulator.v4_gates import (
    evaluate_gate_a_vacuum_fade,
    evaluate_gate_b_seq_fracture,
    evaluate_gate_c_ghost_dip,
)

logger = logging.getLogger("signal-validator")


def evaluate_gates_streaming(
    instrument, granularity="S5", sl=9.0, tp=6.0, export_list=None, do_sweep_exits=False
):
    if granularity == "M1":
        horizons = [15, 60, 240]
        step_ms = 60000
        session_min_pips = 15.0
    else:
        horizons = [36, 120, 360]
        step_ms = 5000
        session_min_pips = 2.0

    candle_list, sig_dict = load_dataset_in_memory(instrument, granularity)
    if not candle_list or not sig_dict:
        return

    pip_multi = 100.0 if "JPY" in instrument else 10000.0

    results = {
        "A_Vacuum_Fade": [],
        "B_Snapback": [],
        "C_GhostDip": [],
    }

    pending = []
    history = deque(maxlen=16)
    c_pct_streak = 0
    cooldown = 0
    current_atr = 0.0
    atr_alpha = 2.0 / (14 + 1)

    logger.info(f"Scanning sequentially for V4 State Machine triggers...")

    processed = 0
    for c_dict in candle_list:
        processed += 1
        if processed % 500000 == 0:
            logger.info(f"Processed {processed} candles...")

        ts_ms = c_dict["ts_ms"]
        p_o = c_dict["o"]
        p_h = c_dict["h"]
        p_l = c_dict["l"]
        p_c = c_dict["c"]

        # EMA calculations
        tr = max(p_h - p_l, abs(p_h - p_c), abs(p_l - p_c))
        if current_atr == 0:
            current_atr = tr
        else:
            current_atr = current_atr + atr_alpha * (tr - current_atr)

        # Fire variables
        direction = 1 if p_c >= p_o else 0
        delta = abs(p_c - p_o)

        delta_bucket = 0
        if tr > 0:
            delta_bucket = min(7, int((delta / tr) * 8))

        atr_ratio = tr / current_atr if current_atr > 0 else 0
        atr_bucket = min(3, int(atr_ratio * 2.0))

        if ts_ms not in sig_dict:
            continue

        sig_data = sig_dict[ts_ms]
        c_pct = sig_data["c_pct"]
        e_pct = sig_data["e_pct"]
        h_pct = sig_data["h_pct"]
        c_drift = sig_data.get("c_drift", 0.5)
        sess_range = sig_data.get("session_range_pips", 0.0)
        v_tick_pct = sig_data.get("v_tick_pct", 0.0)
        ema_240 = sig_data.get("ema_240", p_c)
        atr_14 = sig_data.get("atr_14", 9.0)

        # Lock the current_atr to the pre-computed exact atr_14 value
        current_atr = atr_14 / pip_multi

        if c_pct >= 0.85:
            c_pct_streak += 1
        else:
            c_pct_streak = 0

        # Resolve pending triggers
        active = []
        for p in pending:
            p.evaluate_gate_a_execution(ts_ms, direction, p_c)
            p.update(p_h, p_l, p_c, ts_ms)

            if all(p.completed.values()):
                pip_multi_local = 100.0 if "JPY" in instrument else 10000.0

                row = {}
                for hrz in p.horizons.keys():
                    row[f"mfe_{hrz}"] = p.horizons[hrz]["mfe"] * pip_multi_local
                    row[f"mae_{hrz}"] = p.horizons[hrz]["mae"] * pip_multi_local
                    row[f"pnl_{hrz}"] = p.horizons[hrz]["pnl"]

                if p.name in results:
                    results[p.name].append(row)
                else:  # e.g parameters sweep generated new name
                    results[p.name] = [row]

                if export_list is not None:
                    last_hrz = list(p.horizons.keys())[-1]
                    h_last = p.horizons.get(last_hrz)
                    if h_last:
                        export_list.append(
                            {
                                "Instrument": instrument,
                                "Gate": p.name,
                                "Entry_Time": p.ts_ms,
                                "Side": "LONG" if p.is_long else "SHORT",
                                "Entry_Price": p.entry_price,
                                "Exit_Time": h_last.get("exit_time", 0),
                                "Net_Pips": h_last.get("pnl", 0.0),
                                "Gross_Pips": h_last.get("pnl", 0.0) + 0.6,
                                "Exit_Reason": h_last.get("exit_reason", "Unknown"),
                                "R_Multiple": (
                                    h_last.get("pnl", 0.0)
                                    / (p.risk_distance * pip_multi_local)
                                    if getattr(p, "risk_distance", 0.0) > 0
                                    else 0.0
                                ),
                            }
                        )
            else:
                active.append(p)
        pending = active

        history.append(
            {
                "dir": direction,
                "db": delta_bucket,
                "ab": atr_bucket,
                "c_pct": c_pct,
                "e_pct": e_pct,
                "h_pct": h_pct,
                "c_streak": c_pct_streak,
                "c_drift": c_drift,
                "sess_range": sess_range,
                "v_tick_pct": v_tick_pct,
                "ema_240": ema_240,
                "c": p_c,
                "h": p_h,
                "l": p_l,
            }
        )

        if cooldown > 0:
            cooldown -= 1

        if len(history) < 16:
            continue

        if cooldown > 0:
            continue

        # In a list from history (deque), index 0 is the oldest (T-15), index 15 is the newest (T-0)
        T = list(history)
        T_0 = T[15]

        # The Session Authorization: Thermodynamic Engine Limit
        if T_0["sess_range"] < session_min_pips:
            continue

        t_a = evaluate_gate_a_vacuum_fade(
            T,
            p_c,
            ts_ms,
            current_atr,
            pip_multi,
            horizons,
            step_ms,
            sl,
            tp,
            do_sweep_exits,
            results,
            pending,
        )
        t_b = evaluate_gate_b_seq_fracture(
            T,
            p_c,
            ts_ms,
            current_atr,
            pip_multi,
            horizons,
            step_ms,
            sl,
            tp,
            do_sweep_exits,
            results,
            pending,
        )
        t_c = evaluate_gate_c_ghost_dip(
            T,
            p_c,
            ts_ms,
            current_atr,
            pip_multi,
            horizons,
            step_ms,
            sl,
            tp,
            do_sweep_exits,
            results,
            pending,
        )

        triggered_this_tick = t_a or t_b or t_c

        if triggered_this_tick:
            cooldown = 12

    # Print Report
    print(f"\n{'='*70}")
    print(f"[{instrument}] SIGNAL VALIDATION V5: Sequential Fracture & Edge Decay")
    print(f"{'='*70}")

    for gate_name in sorted(results.keys()):
        metrics = results[gate_name]
        count = len(metrics)
        if count == 0:
            continue

        print(f"\n- {gate_name}: {count} Triggers")

        for hrz in horizons:
            pnls = [x[f"pnl_{hrz}"] for x in metrics]
            if not pnls:
                continue
            avg_pnl = np.mean(pnls)
            net_pnl = sum(pnls)
            win_rate = sum(1 for x in pnls if x > 0) / count * 100.0

            hrz_min = hrz if granularity == "M1" else hrz * 5 // 60

            print(
                f"   [Horizon: {hrz} candles ({hrz_min}m)] Net PnL: {net_pnl:+.2f} Pips | Avg PnL: {avg_pnl:+.2f} Pips | Win: {win_rate:5.1f}%"
            )

    return export_list


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s :: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", type=str, default="EUR_USD")
    parser.add_argument("--granularity", type=str, default="S5")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--sl", type=float, default=9.0)
    parser.add_argument("--tp", type=float, default=6.0)
    parser.add_argument("--export-triggers", type=str, default="")
    parser.add_argument(
        "--sweep-exits", action="store_true", help="Run Phase 6.5 dynamic exit sweep"
    )
    args = parser.parse_args()

    instruments = [args.instrument]
    if args.all:
        instruments = [
            "EUR_USD",
            "GBP_JPY",
            "AUD_USD",
            "XAU_USD",
            "USD_JPY",
            "GBP_USD",
            "US30_USD",
        ]

    global_export = []
    for inst in instruments:
        result_export = evaluate_gates_streaming(
            inst, args.granularity, args.sl, args.tp, global_export, args.sweep_exits
        )
        if result_export is not None:
            global_export = result_export

    if args.export_triggers and global_export:
        df_all = pd.DataFrame(global_export)
        if not df_all.empty:
            for inst in df_all["Instrument"].unique():
                df_inst = df_all[df_all["Instrument"] == inst].copy()

                # output trade ledger
                ledger_path = f"output/trade_ledger_{inst}.csv"
                df_inst.to_csv(ledger_path, index=False)

                # Tear Sheet Calculation
                wins = df_inst[df_inst["R_Multiple"] > 0]
                losses = df_inst[df_inst["R_Multiple"] <= 0]

                win_rate = len(wins) / len(df_inst) if len(df_inst) > 0 else 0
                gross_loss_r = abs(losses["R_Multiple"].sum())
                profit_factor_r = (
                    wins["R_Multiple"].sum() / gross_loss_r
                    if gross_loss_r > 0
                    else float("inf")
                )
                expectancy_r = df_inst["R_Multiple"].mean()

                cumulative_r = df_inst["R_Multiple"].cumsum()
                peak_r = cumulative_r.cummax()
                drawdown_r = peak_r - cumulative_r
                max_drawdown_r = drawdown_r.max()

                annualized_N = len(df_inst) * 2
                std_dev_r = df_inst["R_Multiple"].std()
                sharpe_ratio_r = (
                    (expectancy_r / std_dev_r) * np.sqrt(annualized_N)
                    if std_dev_r > 0
                    else 0
                )

                print(f"\n--- {inst} TEAR SHEET (R-Multiples) ---")
                print(f"Total Trades: {len(df_inst)}")
                print(f"Win Rate: {win_rate:.2%}")
                print(f"Profit Factor (R): {profit_factor_r:.2f}")
                print(f"Expectancy (R/Trade): {expectancy_r:.2f}")
                print(f"Max Drawdown (R): {max_drawdown_r:.2f}")
                print(f"Sharpe Ratio (R): {sharpe_ratio_r:.2f}")

        print(f"\nExported {len(global_export)} triggers properly to CSVS.")
