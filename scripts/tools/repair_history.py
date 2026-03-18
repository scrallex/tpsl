import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys


from scripts.research.data_store import parse, isoformat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("repair")


def repair_instrument_streaming(inst, gran_seconds=5, lookback_days=180):
    p = Path(f"output/market_data/{inst}.jsonl")
    tmp_p = Path(f"output/market_data/{inst}.jsonl.tmp")
    sig_p = Path(f"output/market_data/{inst}.signatures.jsonl")
    gate_p = Path(f"output/market_data/{inst}.gates.jsonl")

    if not p.exists():
        return

    logger.info(f"Repairing {inst} via streaming (lookback={lookback_days} days)...")
    time_step = timedelta(seconds=gran_seconds)

    target_start = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    expected_time = None
    last_close = None
    added = 0
    total = 0
    ignored = 0

    with p.open("r", encoding="utf-8") as f, tmp_p.open("w", encoding="utf-8") as out:
        for line in f:
            if not line.strip():
                continue
            if any(
                y in line
                for y in [
                    "2021-",
                    "2022-",
                    "2023-",
                    "2024-",
                    "2025-01-",
                    "2025-02-",
                    "2025-03-",
                    "2025-04-",
                    "2025-05-",
                    "2025-06-",
                    "2025-07-",
                ]
            ):
                ignored += 1
                continue
            c = json.loads(line)
            t = parse(c["time"])

            if t < target_start:
                ignored += 1
                continue

            if expected_time is None:
                expected_time = t
                last_close = float(c["mid"]["c"])

            while expected_time < t:
                # pad
                out.write(
                    json.dumps(
                        {
                            "time": isoformat(expected_time),
                            "volume": 0,
                            "complete": True,
                            "mid": {
                                "o": str(last_close),
                                "h": str(last_close),
                                "l": str(last_close),
                                "c": str(last_close),
                            },
                        }
                    )
                    + "\n"
                )
                expected_time += time_step
                added += 1

            out.write(json.dumps(c) + "\n")
            total += 1
            last_close = float(c["mid"]["c"])
            expected_time = t + time_step

    logger.info(
        f"Finished {inst}: ignored {ignored} old ticks, kept {total} original, added {added} synthetic ticks padded."
    )
    tmp_p.replace(p)

    for obsolete in [sig_p, gate_p]:
        if obsolete.exists():
            obsolete.unlink()
            logger.info(f"Deleted obsolete cache: {obsolete}")


if __name__ == "__main__":
    for inst in [
        "EUR_USD",
        "GBP_USD",
        "USD_JPY",
        "USD_CAD",
        "USD_CHF",
        "NZD_USD",
        "AUD_USD",
    ]:
        repair_instrument_streaming(inst)
