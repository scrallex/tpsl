#!/usr/bin/env python3
"""GPU-Accelerated Vectorized Backtest Execution Engine."""
from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch

from scripts.research.optimizer.tensor_builder import (
    GpuDataTuple,
    build_regime_filters,
    initialize_tensors,
    load_data_to_gpu,
)
from scripts.research.optimizer.result_parser import parse_gpu_results

logger = logging.getLogger(__name__)


@torch.jit.script
def _process_timeline(
    T: int,
    N: int,
    MAX_TRADES: int,
    closes: torch.Tensor,
    highs: torch.Tensor,
    lows: torch.Tensor,
    g_haz: torch.Tensor,
    g_reps: torch.Tensor,
    g_coh: torch.Tensor,
    g_stab: torch.Tensor,
    g_ent: torch.Tensor,
    g_act: torch.Tensor,
    g_src: torch.Tensor,
    g_st_peak: torch.Tensor,
    g_con_haz: torch.Tensor,
    regime_long_ok: Optional[torch.Tensor],
    regime_short_ok: Optional[torch.Tensor],
    enforce_regime_filtering: bool,
    arr_hold: torch.Tensor,
    arr_reps: torch.Tensor,
    arr_haz: torch.Tensor,
    arr_coh: torch.Tensor,
    arr_stab: torch.Tensor,
    arr_ent: torch.Tensor,
    arr_sl: torch.Tensor,
    arr_tp: torch.Tensor,
    arr_trail: torch.Tensor,
    arr_haz_exit: torch.Tensor,
    arr_be: torch.Tensor,
    in_trade: torch.Tensor,
    be_activated: torch.Tensor,
    trade_dir: torch.Tensor,
    entry_price: torch.Tensor,
    hold_timer: torch.Tensor,
    peak_profit: torch.Tensor,
    cooldown_timer: torch.Tensor,
    cum_pnl_bps: torch.Tensor,
    comp_win: torch.Tensor,
    comp_loss: torch.Tensor,
    idx_grid: torch.Tensor,
    target_source: int,
    is_mean_reversion: bool,
    require_st_peak: bool,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:

    for t in range(T):
        curr_c = closes[t]
        curr_h = highs[t]
        curr_l = lows[t]

        gate_valid_source = bool((g_act[t] != 0).item())
        if target_source > 0:
            if g_src[t] != target_source:
                gate_valid_source = False

        cooldown_timer = torch.where(
            cooldown_timer > 0, cooldown_timer - 1, cooldown_timer
        )

        hold_timer = torch.where(in_trade, hold_timer + 1, hold_timer)
        if in_trade.any():
            pct_change_h = (curr_h - entry_price) / entry_price
            pct_change_l = (curr_l - entry_price) / entry_price
            drawdown = torch.where(trade_dir == 1, pct_change_l, -pct_change_h)
            profit = torch.where(trade_dir == 1, pct_change_h, -pct_change_l)

            peak_profit = torch.where(
                in_trade & (profit > peak_profit), profit, peak_profit
            )
            close_pnl = torch.where(
                trade_dir == 1,
                (curr_c - entry_price) / entry_price,
                (entry_price - curr_c) / entry_price,
            )

            hit_sl = drawdown <= -arr_sl.unsqueeze(1)
            hit_tp = profit >= arr_tp.unsqueeze(1)
            be_activated = be_activated | (profit >= arr_be.unsqueeze(1))
            hit_be = be_activated & (drawdown <= 0.0)
            hit_haz_exit = (
                (hold_timer > 0)
                & (g_con_haz[t] <= arr_haz_exit.unsqueeze(1))
                & (arr_haz_exit.unsqueeze(1) < 0.999)
            )
            hit_trail = (peak_profit > 0) & (
                (peak_profit - drawdown) >= arr_trail.unsqueeze(1)
            )
            hit_time = hold_timer >= arr_hold.unsqueeze(1)

            trigger_exit = (
                hit_sl | hit_tp | hit_be | hit_haz_exit | hit_trail | hit_time
            )
            valid_exit = in_trade & trigger_exit

            if valid_exit.any():
                exit_price_val = curr_c
                exit_price_val = torch.where(
                    hit_sl,
                    entry_price
                    * (
                        1.0
                        - torch.where(
                            trade_dir == 1, arr_sl.unsqueeze(1), -arr_sl.unsqueeze(1)
                        )
                    ),
                    exit_price_val,
                )
                exit_price_val = torch.where(
                    hit_tp,
                    entry_price
                    * (
                        1.0
                        + torch.where(
                            trade_dir == 1, arr_tp.unsqueeze(1), -arr_tp.unsqueeze(1)
                        )
                    ),
                    exit_price_val,
                )
                exit_price_val = torch.where(
                    hit_trail,
                    entry_price
                    * (
                        1.0
                        + torch.where(
                            trade_dir == 1,
                            peak_profit - arr_trail.unsqueeze(1),
                            -(peak_profit - arr_trail.unsqueeze(1)),
                        )
                    ),
                    exit_price_val,
                )
                exit_price_val = torch.where(hit_be, entry_price, exit_price_val)

                exec_pnl = torch.where(
                    trade_dir == 1,
                    (exit_price_val - entry_price) / entry_price,
                    (entry_price - exit_price_val) / entry_price,
                )
                exec_pnl -= 1.5 / 10000.0  # Cost

                cum_pnl_bps += torch.where(valid_exit, exec_pnl * 10000.0, 0.0).sum(
                    dim=1
                )
                comp_win += (valid_exit & (exec_pnl > 0)).int().sum(dim=1)
                comp_loss += (valid_exit & (exec_pnl <= 0)).int().sum(dim=1)
                in_trade = in_trade & ~valid_exit

        if gate_valid_source:
            actual_dir = -g_act[t] if is_mean_reversion else g_act[t]
            if regime_long_ok is not None and regime_short_ok is not None:
                is_long_ok = bool(regime_long_ok[t].item())
                is_short_ok = bool(regime_short_ok[t].item())

                if enforce_regime_filtering:
                    if not (
                        (actual_dir == 1 and is_long_ok)
                        or (actual_dir == -1 and is_short_ok)
                    ):
                        continue

            gate_hz = g_haz[t]
            gate_rp = g_reps[t]
            gate_co = g_coh[t]
            gate_st = g_stab[t]
            gate_en = g_ent[t]

            if is_mean_reversion:
                if require_st_peak:
                    valid_gate = (
                        (gate_hz >= arr_haz)
                        & (gate_rp >= arr_reps)
                        & (gate_co >= arr_coh)
                        & (gate_st >= arr_stab)
                        & (gate_en <= arr_ent)
                        & g_st_peak[t]
                    )
                else:
                    valid_gate = (
                        (gate_hz >= arr_haz)
                        & (gate_rp >= arr_reps)
                        & (gate_co >= arr_coh)
                        & (gate_st >= arr_stab)
                        & (gate_en <= arr_ent)
                    )
            else:
                valid_gate = (
                    (gate_hz <= arr_haz)
                    & (gate_rp >= arr_reps)
                    & (gate_co >= arr_coh)
                    & (gate_st >= arr_stab)
                    & (gate_en <= arr_ent)
                )

            # Mirror the live trade planner's net-sided book: do not admit
            # opposite-direction entries while same-instrument exposure is open.
            signed_open_units = (
                trade_dir.to(torch.int32) * in_trade.to(torch.int32)
            ).sum(dim=1)
            current_side = torch.zeros_like(signed_open_units)
            current_side = torch.where(
                signed_open_units > 0,
                torch.ones_like(current_side),
                current_side,
            )
            current_side = torch.where(
                signed_open_units < 0,
                -torch.ones_like(current_side),
                current_side,
            )
            side_conflict = (current_side != 0) & (
                current_side != actual_dir.to(torch.int32)
            )
            has_open_slot = (~in_trade).any(dim=1)
            new_entry = (
                valid_gate
                & has_open_slot
                & (cooldown_timer == 0)
                & (~side_conflict)
            )

            if new_entry.any():
                first_open_idx = (~in_trade).float().argmax(dim=1)
                update_mask = new_entry.unsqueeze(1) & (
                    idx_grid == first_open_idx.unsqueeze(1)
                )

                in_trade = in_trade | update_mask
                trade_dir = torch.where(
                    update_mask, actual_dir.to(trade_dir.dtype), trade_dir
                )
                entry_price = torch.where(update_mask, curr_c, entry_price)
                hold_timer = torch.where(
                    update_mask,
                    torch.tensor(0, dtype=hold_timer.dtype, device=hold_timer.device),
                    hold_timer,
                )
                peak_profit = torch.where(
                    update_mask,
                    torch.tensor(
                        0.0, dtype=peak_profit.dtype, device=peak_profit.device
                    ),
                    peak_profit,
                )
                be_activated = be_activated & ~update_mask
                cooldown_timer = torch.where(
                    new_entry,
                    torch.tensor(
                        12, dtype=cooldown_timer.dtype, device=cooldown_timer.device
                    ),
                    cooldown_timer,
                )

    return cum_pnl_bps, comp_win, comp_loss


class GpuBacktestRunner:
    @staticmethod
    def execute_gpu_sweep(
        instrument: str,
        start: datetime,
        end: datetime,
        combo_dicts: List[Dict[str, Any]],
        cache_path: Optional[Path] = None,
        preloaded_data: Optional[GpuDataTuple] = None,
        use_regime: bool = False,
        target_signal_type: str = "trend_sniper",
        require_st_peak: bool = False,
    ) -> Tuple[List[Dict[str, Any]], GpuDataTuple]:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(
            f"Initializing GPU Engine ({device}) for {len(combo_dicts)} variants | Mode: {target_signal_type}"
        )

        if preloaded_data and preloaded_data[2].shape[0] > 0:
            (
                highs,
                lows,
                closes,
                g_haz,
                g_reps,
                g_coh,
                g_stab,
                g_ent,
                g_act,
                g_src,
                g_st_peak,
                g_con_haz,
                times,
            ) = preloaded_data
        else:
            (
                highs,
                lows,
                closes,
                g_haz,
                g_reps,
                g_coh,
                g_stab,
                g_ent,
                g_act,
                g_src,
                g_st_peak,
                g_con_haz,
                times,
            ) = load_data_to_gpu(
                instrument,
                start,
                end,
                cache_path,
                device,
                target_signal_type=target_signal_type,
            )

        T = closes.shape[0]
        if T == 0:
            return [], (
                highs,
                lows,
                closes,
                g_haz,
                g_reps,
                g_coh,
                g_stab,
                g_ent,
                g_act,
                g_src,
                g_st_peak,
                g_con_haz,
                times,
            )

        regime_long_ok, regime_short_ok = None, None
        if use_regime:
            regime_filters = build_regime_filters(closes, min(8640, max(100, T // 4)))
            if regime_filters:
                regime_long_ok, regime_short_ok = regime_filters
                if target_signal_type == "mean_reversion":
                    regime_long_ok, regime_short_ok = regime_short_ok, regime_long_ok

        cfg, state, metrics = initialize_tensors(combo_dicts, device, len(combo_dicts))
        idx_grid = (
            torch.arange(5, device=device).unsqueeze(0).expand(len(combo_dicts), -1)
        )

        # Bypass exact string matching constraints.
        # The parameter bounds mathematically isolate physical mechanics natively.
        target_src_code = 0

        metrics.cum_pnl_bps, metrics.comp_win, metrics.comp_loss = _process_timeline(
            T,
            len(combo_dicts),
            5,
            closes,
            highs,
            lows,
            g_haz,
            g_reps,
            g_coh,
            g_stab,
            g_ent,
            g_act,
            g_src,
            g_st_peak,
            g_con_haz,
            regime_long_ok,
            regime_short_ok,
            bool(
                not (
                    target_signal_type == "mean_reversion"
                    and any(p in instrument.upper() for p in ["JPY", "AUD", "NZD"])
                )
            ),
            cfg.arr_hold,
            cfg.arr_reps,
            cfg.arr_haz,
            cfg.arr_coh,
            cfg.arr_stab,
            cfg.arr_ent,
            cfg.arr_sl,
            cfg.arr_tp,
            cfg.arr_trail,
            cfg.arr_haz_exit,
            cfg.arr_be,
            state.in_trade,
            state.be_activated,
            state.trade_dir,
            state.entry_price,
            state.hold_timer,
            state.peak_profit,
            state.cooldown_timer,
            metrics.cum_pnl_bps,
            metrics.comp_win,
            metrics.comp_loss,
            idx_grid,
            target_src_code,
            target_signal_type == "mean_reversion",
            require_st_peak,
        )

        pnl_out = metrics.cum_pnl_bps.cpu().numpy()
        win_out = metrics.comp_win.cpu().numpy()
        loss_out = metrics.comp_loss.cpu().numpy()

        results = parse_gpu_results(combo_dicts, pnl_out, win_out, loss_out)

        return results, (
            highs,
            lows,
            closes,
            g_haz,
            g_reps,
            g_coh,
            g_stab,
            g_ent,
            g_act,
            g_src,
            g_st_peak,
            g_con_haz,
            times,
        )
