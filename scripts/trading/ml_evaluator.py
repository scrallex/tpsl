import logging
import pickle
import collections
from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class MLEvaluator:
    def __init__(self, enabled_instruments: list[str], *, enabled: bool = True):
        self.enabled = bool(enabled)
        self.models = {}
        self.p98_thresholds = {}
        self.st_history = collections.defaultdict(lambda: collections.deque(maxlen=30))
        self.last_st_val = {}
        self.features = [
            "lambda_hazard", "coherence", "entropy", "stability", 
            "reps", "st", "st_peak", "rsi", "volatility", "dist_ema60"
        ]

        if not self.enabled:
            logger.info("Live ML gate disabled; skipping model load.")
            return
        
        for inst in enabled_instruments:
            model_path = Path(f"output/models/{inst}_histgbm.pkl")
            features_path = Path(f"output/ml_data/{inst}_features.parquet")
            
            if model_path.exists() and features_path.exists():
                logger.info(f"[{inst}] Loading Live ML Model for P98 Execution...")
                try:
                    with open(model_path, "rb") as f:
                        model = pickle.load(f)
                    
                    df = pd.read_parquet(features_path)
                    probs = model.predict_proba(df[self.features])[:, 1]
                    p98 = pd.Series(probs).quantile(0.98)
                    
                    self.models[inst] = model
                    self.p98_thresholds[inst] = p98
                    logger.info(f"[{inst}] Live ML Model Loaded. P98 Target: {p98:.4f}")
                except Exception as e:
                    logger.error(f"[{inst}] Failed to load model or compute P98: {e}")
            else:
                logger.warning(f"[{inst}] Missing ML model or features at startup. ML disabled for {inst}.")

    def evaluate_gate(
        self,
        inst: str,
        payload: Dict[str, Any],
        service: Any,
        current_st: float,
        reps: float,
        *,
        ml_primary_gate: bool = False,
    ) -> Tuple[bool, str]:
        if not self.enabled:
            return True, ""

        if inst not in self.models:
            return True, ""

        try:
            # Update ST Context Continuous History
            self.st_history[inst].append(current_st)
            prev_st = self.last_st_val.get(inst, current_st)
            self.last_st_val[inst] = current_st
            
            rolling_st_mean = sum(self.st_history[inst]) / len(self.st_history[inst]) if len(self.st_history[inst]) > 0 else 0
            st_peak = 1 if (current_st < prev_st and prev_st > rolling_st_mean) else 0

            # ML-primary mode evaluates every active directional gate.
            # Veto mode evaluates only gates already admitted upstream.
            dir_str = str(payload.get("direction", "FLAT")).upper()
            is_admitted = bool(payload.get("admit", 0))
            if dir_str == "FLAT":
                return True, ""
            if not ml_primary_gate and not is_admitted:
                return True, ""

            # Fetch Live S5 History for Technicals directly from the Streamer's ZSET to bypass the 5-minute HTTP cache
            import json
            redis_client = getattr(getattr(service, "price_history_cache", None), "_client", None)
            pts = []
            if redis_client:
                try:
                    raw_candles = redis_client.zrange(f"md:candles:{inst}:S5", -200, -1)
                    for b_blob in raw_candles:
                        try:
                            c = json.loads(b_blob.decode('utf-8') if isinstance(b_blob, bytes) else b_blob)
                            pts.append({"close": float(c.get("c", 0))})
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"[{inst}] Redis ZRANGE failed for S5 candles: {e}")
            
            # Fallback to OANDA cache if ZSET is completely empty or Valkey is down
            if not pts:
                history = service.price_history(inst, granularity="S5", count=200)
                pts = history.get("points") or []
            
            rsi, volatility, dist_ema60 = 0.0, 0.0, 0.0
            if len(pts) > 60:
                closes = [float(p.get("close", 0)) for p in pts if p.get("close") is not None]
                if len(closes) > 60:
                    df = pd.DataFrame({"close": closes})
                    
                    delta = df["close"].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / loss
                    df["rsi"] = 100 - (100 / (1 + rs))
                    
                    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
                    df["volatility"] = df["log_ret"].rolling(20).std()
                    
                    ema60 = df["close"].ewm(span=60).mean()
                    df["dist_ema60"] = (df["close"] - ema60) / ema60
                    
                    last_row = df.iloc[-1]
                    rsi = 0.0 if pd.isna(last_row["rsi"]) else last_row["rsi"]
                    volatility = 0.0 if pd.isna(last_row["volatility"]) else last_row["volatility"]
                    dist_ema60 = 0.0 if pd.isna(last_row["dist_ema60"]) else last_row["dist_ema60"]

            hz = float(payload.get("hazard", 0.0))
            coh = 0.0
            comps = payload.get("structure") or payload.get("components") or {}
            ent = 0.0
            stab = 0.0
            if isinstance(comps, dict):
                coh = float(comps.get("coherence", 0.0))
                ent = float(comps.get("entropy", 0.0))
                stab = float(comps.get("stability", 0.0))
                if hz == 0.0:
                    hz = float(comps.get("hazard", 0.0))

            feature_dict = {
                "lambda_hazard": [hz],
                "coherence": [coh],
                "entropy": [ent],
                "stability": [stab],
                "reps": [reps],
                "st": [current_st],
                "st_peak": [st_peak],
                "rsi": [rsi],
                "volatility": [volatility],
                "dist_ema60": [dist_ema60]
            }

            model = self.models[inst]
            p98 = self.p98_thresholds[inst]
            
            df_inf = pd.DataFrame(feature_dict)
            prob = model.predict_proba(df_inf[self.features])[0, 1]
            
            payload["ml_prob"] = prob
            
            if prob >= p98:
                return True, ""
            else:
                return False, f"ml_confidence_low:{prob:.2f}<{p98:.2f}"
                
        except Exception as e:
            logger.error(f"[{inst}] ML Evaluation API Error: {e}")
            return False, f"ml_eval_error"
