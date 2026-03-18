"""Local environment helpers for the isolated options research package."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = PACKAGE_ROOT / ".env"


def load_options_env(env_path: Path | None = None) -> None:
    """Load options-specific environment variables from a local ignored env file."""

    target = env_path or DEFAULT_ENV_PATH
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
