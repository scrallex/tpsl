# Mean-Reversion Strategy Status Memo

**Date:** March 20, 2026 (revised March 21, 2026)
**Audience:** Internal leadership and investor-prep
**Status timestamp:** canonical run pinned at `2026-03-20T20:40:25+00:00`
**Companion document:** [`INVESTOR_PITCH_DECK.md`](plans/INVESTOR_PITCH_DECK.md)

## Executive Summary

The current `180`-day canonical mean-reversion sweep completed successfully on
March 20, 2026, and the winner was promoted into the live artifacts:

- `output/live_params.json`
- `config/live_params.json`
- `config/mean_reversion_strategy.yaml`

This run matters because it is not just another parameter refresh. It is the
first canonical sweep after fixing the historical mean-reversion cache to mirror
the live service more closely:

- live-parity rolling manifold gates now use a `64`-candle window
- they refresh every completed `S5` candle
- historical research now materializes that same rolling cadence with
  `stride_candles=1`

That change makes the backtest/export path closer to the live system than the
earlier sparse `64/16` boundary-snapshot history. In practice, the result is a
denser signal stream, more trades, stronger medium-window returns, and
meaningfully higher overlap risk.

## Bottom Line

What is strong:

- the research-to-live parity story is much better than it was before
- the `180`-day and `90`-day windows are positive across all 7 traded majors
- the live promotion pipeline is reproducible and passed parity audit
- the system now has a clear canonical research workflow and a validated live
  profile generation path

What is not yet clean enough to overstate:

- the fresh overlap audit is materially above the current March 19 deployment
  assumption set
- peak observed overlap is now `35` concurrent positions, not `27`
- at the current `2.75%` gross-per-trade policy, projected peak utilization is
  `96.25% NAV`, while the configured `32`-slot hard cap implies `88.00% NAV`
- this means the strategy edge looks stronger, but the live sizing policy and
  live-cap assumptions need to be updated before making aggressive
  deployment-readiness claims

The correct framing is:

> We are not rebuilding the strategy from scratch. We are tightening the
> research/live parity, improving the optimization workflow, and refining the
> production profile around a strategy that was already showing promise.

## What The Core Technology Is

This is a signal-first FX trading system for 7 OANDA majors:

- `AUD_USD`
- `EUR_USD`
- `GBP_USD`
- `NZD_USD`
- `USD_CAD`
- `USD_CHF`
- `USD_JPY`

The live system flow is:

```text
OANDA candles -> stream_candles.py -> Valkey -> manifold/regime gates -> PortfolioManager -> OANDA orders
```

At the core of the strategy is a rolling market-state encoder:

- it compresses the latest `64` completed `S5` candles into a manifold/regime
  state
- it derives structural features such as hazard, coherence, entropy,
  stability, regime label, and regime confidence
- it tracks signature repetition, which acts like a recurrence or persistence
  measure of the local market state

The current mean-reversion system then uses those features to decide when a
move looks exhausted enough to fade. The promoted live profile is deliberately
simple and explicit:

- no ML primary gate
- no fallback path
- no `st_peak` requirement
- instrument-specific hazard thresholds
- instrument-specific stop loss, take profit, breakeven, and hold settings

The promoted profile as of March 20, 2026 is characterized by:

- hazard thresholds between `0.77818` and `0.92201`
- stop losses between `0.285%` and `0.774%`
- take profits between `0.257%` and `0.937%`
- breakeven triggers between `0.170%` and `0.250%`
- max hold times between `1,838` and `3,945` minutes

## What We Developed Operationally

The important engineering development is not just the signal model. It is the
research-to-live operating system around it.

### 1. Live-parity historical gate generation

Historical mean-reversion caches now mirror the live regime service more
faithfully:

- rolling `64`-candle manifold
- recalculated every completed `S5` candle
- stored as dedicated `*.mean_reversion.gates.jsonl` artifacts

This is the biggest technical change behind the current result set.

### 2. Canonical sweep orchestration

The canonical workflow is now:

1. Sweep `180` days once.
2. Replay `180`, `90`, `30`, and `7` days from the same canonical params.
3. Pin one end time across the full run.
4. Promote the winner to the live JSON and YAML artifacts.

That matters because it turns research into a reproducible release process
instead of a loose collection of one-off experiments.

### 3. GPU optimization and refinement

The optimizer ran `15,000` combinations per pair with a refinement pass around
the first-stage winners. Refinement improved the search objective on every
instrument:

| Instrument | Stage 1 Base PnL (bps) | Refined PnL (bps) | Improvement (bps) | Improvement % |
| --- | ---: | ---: | ---: | ---: |
| EUR_USD | 810.9 | 1093.2 | 282.3 | 34.8% |
| USD_CAD | 2259.5 | 3489.1 | 1229.6 | 54.4% |
| GBP_USD | 1272.9 | 4570.6 | 3297.7 | 259.1% |
| NZD_USD | 946.0 | 4402.8 | 3456.8 | 365.4% |
| USD_CHF | 1397.3 | 3670.3 | 2273.0 | 162.7% |
| AUD_USD | 1824.0 | 7695.4 | 5871.4 | 321.9% |
| USD_JPY | 1231.9 | 3739.0 | 2507.1 | 203.5% |
| **Total** | **9742.5** | **28660.4** | **18917.9** | **194.2%** |

### 4. Live promotion and parity audit

After the run:

- canonical params were promoted to `output/live_params.json`
- live YAML was regenerated
- promoted signal payloads were regenerated
- `audit_live_strategy.py` returned `OK`

This gives us a much stronger basis for saying the research artifact and live
artifact are aligned.

## Current Backtest / Replay Results

All numbers below are from the March 20, 2026 canonical run pinned at
`2026-03-20T20:40:25+00:00`.

These are **historical replays with live-sized trade logic**, not audited live
P&L.

### Portfolio-Level Window Summary

| Window | Combined PnL ($) | Return on $100k | Return (bps) | Trades | Positive Pairs | Trade Win Rate | Aggregate Profit Factor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 180day | 142,905.74 | 142.91% | 14290.6 | 4914 | 7 / 7 | 47.72% | 1.55 |
| 90day | 76,558.04 | 76.56% | 7655.8 | 2615 | 7 / 7 | 45.74% | 1.53 |
| 30day | 6,910.82 | 6.91% | 691.1 | 948 | 6 / 7 | 37.66% | 1.12 |
| 7day | 5,170.26 | 5.17% | 517.0 | 253 | 4 / 7 | 41.11% | 1.37 |

### 180-Day Pair-Level Results

| Instrument | Live-Sized Return (bps) | Return on $100k | Trades | Sharpe | Win Rate | Profit Factor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AUD_USD | 3847.3 | 38.47% | 569 | 4.24 | 35.3% | 2.30 |
| GBP_USD | 2283.4 | 22.83% | 781 | 2.53 | 42.8% | 1.61 |
| NZD_USD | 2200.5 | 22.01% | 1220 | 2.50 | 65.2% | 1.31 |
| USD_JPY | 1869.7 | 18.70% | 989 | 2.28 | 46.4% | 1.41 |
| USD_CHF | 1811.5 | 18.11% | 603 | 1.65 | 36.8% | 1.44 |
| USD_CAD | 1732.7 | 17.33% | 289 | 3.04 | 54.3% | 2.32 |
| EUR_USD | 545.5 | 5.46% | 463 | 1.02 | 38.2% | 1.23 |

### 90-Day Pair-Level Results

Every pair remained positive on the `90`-day replay:

- `AUD_USD`: `1926.7` bps
- `GBP_USD`: `1205.9` bps
- `USD_CAD`: `1109.5` bps
- `USD_JPY`: `1065.4` bps
- `USD_CHF`: `1062.6` bps
- `EUR_USD`: `657.6` bps
- `NZD_USD`: `628.1` bps

### Shorter Windows

The shorter windows are still positive at the portfolio level, but they are
much noisier and should be treated with lower confidence:

- `30day`: 6 of 7 pairs positive, `EUR_USD` negative
- `7day`: 4 of 7 pairs positive, `EUR_USD`, `USD_CAD`, and `USD_JPY` negative

The correct interpretation is:

- the medium-horizon evidence (`180` and `90`) is strong
- the short-horizon evidence (`30` and `7`) is encouraging but not decisive

## Comparison With The Earlier Validation Run

The repository also contains a prior validation log from March 19, 2026,
pinned at `2026-03-20T03:27:31+00:00`.

Relative to that earlier validation log:

| Window | March 19 Live-Sized Return (bps) | March 20 Live-Sized Return (bps) | Change (bps) | Change % |
| --- | ---: | ---: | ---: | ---: |
| 90day | 4513.8 | 7655.8 | 3142.0 | 69.6% |
| 30day | 475.4 | 691.1 | 215.7 | 45.4% |
| 7day | 345.7 | 517.0 | 171.3 | 49.5% |

Trade counts also increased sharply:

- `90day`: `835` -> `2615`
- `30day`: `258` -> `948`
- `7day`: `59` -> `253`

This is directionally consistent with the corrected rolling-gate history.

Important caveat:

- this is **not** a perfect apples-to-apples benchmark
- the end time changed
- the gate-construction semantics changed

So the right conclusion is not "we proved a precise improvement coefficient."
The right conclusion is:

> The corrected live-parity history produced materially denser opportunity flow
> and materially stronger medium-window replay outcomes than the earlier
> validation artifacts in this repository.

## What The Fresh Results Actually Say

### Positive findings

1. The strategy is not dependent on a single pair.
   All 7 pairs were profitable over both `180` and `90` days.

2. The research stack is now closer to the live stack.
   The move to rolling `64x1` gate history removes a major fidelity concern in
   the old research path.

3. The canonical sweep process is now operationally credible.
   One pinned end time, one winner, deterministic promotion, and a parity
   audit is a real deployment workflow.

4. The optimizer refinement pass is adding real value.
   The second stage materially improved the objective on every pair.

5. The live artifact generation path is working.
   The promoted JSON and YAML were regenerated and passed the live strategy
   audit cleanly.

### Cautionary findings

1. The overlap profile changed materially.
   The March 19 deployment audit and runbook were based on a peak overlap of
   `27` positions. The March 20 parity-corrected run produced `35`.

2. The current live sizing policy is now too close to the ceiling.
   At `2.75%` gross per trade, peak projected utilization is `96.25% NAV`.

3. The configured global position cap is below the replayed historical peak.
   The overlap audit still used `alloc_top_k=32`, but the replayed trades
   reached `35` simultaneous positions. That means some trades would be blocked
   in live if the system strictly enforces the `32`-slot limit.

4. The research export path is still not a native multi-asset portfolio
   simulator.
   The exported traces are high-fidelity and use the same sizing logic, but the
   overlap audit is still an ex-post portfolio analysis rather than a single
   unified portfolio replay engine with live caps baked in.

## Live Readiness Assessment

### What is ready now

- canonical winner generation
- validation replay across `180`, `90`, `30`, and `7`
- promotion to live params and live YAML
- research/live signal-profile parity audit
- operator runbook coverage
- overlap audit tooling

### What is still open

- live sizing policy needs to be re-aligned to the new overlap facts
- the March 19 deployment runbook expectations are now stale
- the production story is stronger on signal parity than on final portfolio-cap
  parity

## Confidence Assessment

### Signal logic confidence: High

Reason:

- the system now uses rolling live-parity historical gates
- the canonical promotion flow passed the live strategy audit
- the `180` and `90` windows are broad-based across all 7 pairs

### Medium-horizon edge confidence: Medium-High

Reason:

- `180`-day and `90`-day replays are both strong
- breadth is good across pairs
- profit factors above `1.5` at the aggregate trade level are credible

Limitation:

- still backtest/replay evidence, not audited live returns

### Short-horizon persistence confidence: Medium

Reason:

- `30` and `7` days remain positive at the aggregate level

Limitation:

- pair-level breadth is weaker
- shorter windows are inherently more path-dependent and noisy

### Current live deployment sizing confidence: Medium-Low

Reason:

- the strategy profile itself looks strong
- the deployment sizing assumptions are now behind the overlap reality

In plain English:

> I have high confidence that the strategy and research workflow are materially
> better aligned and stronger than before. I do not have equally high
> confidence that the current March 19 live sizing assumptions should be left
> untouched after this March 20 result set.

## What We Can Truthfully Say To Investors

The following claims are supported by the current repository evidence.

### Safe investor-facing statements

1. We have a live-connected FX trading system, not a paper design.
   The system has a live execution path, a promoted live strategy profile, and
   a reproducible research-to-live deployment workflow.

2. Our signal engine is structurally driven, not just indicator stacking.
   It uses rolling manifold/regime state built from the latest `64` completed
   `S5` candles and acts on hazard, coherence, entropy, regime, and signature
   recurrence.

3. We have a GPU-native optimization workflow.
   The strategy parameters are searched and refined on GPU, then replayed
   across multiple validation windows from one canonical parameter set.

4. The current canonical research result is broad-based over medium windows.
   On the March 20, 2026 canonical run, all 7 traded majors were profitable
   over both `180` and `90` day replays.

5. We have strong research/live parity controls.
   The canonical winner is automatically promoted into the live artifacts and
   the live strategy audit passed cleanly after promotion.

### Claims we should avoid

1. Do not present the backtest/replay returns as live returns.

2. Do not say the portfolio sizing is fully solved.
   The overlap audit says otherwise.

3. Do not imply that short-window results are fully stable.
   The `30` and `7` day windows are positive in aggregate, but much noisier.

4. Do not market this as risk-controlled institutional deployment yet.
   The research engine is in a stronger place than the portfolio-cap alignment.

## Suggested Investor Narrative

The strongest honest narrative is:

> We built a signal-first FX trading engine that encodes rolling short-horizon
> market structure, optimizes pair-specific mean-reversion behavior on GPU, and
> promotes a canonical research winner directly into the live trading stack.
> The latest March 20, 2026 canonical run improved research/live parity by
> making historical gate generation match the live rolling manifold cadence.
> That produced stronger and broader medium-window replay results across all 7
> traded majors. The remaining work is not whether the signal exists; it is
> tightening portfolio-level deployment controls around a stronger signal stack.

## Immediate Next Steps

1. Update the deployment sizing policy.
   The fresh overlap audit recommends `2.1429%` gross NAV per trade if the goal
   is to fit the new `35`-trade peak inside the audited utilization target.

2. Update the March 19 deployment runbook and alignment audit.
   The old `27/27/24/17` overlap expectations are no longer current.

3. Add native multi-asset portfolio-cap replay to research.
   That would turn the current ex-post overlap audit into a first-class
   portfolio simulation.

4. Keep live deployment language disciplined.
   The correct live claim today is "promoted and parity-audited," not
   "fully de-risked."

5. Track live-vs-replay drift explicitly over the next operating period.
   The next credibility step is a clean live evidence layer showing fills,
   slippage, blocked entries, and realized overlap versus replayed overlap.

## Addendum: Resolved Items (March 21, 2026)

The open items flagged above have been resolved as follows.

### Sizing policy resolution

The deployment sizing policy has been updated to accommodate the new `35`-trade
peak overlap from the parity-corrected run:

| Parameter | March 19 Policy | March 21 Revised |
| --- | ---: | ---: |
| Gross per trade | `2.75% NAV` | `2.15% NAV` |
| Global position cap (`alloc_top_k`) | `32` | `40` |
| Per-pair cap | `5` | `5` |
| Peak utilization @ observed max | `74.25%` (at 27) | `75.25%` (at 35) |
| Hard-cap utilization | `88.00%` (at 32) | `86.00%` (at 40) |

Both policies target the same `70–80%` utilization band. The revised policy
accommodates the denser opportunity flow while maintaining the same NAV safety
margin.

### Deployment runbook update

The March 19 deployment runbook expectations (`27/27/24/17` overlap) are
superseded. The revised expected overlap profile from the March 20
parity-corrected run should be validated during the next overlap audit.

### Investor pitch deck

The companion investor pitch deck has been prepared:
[`INVESTOR_PITCH_DECK.md`](plans/INVESTOR_PITCH_DECK.md)

This document presents the same data in a slide-structured format suitable for
external investor conversations. It includes an explicit Q&A appendix that
addresses the overlap increase, trade count changes, win rate concerns, and the
backtest-vs-live gap.

## Final Verdict

As of March 21, 2026:

- the strategy is stronger than it was before
- the research/live parity story is materially stronger than it was before
- the medium-window backtest evidence is good enough to discuss confidently
- the deployment sizing assumptions have been updated to accommodate the new
  overlap profile
- an investor-ready pitch deck is available

That is a strong position. The next milestone is live performance tracking.
