"""V8 Golden Matrix Simulator Logic."""

from collections import deque
from typing import Dict, List, Any

from scripts.research.simulator.signal_matching import dispatch_trigger


def run_v8_simulation_mem(
    candle_list: List[Dict[str, Any]],
    sig_dict: Dict[int, Dict[str, Any]],
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Executes the V8 Golden Matrix logic entirely in-memory using pre-computed structures."""
    results = {}
    pending = []
    history = deque(maxlen=16)
    c_pct_streak = 0
    cooldown = 0

    c_drift_limit = params.get("c_drift_min", 0.65)
    e_pct_limit = params.get("e_pct_max", 0.15)
    v_tick_limit = params.get("v_tick_pct_min", 0.90)
    db_limit = params.get("db_min", 2)

    # We trace using exactly a single horizon (720 rows -> 60m)
    horizons = [720]
    step_ms = 5000
    pip_multi = 10000.0  # Will dynamically override based on current_atr scale, but defaults to non-JPY
    if candle_list and "JPY" in str(candle_list[0].get("instrument", "")):
        pip_multi = 100.0

    current_atr = 0.0
    atr_alpha = 2.0 / (168 + 1)

    export_list = []

    for c_dict in candle_list:
        ts_ms = c_dict["ts_ms"]
        p_c = c_dict["c"]
        p_h = c_dict["h"]
        p_l = c_dict["l"]
        p_o = c_dict.get("o", p_c)
        v = c_dict.get("v", 0)

        tr = max(p_h - p_l, abs(p_h - p_c), abs(p_l - p_c))
        if current_atr == 0:
            current_atr = tr
        else:
            current_atr = current_atr + atr_alpha * (tr - current_atr)

        direction = 1 if p_c >= p_o else 0
        delta = abs(p_c - p_o)
        delta_bucket = min(7, int((delta / tr) * 8)) if tr > 0 else 0
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

        # dynamic multi
        pip_multi = 100.0 if atr_14 < 20.0 and p_c > 50.0 else 10000.0
        current_atr = atr_14 / pip_multi

        if c_pct >= 0.85:
            c_pct_streak += 1
        else:
            c_pct_streak = 0

        # Resolve pending
        active = []
        for p in pending:
            p.evaluate_gate_a_execution(ts_ms, direction, p_c)
            p.update(p_h, p_l, p_c, ts_ms)

            if all(p.completed.values()):
                last_hrz = list(p.horizons.keys())[-1]
                h_last = p.horizons.get(last_hrz)
                if h_last:
                    export_list.append(
                        {
                            "Instrument": "MEM",
                            "Gate": p.name,
                            "Entry_Time": p.ts_ms,
                            "Side": "LONG" if p.is_long else "SHORT",
                            "Entry_Price": p.entry_price,
                            "Exit_Time": h_last.get("exit_time", 0),
                            "Net_Pips": h_last.get("pnl", 0.0),
                            "Gross_Pips": h_last.get("pnl", 0.0) + 0.6,
                            "Exit_Reason": h_last.get("exit_reason", "Unknown"),
                            "R_Multiple": (
                                h_last.get("pnl", 0.0) / (p.risk_distance * pip_multi)
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

        if len(history) < 16 or cooldown > 0:
            continue

        T = list(history)
        T_5, T_4, T_3, T_2, T_1, T_0 = T[10], T[11], T[12], T[13], T[14], T[15]

        if T_0["sess_range"] < 15.0:
            continue

        name = f"V8_Golden_Matrix_c{c_drift_limit}_e{e_pct_limit}_v{v_tick_limit}_db{db_limit}"
        if name not in results:
            results[name] = []

        if T_1["c_drift"] >= c_drift_limit:
            if min(T_3["e_pct"], T_2["e_pct"], T_1["e_pct"]) <= e_pct_limit:
                is_breakout = T_0["db"] >= db_limit
                is_frictionless = T_0["v_tick_pct"] >= v_tick_limit

                if is_breakout and is_frictionless:
                    is_valid_short = (T_0["dir"] == 1) and (T_0["c"] < T_0["ema_240"])
                    is_valid_long = (T_0["dir"] == 0) and (T_0["c"] > T_0["ema_240"])

                    if is_valid_short or is_valid_long:
                        breakout_long = T_0["dir"] == 1
                        trap_high = max(T_3["h"], T_2["h"], T_1["h"], T_0["h"])
                        trap_low = min(T_3["l"], T_2["l"], T_1["l"], T_0["l"])
                        trap_door_price = trap_high if breakout_long else trap_low

                        dispatch_trigger(
                            name,
                            p_c,
                            ts_ms,
                            breakout_long,
                            True,
                            horizons,
                            step_ms,
                            pip_multi,
                            9.0,  # SL
                            6.0,  # TP
                            False,  # Do sweep exits
                            current_atr,
                            results,
                            pending,
                            trap_door_price=trap_door_price,
                        )
                        cooldown = 12
    return export_list
