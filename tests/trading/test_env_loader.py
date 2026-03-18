from __future__ import annotations

import os

from scripts.trading.env_loader import load_oanda_env


def test_load_oanda_env_overrides_stale_shell_values(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "OANDA.env"
    env_path.write_text(
        "\n".join(
            [
                "OANDA_API_KEY=file-live-key",
                "OANDA_ACCOUNT_ID=file-live-account",
                "OANDA_ENVIRONMENT=live",
                "OANDA_API_TOKEN=${OANDA_API_KEY}",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("OANDA_API_KEY", "stale-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "stale-account")
    monkeypatch.delenv("OANDA_ENVIRONMENT", raising=False)
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)

    loaded = load_oanda_env(tmp_path, override=True)

    assert loaded["OANDA_API_KEY"] == "file-live-key"
    assert os.environ["OANDA_API_KEY"] == "file-live-key"
    assert os.environ["OANDA_ACCOUNT_ID"] == "file-live-account"
    assert os.environ["OANDA_ENVIRONMENT"] == "live"
    assert os.environ["OANDA_API_TOKEN"] == "file-live-key"
