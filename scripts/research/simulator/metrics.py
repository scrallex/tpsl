import dataclasses
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class CoreMetrics:
    pnl: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    trades: int
    win_rate: float
    profit_factor: float


@dataclass
class ExecutionMetrics:
    avg_mae: float
    avg_mfe: float
    avg_hold_minutes: float
    avg_win_pnl: float = 0.0
    avg_loss_pnl: float = 0.0


@dataclass
class ExitMetrics:
    tp_exits: int = 0
    sl_exits: int = 0
    trail_exits: int = 0
    be_exits: int = 0
    hazard_exits: int = 0
    time_exits: int = 0
    tp_hit_rate: float = 0.0
    sl_hit_rate: float = 0.0


@dataclass
class BundleMetrics:
    bundle_trade_count: int = 0
    bundle_win_rate: float = 0.0
    bundle_pnl: float = 0.0


@dataclass
class TPSLSimulationMetrics:
    """Enriched metrics with TP/SL statistics."""

    core: CoreMetrics
    execution: ExecutionMetrics
    exits: ExitMetrics
    bundle: BundleMetrics

    def to_dict(self) -> Dict[str, Any]:
        return {
            **dataclasses.asdict(self.core),
            **dataclasses.asdict(self.execution),
            **dataclasses.asdict(self.exits),
            **dataclasses.asdict(self.bundle),
        }


def compute_r_multiples(trades: list[dict], sl_pct: float) -> tuple[float, float]:
    """Calculate Return Multiples (R-Multiples) for a simulated trade list."""
    from scripts.research.simulator.pricing_utils import convert_to_usd

    gross_wins_r = 0.0
    gross_losses_r = 0.0

    for t in trades:
        risk_base = t["entry_price"] * sl_pct * abs(t["units"])
        risk_usd = convert_to_usd(t["instrument"], risk_base, t["entry_price"])

        r_mult = t["pnl"] / risk_usd if risk_usd > 0 else 0.0
        t["r_multiple"] = round(r_mult, 4)

        if r_mult > 0:
            gross_wins_r += r_mult
        else:
            gross_losses_r += abs(r_mult)

    pf_r = gross_wins_r / gross_losses_r if gross_losses_r > 0 else float("inf")
    expected_r = (gross_wins_r - gross_losses_r) / len(trades) if trades else 0.0
    return round(pf_r, 4), round(expected_r, 4)
