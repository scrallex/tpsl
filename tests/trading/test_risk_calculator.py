import pytest
from scripts.trading.risk_calculator import RiskSizer


def test_risk_sizer_compute_caps():
    sizer = RiskSizer(nav_risk_pct=0.01, per_position_pct_cap=0.02, alloc_top_k=3)
    caps = sizer.compute_caps(10000.0)
    assert caps.nav_risk_cap == pytest.approx(100.0)
    assert caps.per_position_cap == pytest.approx(100.0)
    assert caps.portfolio_cap == pytest.approx(300.0)


def test_target_units_base_usd():
    sizer = RiskSizer(nav_risk_pct=0.01, per_position_pct_cap=0.02, alloc_top_k=3)
    units, margin, adj_exp = sizer.target_units(
        "USD_CAD", target_exposure=100.0, exposure_scale=0.02, price_data={"mid": 1.35}
    )
    assert margin == pytest.approx(0.02)
    assert units == 5000


def test_target_units_quote_usd():
    sizer = RiskSizer(nav_risk_pct=0.01, per_position_pct_cap=0.02, alloc_top_k=3)
    units, margin, adj_exp = sizer.target_units(
        "EUR_USD", target_exposure=100.0, exposure_scale=0.02, price_data={"mid": 1.10}
    )
    assert margin == pytest.approx(0.022)
    assert units == int(100.0 / 0.022)


def test_compute_notional_caps_scales_scalar_budget() -> None:
    sizer = RiskSizer(nav_risk_pct=0.05, per_position_pct_cap=0.05, alloc_top_k=12)
    caps = sizer.compute_notional_caps(569.1944, exposure_scale=0.02)
    assert caps.per_position_cap == pytest.approx(1422.986)
    assert caps.portfolio_cap == pytest.approx(17075.836)
