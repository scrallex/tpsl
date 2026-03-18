#!/usr/bin/env python3
"""Verify required auxiliary Valkey keys exist before running the trading stack."""
from __future__ import annotations


import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scripts.tools.cli_runner import CLIRunner

try:
    import redis  # type: ignore
except Exception as exc:  # pragma: no cover - optional dependency missing
    redis = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


DEFAULT_KEYS = [
    ("ops:kill_switch", "Global kill switch engagement flag (1 = engaged)"),
    ("risk:nav_snapshot", "Last known NAV snapshot used for sizing"),
]


def _parse_keys(raw: Optional[Sequence[str]]) -> List[str]:
    if not raw:
        return []
    keys: List[str] = []
    for item in raw:
        if not item:
            continue
        if "," in item:
            keys.extend(part.strip() for part in item.split(",") if part.strip())
        else:
            keys.append(item.strip())
    return [key for key in keys if key]


def check_keys(
    redis_url: str, keys: Sequence[str]
) -> Tuple[List[Dict[str, object]], List[str]]:
    if not redis:
        raise RuntimeError(f"redis dependency missing: {_IMPORT_ERROR}")
    client = redis.from_url(redis_url)
    summary: List[Dict[str, object]] = []
    missing: List[str] = []
    for key in keys:
        try:
            exists = bool(client.exists(key))
        except Exception as exc:  # pragma: no cover - network error path
            raise RuntimeError(f"unable to query {key}: {exc}") from exc
        ttl = None
        if exists:
            try:
                ttl = client.ttl(key)
            except Exception:
                ttl = None
        value = None
        if exists:
            raw = client.get(key)
            if raw is not None:
                try:
                    value = raw.decode("utf-8")
                except Exception:
                    value = raw
        else:
            missing.append(key)
        summary.append(
            {
                "key": key,
                "exists": exists,
                "ttl": ttl,
                "value": value,
            }
        )
    return summary, missing


def main(args) -> int:
    keys = _parse_keys(args.keys) or [key for key, _ in DEFAULT_KEYS]

    try:
        summary, missing = check_keys(args.redis, keys)
    except Exception as exc:
        print(f"Aux key check failed: {exc}", file=sys.stderr)
        return 2

    payload = {
        "checked_keys": keys,
        "results": summary,
        "missing": missing,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        descriptions = {key: desc for key, desc in DEFAULT_KEYS}
        for item in summary:
            key = item["key"]
            exists = item["exists"]
            desc = descriptions.get(key, "")
            marker = "OK" if exists else "MISSING"
            ttl = item.get("ttl")
            ttl_str = (
                "∞"
                if ttl is not None and ttl < 0
                else (f"{ttl}s" if ttl is not None else "n/a")
            )
            print(
                f"[{marker:7}] {key:20} ttl={ttl_str:>6} value={item.get('value')} {('- ' + desc) if desc else ''}"
            )
        if missing:
            print(f"{len(missing)} required keys missing", file=sys.stderr)

    return 2 if missing else 0


if __name__ == "__main__":  # pragma: no cover
    runner = CLIRunner("Check auxiliary Valkey keys required by the trading backend")
    runner.add_redis_arg()
    runner.add_arg(
        "--keys",
        nargs="*",
        help="Specific keys to verify (comma separated or repeated). Defaults to ops:kill_switch and risk:nav_snapshot.",
    )
    runner.add_arg("--json", action="store_true", help="Emit JSON output for scripting")
    runner.run(main)
