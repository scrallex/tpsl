#!/usr/bin/env python3
"""Push optimizer winners to the live strategy update webhook."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Tuple


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="POST one or more optimizer winners to /api/strategy/update."
    )
    parser.add_argument(
        "--payload",
        default="output/live_params.json",
        help="Path to the optimizer params JSON.",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Full target URL, e.g. http://127.0.0.1:8000/api/strategy/update",
    )
    parser.add_argument(
        "--signal-type",
        default="mean_reversion",
        help="Signal block to extract from the params JSON.",
    )
    parser.add_argument(
        "--instrument",
        nargs="*",
        help="Optional instrument subset; defaults to all payload instruments.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    return parser.parse_args()


def _selected_instruments(raw: Iterable[str] | None) -> set[str]:
    return {item.strip().upper() for item in (raw or []) if item and item.strip()}


def iter_updates(
    payload: Dict[str, Any],
    *,
    signal_type: str,
    instruments: Iterable[str] | None = None,
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    selected = _selected_instruments(instruments)
    for instrument, body in sorted(
        payload.items(), key=lambda item: str(item[0] or "").upper()
    ):
        inst = str(instrument or "").upper()
        if not inst or (selected and inst not in selected):
            continue
        if not isinstance(body, dict):
            continue
        bounds = (
            body.get(signal_type) if isinstance(body.get(signal_type), dict) else body
        )
        if isinstance(bounds, dict) and bounds:
            yield inst, bounds


def _post_update(
    target: str,
    *,
    instrument: str,
    bounds: Dict[str, Any],
    timeout: float,
) -> Tuple[bool, str]:
    body = json.dumps({"instrument": instrument, "bounds": bounds}).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        return bool(payload.get("success")), json.dumps(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)


def main() -> int:
    args = _parse_args()
    payload_path = Path(args.payload)
    if not payload_path.exists():
        raise SystemExit(f"Missing payload file: {payload_path}")

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    updates = list(
        iter_updates(
            payload,
            signal_type=args.signal_type,
            instruments=args.instrument,
        )
    )
    if not updates:
        raise SystemExit("No strategy updates selected from payload.")

    failures = 0
    for instrument, bounds in updates:
        ok, detail = _post_update(
            args.target,
            instrument=instrument,
            bounds=bounds,
            timeout=args.timeout,
        )
        status = "OK" if ok else "FAILED"
        print(f"[{status}] {instrument}: {detail}")
        if not ok:
            failures += 1
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
