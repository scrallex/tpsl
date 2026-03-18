from scripts.tools.push_config import iter_updates


def test_iter_updates_prefers_signal_block() -> None:
    payload = {
        "eur_usd": {
            "mean_reversion": {"Haz": 0.9, "Reps": 1},
            "trend_sniper": {"Haz": 0.2, "Reps": 3},
        },
        "USD_JPY": {"mean_reversion": {"Haz": 0.85, "Reps": 2}},
    }

    updates = list(iter_updates(payload, signal_type="mean_reversion"))

    assert updates == [
        ("EUR_USD", {"Haz": 0.9, "Reps": 1}),
        ("USD_JPY", {"Haz": 0.85, "Reps": 2}),
    ]


def test_iter_updates_filters_instruments_and_passes_flat_payloads() -> None:
    payload = {
        "EUR_USD": {"Haz": 0.9, "Reps": 1},
        "USD_JPY": {"Haz": 0.85, "Reps": 2},
    }

    updates = list(
        iter_updates(
            payload,
            signal_type="mean_reversion",
            instruments=["usd_jpy"],
        )
    )

    assert updates == [("USD_JPY", {"Haz": 0.85, "Reps": 2})]
