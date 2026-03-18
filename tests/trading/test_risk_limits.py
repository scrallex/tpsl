from scripts.trading.risk_limits import RiskLimits, RiskManager


def test_can_add_enforces_position_and_portfolio_exposure_caps() -> None:
    risk_manager = RiskManager(
        RiskLimits(
            max_position_size=100.0,
            max_total_exposure=200.0,
            max_total_positions=3,
        )
    )
    risk_manager.record_fill("EUR_USD", 50, 1.0)

    assert risk_manager.can_add("EUR_USD", 60, price=1.0) is False
    assert risk_manager.can_add("GBP_USD", 160, price=1.0) is False
    assert risk_manager.can_add("GBP_USD", 100, price=1.0) is True
