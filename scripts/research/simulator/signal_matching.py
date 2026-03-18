"""Module for signal matching and sweep configurations in validation engine."""

from .pending_trigger import PendingTrigger


def parse_signature(sig_str: str):
    parts = sig_str.split("_")
    c = float(parts[0][1:])
    s = float(parts[1][1:])
    e = float(parts[2][1:])
    return c, s, e


def compute_hazard(c: float, s: float, e: float) -> float:
    return min(1.0, max(0.0, e * (1.0 - c) + (1.0 - s) * 0.5))


def generate_sweep_configs(current_atr_pips):
    configs = []
    # Phase 7: The Unbound Exit Matrix
    configs.append(
        {
            "name": "MACRO_V7",
            "sl_pips": round(2.5 * current_atr_pips, 1),
            "tp_pips": 9999.0,
        }
    )
    return configs
    # 2. Fixed Asymmetric Sweep
    for fixed_sl in [12.0, 15.0, 18.0]:
        for fixed_tp in [10.0, 15.0, 20.0]:
            configs.append(
                {
                    "name": f"FIXED_SL{fixed_sl}_TP{fixed_tp}",
                    "sl_pips": fixed_sl,
                    "tp_pips": fixed_tp,
                }
            )
    # 3. Time-Decay Exit (60-minute rule)
    configs.append({"name": "TIME_DECAY_60M", "sl_pips": 25.0, "tp_pips": 9999.0})
    return configs


def dispatch_trigger(
    name,
    p_c,
    ts_ms,
    is_long,
    is_gate_a,
    horizons,
    step_ms,
    pip_multi,
    sl,
    tp,
    do_sweep_exits,
    current_atr,
    results,
    pending,
    trap_door_price=0.0,
):
    if do_sweep_exits:
        current_atr_pips = current_atr * pip_multi
        configs = generate_sweep_configs(current_atr_pips)
        for cfg in configs:
            sweep_name = f"{name}_{cfg['name']}"
            if sweep_name not in results:
                results[sweep_name] = []
            pending.append(
                PendingTrigger(
                    sweep_name,
                    p_c,
                    ts_ms,
                    is_long,
                    is_gate_a,
                    horizons,
                    step_ms,
                    pip_multi,
                    sl_pips=cfg["sl_pips"],
                    tp_pips=cfg["tp_pips"],
                    trap_door_price=trap_door_price,
                )
            )
    else:
        if name not in results:
            results[name] = []
        pending.append(
            PendingTrigger(
                name,
                p_c,
                ts_ms,
                is_long,
                is_gate_a,
                horizons,
                step_ms,
                pip_multi,
                sl_pips=sl,
                tp_pips=tp,
                trap_door_price=trap_door_price,
            )
        )
