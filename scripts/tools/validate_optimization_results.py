#!/usr/bin/env python3
"""Audit optimization results for unit mismatches and live system compatibility.

This script validates that:
1. Drawdown percentages are calculated correctly
2. Results would qualify under live system constraints
3. NAV assumptions match deployment environment
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any, List


def load_results(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Load optimization results from JSON checkpoint."""
    with open(path) as f:
        data = json.load(f)
    return data.get("results", {})


def audit_result(
    instrument: str,
    result: Dict[str, Any],
    live_nav: float,
    max_dd_threshold: float,
    min_trades: int,
) -> Dict[str, Any]:
    """Audit a single optimization result for correctness and qualification."""
    metrics = result.get("metrics", {})
    
    # Extract raw values
    raw_dd = metrics.get("max_dd", 0)
    raw_dd_pct = metrics.get("max_dd_pct", 0)
    trades = metrics.get("trades", 0)
    pnl = metrics.get("pnl", 0)
    sharpe = metrics.get("sharpe", 0)
    win_rate = metrics.get("win_rate", 0)
    
    # Recalculate DD percentage with specified NAV
    # Bug scenario: raw_dd is absolute dollars, raw_dd_pct may be incorrectly calculated
    recalc_dd_pct = raw_dd / live_nav if live_nav > 0 else 0
    
    # Check if stored dd_pct matches expected calculation
    dd_pct_mismatch = abs(raw_dd_pct - recalc_dd_pct) > 0.0001
    
    # Determine qualification under correct calculation
    qualified_correct = (
        trades >= min_trades and
        recalc_dd_pct <= max_dd_threshold
    )
    
    # Determine qualification under buggy calculation (if raw_dd was compared directly)
    qualified_buggy = (
        trades >= min_trades and
        raw_dd <= max_dd_threshold  # Bug: comparing dollars to percentage threshold
    )
    
    return {
        "instrument": instrument,
        "trades": trades,
        "pnl": pnl,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "raw_dd_dollars": raw_dd,
        "stored_dd_pct": raw_dd_pct,
        "recalculated_dd_pct": recalc_dd_pct,
        "dd_pct_mismatch": dd_pct_mismatch,
        "qualified_correct": qualified_correct,
        "qualified_buggy": qualified_buggy,
        "score": result.get("score", 0),
    }


def format_audit_report(
    audits: List[Dict[str, Any]],
    live_nav: float,
    max_dd_threshold: float,
    min_trades: int,
) -> str:
    """Generate human-readable audit report."""
    lines = []
    lines.append("=" * 80)
    lines.append("OPTIMIZATION RESULTS AUDIT REPORT")
    lines.append("=" * 80)
    lines.append(f"NAV Assumption: ${live_nav:,.0f}")
    lines.append(f"Max DD Threshold: {max_dd_threshold:.2%}")
    lines.append(f"Min Trades: {min_trades}")
    lines.append("=" * 80)
    lines.append("")
    
    # Count by qualification status
    qualified_correct = [a for a in audits if a["qualified_correct"]]
    qualified_buggy = [a for a in audits if a["qualified_buggy"]]
    mismatches = [a for a in audits if a["dd_pct_mismatch"]]
    
    lines.append(f"Total Results Audited: {len(audits)}")
    lines.append(f"Qualified (Correct Calculation): {len(qualified_correct)}")
    lines.append(f"Qualified (Buggy Calculation): {len(qualified_buggy)}")
    lines.append(f"DD% Calculation Mismatches: {len(mismatches)}")
    lines.append("")
    
    if mismatches:
        lines.append("⚠️  WARNING: DD% MISMATCHES DETECTED")
        lines.append("The stored 'max_dd_pct' values don't match expected calculations.")
        lines.append("This suggests the optimization was run with a different NAV or has a bug.")
        lines.append("")
    
    # Top qualified results (correct calculation)
    if qualified_correct:
        lines.append("TOP QUALIFIED RESULTS (Correct Calculation):")
        lines.append("-" * 80)
        qualified_sorted = sorted(qualified_correct, key=lambda x: x["score"], reverse=True)
        for a in qualified_sorted[:10]:  # Top 10
            lines.append(
                f"{a['instrument']:10s} | Score: {a['score']:6.4f} | "
                f"Trades: {a['trades']:3d} | PnL: ${a['pnl']:8.2f} | "
                f"Sharpe: {a['sharpe']:5.2f} | DD: {a['recalculated_dd_pct']:5.2%}"
            )
        lines.append("")
    else:
        lines.append("❌ NO QUALIFIED RESULTS UNDER CORRECT CALCULATION")
        lines.append("All results failed to meet qualification criteria.")
        lines.append("")
        lines.append("Possible reasons:")
        lines.append("  1. Max DD threshold is too strict for this test period")
        lines.append("  2. Parameter space doesn't include profitable combinations")
        lines.append("  3. Test period was unusually volatile or unfavorable")
        lines.append("")
    
    # Results that would qualify under buggy calculation but not correct
    false_qualifications = [
        a for a in audits 
        if a["qualified_buggy"] and not a["qualified_correct"]
    ]
    if false_qualifications:
        lines.append("⚠️  FALSE QUALIFICATIONS (Buggy Calculation):")
        lines.append("These would have passed under the bug (comparing $ vs %):")
        lines.append("-" * 80)
        for a in false_qualifications[:5]:
            lines.append(
                f"{a['instrument']:10s} | Raw DD: ${a['raw_dd_dollars']:8.2f} | "
                f"Actual DD%: {a['recalculated_dd_pct']:5.2%} (exceeds {max_dd_threshold:.0%} threshold)"
            )
        lines.append("")
    
    # Diagnostic: Show DD distribution
    lines.append("DRAWDOWN DISTRIBUTION:")
    lines.append("-" * 80)
    dd_buckets = {
        "0-1%": 0,
        "1-2%": 0,
        "2-5%": 0,
        "5-10%": 0,
        "10-15%": 0,
        ">15%": 0,
    }
    for a in audits:
        dd = a["recalculated_dd_pct"]
        if dd < 0.01:
            dd_buckets["0-1%"] += 1
        elif dd < 0.02:
            dd_buckets["1-2%"] += 1
        elif dd < 0.05:
            dd_buckets["2-5%"] += 1
        elif dd < 0.10:
            dd_buckets["5-10%"] += 1
        elif dd < 0.15:
            dd_buckets["10-15%"] += 1
        else:
            dd_buckets[">15%"] += 1
    
    for bucket, count in dd_buckets.items():
        pct = (count / len(audits) * 100) if audits else 0
        bar = "█" * int(pct / 2)  # Scale to 50 chars max
        lines.append(f"{bucket:>8s}: {count:4d} ({pct:5.1f}%) {bar}")
    lines.append("")
    
    lines.append("=" * 80)
    lines.append("DIAGNOSTIC RECOMMENDATIONS:")
    lines.append("=" * 80)
    
    if not qualified_correct:
        lines.append("1. IMMEDIATE: Verify NAV matches live deployment")
        lines.append(f"   Current assumption: ${live_nav:,.0f}")
        lines.append("   Run: curl -H 'Authorization: Bearer $OANDA_TOKEN' \\")
        lines.append("             https://api-fxpractice.oanda.com/v3/accounts/$OANDA_ACCOUNT")
        lines.append("")
        lines.append("2. Consider loosening qualification constraints:")
        lines.append(f"   - Increase max_dd_threshold from {max_dd_threshold:.0%} to 20% or 25%")
        lines.append(f"   - Decrease min_trades from {min_trades} to 5 or 8")
        lines.append("")
        lines.append("3. Expand test period from 4 weeks to 12-16 weeks")
        lines.append("   More data = higher confidence in parameter stability")
        lines.append("")
    
    if mismatches:
        lines.append("4. BUG DETECTED: max_dd_pct calculation is incorrect")
        lines.append("   Check the drawdown normalization in the active optimizer path:")
        lines.append("   Should be: 'max_dd_pct': m.max_drawdown / NAV")
        lines.append(f"   Currently storing: {audits[0]['stored_dd_pct']:.6f}")
        lines.append(f"   Should be: {audits[0]['recalculated_dd_pct']:.6f}")
        lines.append("")
    
    lines.append("5. Run paper trading validation:")
    lines.append("   Deploy top candidate(s) in read-only mode for 2 weeks")
    lines.append("   Compare live DD vs backtest predictions")
    lines.append("   If live DD > 2x backtest DD, reject these parameters")
    lines.append("")
    
    lines.append("=" * 80)
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Audit optimization results for unit mismatches and qualification errors"
    )
    parser.add_argument(
        "results_file",
        type=Path,
        help="Path to optimization results JSON (e.g., output/ultimate_candidate_results.json)",
    )
    parser.add_argument(
        "--nav",
        type=float,
        default=100_000.0,
        help="Live account NAV for DD% recalculation (default: 100000)",
    )
    parser.add_argument(
        "--max-dd-threshold",
        type=float,
        default=0.15,
        help="Max drawdown threshold for qualification (default: 0.15 = 15%%)",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=10,
        help="Minimum trade count for qualification (default: 10)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write report to file instead of stdout",
    )
    
    args = parser.parse_args()
    
    if not args.results_file.exists():
        print(f"Error: Results file not found: {args.results_file}", file=sys.stderr)
        return 1
    
    # Load and audit results
    results = load_results(args.results_file)
    
    if not results:
        print("Error: No results found in file", file=sys.stderr)
        return 1
    
    all_audits = []
    for instrument, inst_results in results.items():
        for result in inst_results:
            audit = audit_result(
                instrument,
                result,
                args.nav,
                args.max_dd_threshold,
                args.min_trades,
            )
            all_audits.append(audit)
    
    # Generate report
    report = format_audit_report(
        all_audits,
        args.nav,
        args.max_dd_threshold,
        args.min_trades,
    )
    
    # Output
    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report written to {args.output}")
    else:
        print(report)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
