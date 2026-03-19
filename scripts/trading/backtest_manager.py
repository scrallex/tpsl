import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from scripts.trading.live_params import extract_signal_payload

# BacktestRunner missing from codebase

logger = logging.getLogger(__name__)

DEFAULT_BACKTEST_NAV = 100000
LIVE_PARAMS_CANDIDATES = (
    Path("output/live_params.json"),
    Path("config/live_params.json"),
)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def compute_week_range() -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=7), now


def load_grid_config(path: Any) -> Dict[str, Any]:
    return {}


def resolve_live_params_path() -> Optional[Path]:
    for candidate in LIVE_PARAMS_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


class BacktestManager:
    """Manages background backtest jobs triggered via the live trading service."""

    def __init__(self, portfolio_manager: Any, enabled_pairs: Iterable[str]) -> None:
        self.portfolio_manager = portfolio_manager
        self.enabled_pairs = list({inst.upper() for inst in enabled_pairs})

        self.backtest_results_path = Path(
            os.getenv("BACKTEST_RESULTS_PATH", "output/backtests/latest.json")
        )
        self.backtest_partial_path = self.backtest_results_path.with_suffix(
            ".partial.json"
        )
        self.backtest_error_path = self.backtest_results_path.with_suffix(".error.json")
        self.backtest_grid_config = Path(
            os.getenv("BACKTEST_GRID_CONFIG", "output/backtests/grid.json")
        )

        self._backtest_lock = threading.Lock()
        self._backtest_status: Dict[str, Any] = {"state": "idle"}
        self._backtest_thread: Optional[threading.Thread] = None

    def latest_results(self) -> Dict[str, Any]:
        base_path = self.backtest_results_path
        partial = self.backtest_partial_path
        error_file = self.backtest_error_path
        target_path = base_path
        if partial.exists():
            target_path = partial
        elif not base_path.exists() and error_file.exists():
            try:
                return json.loads(error_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"error": "unreadable"}
        elif not base_path.exists():
            return {"error": "not_found"}
        try:
            return json.loads(target_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read backtest results: %s", exc)
            return {"error": "unreadable"}

    def get_status(self) -> Dict[str, Any]:
        with self._backtest_lock:
            status = dict(self._backtest_status)
        if status.get("state") == "running" and self.backtest_partial_path.exists():
            try:
                partial = json.loads(
                    self.backtest_partial_path.read_text(encoding="utf-8")
                )
                if partial.get("progress"):
                    status["progress"] = partial["progress"]
                if partial.get("window") and "window" not in status:
                    status["window"] = partial["window"]
            except (json.JSONDecodeError, OSError):
                pass
        return status

    def trigger(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        instruments: Optional[Iterable[str]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        with self._backtest_lock:
            if self._backtest_status.get("state") == "running":
                return False, dict(self._backtest_status)

            try:
                if start and end:
                    start_dt = datetime.fromisoformat(
                        start.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    end_dt = datetime.fromisoformat(
                        end.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                else:
                    start_dt, end_dt = compute_week_range()
            except ValueError as exc:
                logger.warning("Invalid backtest window: %s", exc)
                return False, {"state": "idle", "error": "invalid_time_range"}

            selected_instruments = [
                inst.upper() for inst in (instruments or self.enabled_pairs)
            ]
            if not selected_instruments:
                selected_instruments = ["EUR_USD"]

            job_id = datetime.now(timezone.utc).isoformat()
            self._backtest_status = {
                "state": "running",
                "job_id": job_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "message": "Backtest grid running",
                "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
                "instruments": selected_instruments,
            }
            for stale in (self.backtest_partial_path, self.backtest_error_path):
                try:
                    if stale.exists():
                        stale.unlink()
                except OSError:
                    logger.debug("Failed to remove stale backtest artifact: %s", stale)

            self._backtest_thread = threading.Thread(
                target=self._run_job,
                args=(start_dt, end_dt, tuple(selected_instruments), job_id),
                name="BacktestGrid",
                daemon=True,
            )
            self._backtest_thread.start()
            return True, dict(self._backtest_status)

    def _run_job(
        self,
        start: datetime,
        end: datetime,
        instruments: Tuple[str, ...],
        job_id: str,
    ) -> None:
        try:
            gate_client = getattr(
                getattr(self.portfolio_manager, "gate_reader", None), "_client", None
            )
            if gate_client and instruments:
                latest_ts = None
                for inst in instruments:
                    try:
                        row = gate_client.zrevrange(
                            f"gate:index:{inst}", 0, 0, withscores=True
                        )
                    except Exception:
                        row = None
                    if row:
                        _, score = row[0]
                        if latest_ts is None or score > latest_ts:
                            latest_ts = score
                if latest_ts:
                    latest_dt = datetime.fromtimestamp(
                        float(latest_ts) / 1000.0, tz=timezone.utc
                    )
                    if latest_dt < end:
                        end = latest_dt
                    start_candidate = latest_dt - timedelta(days=5)
                    if start_candidate > start:
                        start = start_candidate
            if end <= start:
                end = start + timedelta(days=5)
            now = datetime.now(timezone.utc)
            if end > now:
                end = now
            grid = load_grid_config(self.backtest_grid_config)
            redis_url = (
                os.getenv("VALKEY_URL")
                or os.getenv("REDIS_URL")
                or "redis://localhost:6379/0"
            )
            nav = float(
                os.getenv("BACKTEST_NAV", str(DEFAULT_BACKTEST_NAV))
                or DEFAULT_BACKTEST_NAV
            )
            nav_risk_pct = float(os.getenv("BACKTEST_NAV_RISK_PCT", "0.01") or 0.01)
            cost_bps = float(os.getenv("BACKTEST_COST_BPS", "1.5") or 1.5)
            granularity = os.getenv("BACKTEST_GRANULARITY", "S5") or "S5"

            try:
                from scripts.research.simulator.backtest_simulator import (
                    TPSLBacktestSimulator,
                )
                from scripts.research.simulator.models import TPSLSimulationParams
            except ImportError as exc:
                raise RuntimeError(
                    "backtest_runtime_unavailable: scripts/research is not available in this environment"
                ) from exc

            live_params = {}
            params_file = resolve_live_params_path()
            if params_file is not None:
                try:
                    live_params = json.loads(params_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            signal_type = (
                os.getenv("BACKTEST_SIGNAL_TYPE")
                or os.getenv("SIGNAL_TYPE")
                or "mean_reversion"
            )
            is_mean_reversion = signal_type == "mean_reversion"

            sim = TPSLBacktestSimulator(
                redis_url=redis_url,
                nav=nav,
                nav_risk_pct=nav_risk_pct,
                cost_bps=cost_bps,
                granularity=granularity,
            )

            results_list = []
            for inst in instruments:
                inst_profile = sim.profile.get(inst)
                require_st_peak = bool(
                    getattr(inst_profile, "require_st_peak", is_mean_reversion)
                )
                p = extract_signal_payload(live_params.get(inst, {}), signal_type) or {}
                sim_params = TPSLSimulationParams(
                    hazard_override=None if is_mean_reversion else _float_or_none(p.get("Haz")),
                    hazard_min=_float_or_none(p.get("Haz")) if is_mean_reversion else None,
                    signal_type=signal_type,
                    min_repetitions=_int_or_default(p.get("Reps"), 1),
                    hold_minutes=_int_or_default(p.get("Hold"), 60),
                    stop_loss_pct=_float_or_none(p.get("SL")),
                    take_profit_pct=_float_or_none(p.get("TP")),
                    trailing_stop_pct=_float_or_none(p.get("Trail")),
                    breakeven_trigger_pct=_float_or_none(p.get("BE")),
                    hazard_exit_threshold=_float_or_none(p.get("HazEx")),
                    coherence_threshold=_float_or_none(p.get("Coh")),
                    entropy_threshold=_float_or_none(p.get("Ent")),
                    stability_threshold=_float_or_none(p.get("Stab")),
                    invert_bundles=is_mean_reversion,
                    st_peak_mode=require_st_peak,
                )

                cache = Path(f"output/market_data/{inst}.jsonl")
                sim.cache_path = cache if cache.exists() else None

                res = sim.simulate(
                    inst,
                    start=start,
                    end=end,
                    params=sim_params,
                    instrument_profile=inst_profile,
                )
                if res:
                    results_list.append(res.to_dict())

            summary = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "results": results_list,
            }

            self.backtest_results_path.parent.mkdir(parents=True, exist_ok=True)
            self.backtest_results_path.write_text(json.dumps(summary))
            summary = {"generated_at": datetime.now(timezone.utc).isoformat()}
            with self._backtest_lock:
                self._backtest_status = {
                    "state": "completed",
                    "job_id": job_id,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "message": "Backtest grid completed",
                    "window": summary.get(
                        "window", {"start": start.isoformat(), "end": end.isoformat()}
                    ),
                    "instruments": list(instruments),
                    "generated_at": summary.get("generated_at"),
                }
        except Exception as exc:
            logger.exception("Backtest grid failed")
            try:
                self.backtest_error_path.parent.mkdir(parents=True, exist_ok=True)
                self.backtest_error_path.write_text(
                    json.dumps(
                        {
                            "error": str(exc),
                            "window": {
                                "start": start.isoformat(),
                                "end": end.isoformat(),
                            },
                            "job_id": job_id,
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                logger.debug("Failed to persist backtest error payload", exc_info=True)
            with self._backtest_lock:
                self._backtest_status = {
                    "state": "error",
                    "job_id": job_id,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "message": str(exc),
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                }
