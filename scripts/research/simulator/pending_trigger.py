"""Module containing the PendingTrigger class for signal validation."""


class PendingTrigger:
    def __init__(
        self,
        name,
        entry_price,
        ts_ms,
        is_long,
        is_gate_a=False,
        horizons=None,
        step_ms=5000,
        pip_multi=10000.0,
        sl_pips=9.0,
        tp_pips=6.0,
        trap_door_price=0.0,
    ):
        self.name = name
        self.entry_price = float(entry_price)
        self.ts_ms = ts_ms
        self.is_long = is_long
        self.is_gate_a = is_gate_a
        self.step_ms = step_ms
        self.pip_multi = pip_multi
        self.sl_pips = sl_pips
        self.tp_pips = tp_pips
        self.trap_door_price = float(trap_door_price)
        self.risk_distance = (
            abs(float(entry_price) - self.trap_door_price)
            if self.trap_door_price > 0
            else (sl_pips / pip_multi)
        )
        self.bars_held = 0

        self.executed = not is_gate_a
        self.executed_price = float(entry_price)

        # MFE/MAE bounded by horizon
        self.horizons = {
            h: {"mfe": 0.0, "mae": 0.0, "pnl": 0.0, "closed": False} for h in horizons
        }
        self.completed = {h: False for h in horizons}

    def evaluate_gate_a_execution(self, ts_ms, direction, current_price):
        if self.executed:
            return

        # 10 second window to fade
        if ts_ms - self.ts_ms > 10000:
            self.executed = True
            return

        # If the breakout direction was long, we wait for a red candle (dir == 0) to execute our short fade.
        if self.is_long and direction == 0:
            self.executed = True
            self.is_long = False
            self.executed_price = current_price

        # If the breakout direction was short, we wait for a green candle (dir == 1) to execute our long fade.
        elif not self.is_long and direction == 1:
            self.executed = True
            self.is_long = True
            self.executed_price = current_price

    def update(self, h, l, c, ts_ms):
        if not self.executed:
            return

        if self.is_long:
            fav = h - self.executed_price
            adv = self.executed_price - l
            close_pnl = c - self.executed_price
        else:
            fav = self.executed_price - l
            adv = h - self.executed_price
            close_pnl = self.executed_price - c

        # Increment bars held
        self.bars_held += 1

        # Calculate 15m and 60m threshold based on step_ms
        bars_15m = 15 * 60000 // self.step_ms
        bars_60m = 60 * 60000 // self.step_ms

        breached_trap = False
        if self.trap_door_price > 0:
            if self.is_long and l <= self.trap_door_price:
                breached_trap = True
            elif not self.is_long and h >= self.trap_door_price:
                breached_trap = True

        tid_exit = False
        if self.bars_held >= bars_15m and close_pnl < 0:
            tid_exit = True

        harvest_exit = False
        if self.bars_held >= bars_60m:
            harvest_exit = True

        force_exit_reason = None
        pnl = close_pnl * self.pip_multi - 0.6
        if breached_trap:
            force_exit_reason = "Trap_Door_SL"
            # Calculate PnL based on exact breach price
            exit_p = self.trap_door_price
            pnl_breach = (
                (exit_p - self.executed_price)
                if self.is_long
                else (self.executed_price - exit_p)
            )
            pnl = pnl_breach * self.pip_multi - 0.6
        elif tid_exit:
            force_exit_reason = "TID_15m_Exit"
        elif harvest_exit:
            force_exit_reason = "Harvest_60m_Exit"

        for hrz_candles, struct in self.horizons.items():
            if not self.completed[hrz_candles]:
                if fav > struct["mfe"]:
                    struct["mfe"] = fav
                if adv > struct["mae"]:
                    struct["mae"] = adv

                if not struct["closed"]:
                    if force_exit_reason:
                        struct["pnl"] = pnl
                        struct["closed"] = True
                        struct["exit_time"] = ts_ms
                        struct["exit_reason"] = force_exit_reason
                    else:
                        # Legacy fallback SL/TP
                        mfe_pips = fav * self.pip_multi - 0.6
                        mae_pips = adv * self.pip_multi + 0.6
                        if mae_pips >= self.sl_pips:
                            struct["pnl"] = -self.sl_pips
                            struct["closed"] = True
                            struct["exit_time"] = ts_ms
                            struct["exit_reason"] = "Stop_Loss_Hit"
                        elif mfe_pips >= self.tp_pips:
                            struct["pnl"] = self.tp_pips
                            struct["closed"] = True
                            struct["exit_time"] = ts_ms
                            struct["exit_reason"] = "Take_Profit_Hit"

                if ts_ms >= self.ts_ms + (hrz_candles * self.step_ms):
                    if not struct["closed"]:
                        struct["pnl"] = close_pnl * self.pip_multi - 0.6
                        struct["closed"] = True
                        struct["exit_time"] = ts_ms
                        struct["exit_reason"] = f"Time_{hrz_candles}M"
                    self.completed[hrz_candles] = True

        if force_exit_reason:
            self.completed = {k: True for k in self.horizons}
