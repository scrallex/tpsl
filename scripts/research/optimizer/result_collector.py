#!/usr/bin/env python3
"""Result collection and reporting for GPU optimizer."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ResultCollector:
    def __init__(self, output_file: str):
        self.output_file = output_file
        self.out_path = Path(f"output/{self.output_file}")
        self.master_config: Dict[str, Any] = {}
        if self.out_path.exists():
            with open(self.out_path, "r") as f:
                self.master_config = json.load(f)

    def process_stage1_results(
        self,
        instrument: str,
        results: List[Dict[str, Any]],
        signal_type: str,
        refine: bool,
        min_trades: int = 1,
        max_trades: int = 10000,
        require_profit: bool = True,
    ) -> List[Dict[str, Any]]:
        active = [
            r for r in results if min_trades <= r["metrics"]["trades"] <= max_trades
        ]
        if require_profit:
            active = [r for r in active if r["metrics"]["pnl_bps"] > 0]

        if active and refine:
            logger.info(f"--- Stage 1 Complete for {instrument} ---")
            logger.info(f"Top base PnL: {active[0]['metrics']['pnl_bps']:.1f} bps")
        return active

    def save_winner(
        self,
        instrument: str,
        results: List[Dict[str, Any]],
        signal_type: str,
    ) -> None:
        if not results:
            logger.warning(f"No trades found for {instrument} using {signal_type}")
            return

        winner = results[0]
        print(f"\n--- WINNER: {instrument} ({signal_type}) ---")
        print(
            f"PnL: {winner['metrics']['pnl_bps']:.1f} bps | Trades: {winner['metrics']['trades']}"
        )

        if instrument not in self.master_config:
            self.master_config[instrument] = {}
        self.master_config[instrument][signal_type] = winner["params"]

        if not self.out_path.parent.exists():
            self.out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.out_path, "w") as f:
            json.dump(self.master_config, f, indent=4)

    def export_optimal_trades(
        self,
        instrument: str,
        start_dt: datetime,
        end_dt: datetime,
        signal_type: str,
        use_regime: bool,
        require_st_peak: bool = False,
    ) -> None:
        logger.info(f"Running full simulator export for {instrument}...")
        from scripts.tools.export_optimal_trades import export_single_trade_history
        from scripts.research.simulator.gate_cache import gate_cache_path_for

        params = self.master_config.get(instrument, {}).get(signal_type)
        if not params:
            logger.error(
                f"Cannot export trades: No saved parameters for {instrument} {signal_type}"
            )
            return

        if use_regime:
            from scripts.research.regime_manifold.regime_math import (
                compute_regime_filtering,
            )

            logger.info(
                f"[{instrument}] Pre-computing native 200-SMA regime matrix to mirror GPU logic..."
            )
            regime_map = compute_regime_filtering(instrument, start_dt, end_dt)
            cache_path = gate_cache_path_for(instrument, signal_type)

            if cache_path.exists() and regime_map:
                logger.info(f"Applying regime filter to {cache_path}...")
                dropped = 0
                kept = 0
                import tempfile
                import shutil

                fd, tmp_path = tempfile.mkstemp()
                with open(cache_path, "r", encoding="utf-8") as f, open(
                    fd, "w", encoding="utf-8"
                ) as out:
                    for line in f:
                        if not line.strip():
                            continue
                        g = json.loads(line)
                        ts_ms = g.get("ts_ms", 0)
                        dir_str = str(g.get("direction", "")).upper()

                        admit = True
                        if ts_ms in regime_map:
                            long_ok, short_ok = regime_map[ts_ms]

                            effective_dir = dir_str
                            is_pacific = any(
                                p in instrument.upper() for p in ["JPY", "AUD", "NZD"]
                            )
                            enforce_regime = True

                            if signal_type == "mean_reversion":
                                if dir_str == "BUY":
                                    effective_dir = "SELL"
                                elif dir_str == "SELL":
                                    effective_dir = "BUY"
                                if is_pacific:
                                    enforce_regime = False

                            if enforce_regime:
                                if effective_dir == "BUY" and not long_ok:
                                    admit = False
                                elif effective_dir == "SELL" and not short_ok:
                                    admit = False

                        if not admit:
                            g["admit"] = 0
                            reasons = g.get("reasons", [])
                            if isinstance(reasons, str):
                                reasons = [reasons]
                            elif not isinstance(reasons, list):
                                reasons = []
                            if "regime_filtered" not in reasons:
                                reasons.append("regime_filtered")
                            g["reasons"] = reasons
                            out.write(json.dumps(g) + "\n")
                            dropped += 1
                        else:
                            out.write(line)
                            kept += 1
                logger.info(
                    f"Regime filter applied -> Kept: {kept}, Dropped: {dropped}"
                )
                backup_path = cache_path.with_suffix(".gates.backup.jsonl")
                shutil.copy2(cache_path, backup_path)
                try:
                    shutil.move(tmp_path, cache_path)
                    export_single_trade_history(
                        instrument,
                        start_dt,
                        end_dt,
                        params,
                        signal_type,
                        use_regime=use_regime,
                        require_st_peak=require_st_peak,
                    )
                finally:
                    shutil.move(backup_path, cache_path)
            else:
                export_single_trade_history(
                    instrument,
                    start_dt,
                    end_dt,
                    params,
                    signal_type,
                    use_regime=use_regime,
                    require_st_peak=require_st_peak,
                )
        else:
            export_single_trade_history(
                instrument,
                start_dt,
                end_dt,
                params,
                signal_type,
                use_regime=use_regime,
                require_st_peak=require_st_peak,
            )
