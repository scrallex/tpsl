import json
import dateutil.parser

# "2026-02-23T21:27:55+00:00"
entry_time_str = "2026-02-24T05:14:35+00:00"
exit_time_str = "2026-02-27T19:43:35+00:00"

entry_dt = dateutil.parser.isoparse(entry_time_str)
exit_dt = dateutil.parser.isoparse(exit_time_str)

candles = []
with open("output/market_data/EUR_USD.jsonl", "r") as f:
    for line in f:
        g = json.loads(line)
        if "mid" in g and "time" in g:
            ts = dateutil.parser.isoparse(g["time"])
            if entry_dt <= ts <= exit_dt:
                candles.append(g["mid"])

if not candles:
    print("No candles found!")
else:
    entry_p = float(candles[0]["c"])
    max_h = max(float(c["h"]) for c in candles)
    min_l = min(float(c["l"]) for c in candles)

    print(f"Entry Price: {entry_p}")
    print(
        f"Max High (Adverse for SHORT): {max_h} (+{((max_h - entry_p) / entry_p)*10000:.1f} bps)  [SL req = +49.2 bps]"
    )
    print(
        f"Min Low (Favorable for SHORT): {min_l} ({((min_l - entry_p) / entry_p)*10000:.1f} bps) [TP req = -74.3 bps]"
    )
