#!/usr/bin/env python3
"""
GPU-Accelerated Vectorized Backtest Optimizer (Ensemble Edition)
Optimizes 'Trend', 'Squeeze', and 'Reversion' strategies independently.
"""
from __future__ import annotations


import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


from scripts.research.optimizer.parameter_grid import ParameterGrid
from scripts.research.optimizer.gpu_runner import GpuBacktestRunner
from scripts.research.optimizer.result_collector import ResultCollector

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s :: %(message)s"
    )
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", nargs="+", default=["EUR_USD"])
    parser.add_argument(
        "--signal-type",
        type=str,
        default="mean_reversion",
        choices=["trend_sniper", "squeeze_breakout", "mean_reversion"],
    )
    parser.add_argument("--output-file", type=str, default="live_params.json")
    parser.add_argument("--max_combinations", type=int, default=5000)
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--use-regime", action="store_true")
    parser.add_argument("--refine", action="store_true")
    parser.add_argument(
        "--min-trades", type=int, default=100, help="Minimum acceptable trade count"
    )
    parser.add_argument(
        "--max-trades", type=int, default=300, help="Maximum acceptable trade count"
    )
    parser.add_argument(
        "--export-trades",
        action="store_true",
        help="Automatically run full simulator export on the best parameters.",
    )
    args = parser.parse_args()

    from datetime import timedelta

    end_dt = datetime.now(timezone.utc).replace(microsecond=0)
    start_dt = end_dt - timedelta(days=args.lookback_days)

    instruments = (
        args.instrument if isinstance(args.instrument, list) else [args.instrument]
    )

    collector = ResultCollector(args.output_file)

    for inst in instruments:
        combos = ParameterGrid.generate_combos(
            args.signal_type, args.max_combinations, instrument=inst
        )
        cache = Path(f"output/export/{inst}.json")
        try:
            res, preloaded = GpuBacktestRunner.execute_gpu_sweep(
                inst,
                start_dt,
                end_dt,
                combos,
                cache,
                use_regime=args.use_regime,
                target_signal_type=args.signal_type,
            )

            active = collector.process_stage1_results(
                inst,
                res,
                args.signal_type,
                args.refine,
                min_trades=args.min_trades,
                max_trades=args.max_trades,
            )

            if active and args.refine:
                anchors = [active[0]["params"]]
                logger.info(
                    f"Running Stage 2 Refinement with {len(anchors)} anchor zones..."
                )
                refined_combos = ParameterGrid.generate_combos(
                    args.signal_type,
                    args.max_combinations,
                    anchors=anchors,
                    instrument=inst,
                )

                res_refine, _ = GpuBacktestRunner.execute_gpu_sweep(
                    inst,
                    start_dt,
                    end_dt,
                    refined_combos,
                    cache,
                    preloaded_data=preloaded,
                    use_regime=args.use_regime,
                    target_signal_type=args.signal_type,
                )
                active_refine = [r for r in res_refine if r["metrics"]["trades"] > 0]

                if (
                    active_refine
                    and active_refine[0]["metrics"]["pnl_bps"]
                    > active[0]["metrics"]["pnl_bps"]
                ):
                    logger.info(
                        f"Refinement improved PnL from {active[0]['metrics']['pnl_bps']:.1f} to {active_refine[0]['metrics']['pnl_bps']:.1f}"
                    )
                    active = active_refine
                else:
                    logger.info(
                        f"Refinement did not find a better peak. Sticking with Stage 1 best."
                    )

            if active:
                collector.save_winner(inst, active, args.signal_type)
                if args.export_trades:
                    collector.export_optimal_trades(
                        inst, start_dt, end_dt, args.signal_type, args.use_regime
                    )
        except Exception as e:
            logger.error(f"Failed {inst}: {e}")
