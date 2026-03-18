import dataclasses
from dataclasses import dataclass
from typing import Optional

from scripts.trading.tpsl import TPSLConfig


@dataclass(frozen=True)
class TPSLSimulationParams:
    """Simulation parameters with TP/SL additions."""

    hazard_multiplier: Optional[float] = 1.0
    hazard_override: Optional[float] = None
    hazard_min: Optional[float] = None
    signal_type: Optional[str] = None
    min_repetitions: int = 1
    hold_minutes: int = 30
    exposure_scale: float = 0.02
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    breakeven_trigger_pct: Optional[float] = None
    hazard_exit_threshold: Optional[float] = None
    coherence_threshold: Optional[float] = None
    entropy_threshold: Optional[float] = None
    stability_threshold: Optional[float] = None
    st_percentile: Optional[float] = None
    st_reversal_mode: bool = False
    allow_fallback: Optional[bool] = None
    bundles_only: bool = False
    invert_bundles: bool = False
    ml_primary_gate: bool = False
    disable_bundle_overrides: bool = False
    disable_stacking: bool = False
    st_peak_mode: bool = False

    def to_tpsl_config(self) -> TPSLConfig:
        if not any(
            (
                self.stop_loss_pct,
                self.take_profit_pct,
                self.trailing_stop_pct,
                self.breakeven_trigger_pct,
            )
        ):
            return TPSLConfig()
        return TPSLConfig(
            stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=self.take_profit_pct,
            trailing_stop_pct=self.trailing_stop_pct,
            breakeven_trigger_pct=self.breakeven_trigger_pct,
        )
