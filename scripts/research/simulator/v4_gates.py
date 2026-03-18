"""V4 Topology Gate Evaluators."""

from typing import Dict, List, Any

from scripts.research.simulator.signal_matching import dispatch_trigger


def evaluate_gate_a_vacuum_fade(
    T: List[Dict[str, Any]],
    p_c: float,
    ts_ms: int,
    current_atr: float,
    pip_multi: float,
    horizons: List[int],
    step_ms: int,
    sl: float,
    tp: float,
    do_sweep_exits: bool,
    results: Dict[str, list],
    pending: list,
) -> bool:
    T_3, T_2, T_1, T_0 = T[12], T[13], T[14], T[15]
    triggered_this_tick = False

    for c_drift_limit in [0.65]:
        for e_pct_limit in [0.15]:
            for v_tick_limit in [0.90]:
                for db_limit in [2]:
                    name = f"A_VacFade_c{c_drift_limit}_e{e_pct_limit}_v{v_tick_limit}_db{db_limit}"
                    if name not in results:
                        results[name] = []

                    if T_1["c_drift"] >= c_drift_limit:
                        if min(T_3["e_pct"], T_2["e_pct"], T_1["e_pct"]) <= e_pct_limit:
                            is_breakout = T_0["db"] >= db_limit
                            is_frictionless = T_0["v_tick_pct"] >= v_tick_limit

                            if is_breakout and is_frictionless:
                                is_valid_short = (T_0["dir"] == 1) and (
                                    T_0["c"] < T_0["ema_240"]
                                )
                                is_valid_long = (T_0["dir"] == 0) and (
                                    T_0["c"] > T_0["ema_240"]
                                )

                                if is_valid_short or is_valid_long:
                                    breakout_long = T_0["dir"] == 1
                                    trap_high = max(
                                        T_3["h"], T_2["h"], T_1["h"], T_0["h"]
                                    )
                                    trap_low = min(
                                        T_3["l"], T_2["l"], T_1["l"], T_0["l"]
                                    )
                                    trap_door_price = (
                                        trap_high if breakout_long else trap_low
                                    )

                                    dispatch_trigger(
                                        name,
                                        p_c,
                                        ts_ms,
                                        breakout_long,
                                        True,
                                        horizons,
                                        step_ms,
                                        pip_multi,
                                        sl,
                                        tp,
                                        do_sweep_exits,
                                        current_atr,
                                        results,
                                        pending,
                                        trap_door_price=trap_door_price,
                                    )
                                    triggered_this_tick = True
    return triggered_this_tick


def evaluate_gate_b_seq_fracture(
    T: List[Dict[str, Any]],
    p_c: float,
    ts_ms: int,
    current_atr: float,
    pip_multi: float,
    horizons: List[int],
    step_ms: int,
    sl: float,
    tp: float,
    do_sweep_exits: bool,
    results: Dict[str, list],
    pending: list,
) -> bool:
    T_3, T_2, T_1, T_0 = T[12], T[13], T[14], T[15]
    macro_window = T[0:12]
    polarity_sum = sum(x["dir"] for x in macro_window)
    triggered_this_tick = False

    for b_c_pct_limit in [0.80]:
        for b_h_pct_limit in [0.90]:
            name_base = f"B_SeqFracture_c{b_c_pct_limit}_h{b_h_pct_limit}"
            short_name = f"{name_base}_Short"
            long_name = f"{name_base}_Long"
            if short_name not in results:
                results[short_name] = []
                results[long_name] = []

            c_pct_max = max(x["c_pct"] for x in macro_window)
            if c_pct_max >= b_c_pct_limit:
                frac_window = T[12:15]
                h_pct_max = max(x["h_pct"] for x in frac_window)
                c_pct_last = frac_window[-1]["c_pct"]

                if h_pct_max >= b_h_pct_limit and c_pct_last < 0.60:
                    if polarity_sum >= 8:
                        if T_0["dir"] == 0 and T_0["ab"] >= 2:
                            if T_0["c"] < T_0["ema_240"]:
                                trap_high = max(T_3["h"], T_2["h"], T_1["h"], T_0["h"])
                                dispatch_trigger(
                                    short_name,
                                    p_c,
                                    ts_ms,
                                    False,
                                    False,
                                    horizons,
                                    step_ms,
                                    pip_multi,
                                    sl,
                                    tp,
                                    do_sweep_exits,
                                    current_atr,
                                    results,
                                    pending,
                                    trap_door_price=trap_high,
                                )
                                triggered_this_tick = True
                    elif polarity_sum <= 4:
                        if T_0["dir"] == 1 and T_0["ab"] >= 2:
                            if T_0["c"] > T_0["ema_240"]:
                                trap_low = min(T_3["l"], T_2["l"], T_1["l"], T_0["l"])
                                dispatch_trigger(
                                    long_name,
                                    p_c,
                                    ts_ms,
                                    True,
                                    False,
                                    horizons,
                                    step_ms,
                                    pip_multi,
                                    sl,
                                    tp,
                                    do_sweep_exits,
                                    current_atr,
                                    results,
                                    pending,
                                    trap_door_price=trap_low,
                                )
                                triggered_this_tick = True
    return triggered_this_tick


def evaluate_gate_c_ghost_dip(
    T: List[Dict[str, Any]],
    p_c: float,
    ts_ms: int,
    current_atr: float,
    pip_multi: float,
    horizons: List[int],
    step_ms: int,
    sl: float,
    tp: float,
    do_sweep_exits: bool,
    results: Dict[str, list],
    pending: list,
) -> bool:
    T_5, T_4, T_3, T_2, T_1, T_0 = T[10], T[11], T[12], T[13], T[14], T[15]
    triggered_this_tick = False

    for c_lim in [0.75]:
        for dip_db in [2]:
            name = f"C_GhostDip_c{c_lim}_db{dip_db}"
            if name not in results:
                results[name] = []

            if (
                T_5["c_pct"] >= c_lim
                and T_4["c_pct"] >= c_lim
                and T_3["c_pct"] >= c_lim
            ):
                if (
                    T_2["dir"] == 0
                    and T_1["dir"] == 0
                    and T_2["db"] <= 1
                    and T_1["db"] <= 1
                ):
                    if T_0["dir"] == 1 and T_0["db"] >= dip_db:
                        if T_0["c"] > T_0["ema_240"]:
                            trap_low = min(T_3["l"], T_2["l"], T_1["l"], T_0["l"])
                            dispatch_trigger(
                                name,
                                p_c,
                                ts_ms,
                                True,
                                False,
                                horizons,
                                step_ms,
                                pip_multi,
                                sl,
                                tp,
                                do_sweep_exits,
                                current_atr,
                                results,
                                pending,
                                trap_door_price=trap_low,
                            )
                            triggered_this_tick = True

                if (
                    T_2["dir"] == 1
                    and T_1["dir"] == 1
                    and T_2["db"] <= 1
                    and T_1["db"] <= 1
                ):
                    if T_0["dir"] == 0 and T_0["db"] >= dip_db:
                        if T_0["c"] < T_0["ema_240"]:
                            trap_high = max(T_3["h"], T_2["h"], T_1["h"], T_0["h"])
                            dispatch_trigger(
                                name,
                                p_c,
                                ts_ms,
                                False,
                                False,
                                horizons,
                                step_ms,
                                pip_multi,
                                sl,
                                tp,
                                do_sweep_exits,
                                current_atr,
                                results,
                                pending,
                                trap_door_price=trap_high,
                            )
                            triggered_this_tick = True
    return triggered_this_tick
