from __future__ import annotations

from datetime import datetime, timezone

from options_research.models import BacktestResult, EquityPoint
from options_research.reporting import BasicMetricsCalculator


def test_basic_metrics_calculator_handles_multi_day_equity_curve() -> None:
    result = BacktestResult(
        strategy_name="test_strategy",
        started_at=datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc),
        positions=(),
        equity_curve=(
            EquityPoint(
                timestamp=datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc),
                equity=100000.0,
                cash=100000.0,
                drawdown=0.0,
            ),
            EquityPoint(
                timestamp=datetime(2026, 3, 3, 21, 0, tzinfo=timezone.utc),
                equity=101000.0,
                cash=101000.0,
                drawdown=0.0,
            ),
            EquityPoint(
                timestamp=datetime(2026, 3, 4, 21, 0, tzinfo=timezone.utc),
                equity=100500.0,
                cash=100500.0,
                drawdown=0.0049504950495049506,
            ),
        ),
        metrics={},
        config={},
    )

    report = BasicMetricsCalculator().compute(result)

    assert report.metrics["total_return"] == 0.005
    assert report.metrics["max_drawdown"] > 0.0
    assert "sharpe" in report.metrics
