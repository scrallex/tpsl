"""Helpers for loading local shell-style env files used by SEP tooling."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]


def load_env_file(
    env_path: Path,
    *,
    override: bool = False,
    only_keys: Iterable[str] | None = None,
) -> dict[str, str]:
    """Load KEY=VALUE pairs from a local env file into os.environ."""

    if not env_path.exists():
        return {}

    allowed = {key for key in (only_keys or []) if key}
    loaded: dict[str, str] = {}

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or (allowed and key not in allowed):
            continue

        resolved = os.path.expandvars(value.strip().strip('"').strip("'"))
        if not override and key in os.environ:
            continue

        os.environ[key] = resolved
        loaded[key] = resolved

    return loaded


def load_oanda_env(root: Path | None = None, *, override: bool = True) -> dict[str, str]:
    """Load OANDA.env with shell-like override behavior."""

    base_dir = root or ROOT
    return load_env_file(base_dir / "OANDA.env", override=override)
