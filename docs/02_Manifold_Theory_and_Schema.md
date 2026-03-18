# Manifold Theory & Gate Schema

The SEP Trading System relies on translating basic OHLC price action into a 5-dimensional structural manifold. This document outlines the physical principles behind the evaluation engine and provides the strict JSON payload schema expected by the streaming services.

## 1. Microstructure Physics (V4 Engine)

### The Structural Reality (S5 Fractality)
The core insight of the V4 Engine abandons macro-timeframe static thresholds in favor of pure dynamic microstructure physics. 

A 64-candle phase-space window on an S5 (5-second) tape represents only 5.3 minutes of real-time market motion. At this sub-atomic level, the market is overwhelmingly dominated by Brownian motion and market-maker quote adjustments. 

### Dynamic Percentile Normalization
**Rule: Never use Z-Scores for bounded topological metrics ($c$, $e$, $\lambda$).**

Structural metrics are strictly bounded between `0.0` and `1.0` and are heavily skewed. If volatility drops, standard deviation collapses to near zero, causing tiny fluctuations to trigger massive statistical distortions. 

The V4 Engine relies exclusively on **1440-period Rolling Percentiles** (the trailing 2 hours). The structural validity of an entry is determined by ranking the current physical state of the order book against the exact recent past of the specific asset.

### Tension Resolution Horizons
Because the Engine evaluates tension over a 5.3-minute window, it is a physical fallacy to expect that tension to release in 60 seconds, especially when the bid-ask spread consumes 60% of the initial variance.

**Horizon Scaling:**
* **Horizon 1:** 36 candles (3 minutes) - Initial kinetic release.
* **Horizon 2:** 120 candles (10 minutes) - Primary microstructure trend.
* **Horizon 3:** 360 candles (30 minutes) - Macro alignment.

## 2. Gate Definitions

The system defines specific combinations of structural percentiles as "Gates". The primary gates are:

### Gate A: The "Liquidity Trap" (Weaponized Fade)
When the Manifold Engine flags extreme kinetic compression (Entropy $\le$ 5th percentile), the order book has gone empty. The liquidity providers have pulled their quotes. 

When the inevitable price breakout occurs (Delta $\ge$ 4), it is an algorithmic stop-hunt ripping through a thin book. Within 10 to 30 seconds, the market makers step back in and snap the mid-price back.
* **Alpha:** We wait up to 2 candles for the direction to flip **against** the $T_0$ breakout, and execute the fade. 

### Gate C: The Decoupled "Ghost Dip"
Historically, high Coherence requirements starved pullback setups due to the **Coherence Decay Law**: a nominal price pullback physically bends the spatial vector, instantly dropping Coherence. Seeking "high coherence during a pullback" is a mathematical paradox.
* **Alpha:** The V4 Engine verifies high Coherence ($T_{-5}$ to $T_{-3}$) *before* the pullback begins. The pullback itself ($T_{-2}$ to $T_{-1}$) is measured strictly via nominal price drift. When the trend violently resumes ($T_0$), we fire.

## 3. Canonical Gate Payload Schema

Gate payloads are JSON objects stored in Valkey at `gate:last:{INSTRUMENT}`.

**Design Principles:**
1. **Canonical metrics** - Values always live under `payload.metrics` (no fallback searches).
2. **Immutable timestamps** - `ts_ms` uniquely identifies the gate.
3. **Explicit admission** - `admit` field is the hard gate (1 = trade-eligible, 0 = blocked).

### Example JSON Payload
```json
{
  "ts_ms": 1706882400000,
  "instrument": "EUR_USD",
  "admit": 1,
  "repetitions": 3,
  "signal_key": "gate:EUR_USD:1706882400000",
  "metrics": {
    "coherence": 0.75,
    "stability": 0.82,
    "entropy": 0.45,
    "hazard": 0.28,
    "coherence_tau_slope": 0.012,
    "domain_wall_slope": 0.008,
    "spectral_lowf_share": 0.65,
    "reynolds_ratio": 1.2,
    "temporal_half_life": 12.5,
    "spatial_corr_length": 8.3,
    "pinned_alignment": 0.88
  },
  "regime": {
    "label": "ranging",
    "confidence": 0.72
  },
  "bundle_hits": [
    {
      "id": "SCALP_LONG",
      "action": "promote",
      "score": 0.85,
      "hold_minutes": 30
    }
  ],
  "bundle_blocks": []
}
```

### Python Validation
All pipeline validation inside the live system must utilize `scripts/trading/gate_evaluator.py`, verifying the `admit` block and cross-verifying the dynamic thresholds loaded from the asset configuration prior to executing.
