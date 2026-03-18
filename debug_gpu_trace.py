import torch
import json
from datetime import datetime
from scripts.research.optimizer.tensor_builder import (
    load_data_to_gpu,
    build_regime_filters,
    initialize_tensors,
)
from scripts.research.optimizer.gpu_runner import _process_timeline

device = torch.device("cuda")

start = datetime.fromisoformat("2026-01-31T00:00:00+00:00")
end = datetime.fromisoformat("2026-03-02T11:21:27+00:00")
data = load_data_to_gpu("EUR_USD", start, end, None, device)
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
    times,
) = data
T = closes.shape[0]

with open("output/live_params.json") as f:
    best_p = json.load(f)["EUR_USD"]["mean_reversion"]

combo_dicts = [best_p]
target_signal_type = "mean_reversion"

regime_filters = build_regime_filters(closes, min(8640, max(100, T // 4)))
regime_long_ok, regime_short_ok = regime_filters
if target_signal_type == "mean_reversion":
    regime_long_ok, regime_short_ok = regime_short_ok, regime_long_ok

cfg, state, metrics = initialize_tensors(combo_dicts, device, len(combo_dicts))
idx_grid = torch.arange(5, device=device).unsqueeze(0).expand(len(combo_dicts), -1)

# Modify _process_timeline slightly just to capture traces? No, let's just run it
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
    regime_long_ok,
    regime_short_ok,
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
    0,
    True,
)

print(
    f"PnL: {metrics.cum_pnl_bps.cpu().numpy()[0]:.1f} | Win: {metrics.comp_win.cpu().numpy()[0]} | Loss: {metrics.comp_loss.cpu().numpy()[0]}"
)
