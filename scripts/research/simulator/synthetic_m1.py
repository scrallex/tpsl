import json
from collections import deque
from pathlib import Path


def stream_synthetic_m1(s5_jsonl_path: Path):
    """
    Generator that parses an S5 JSONL file and yields continuous, synthetic M1 candles.
    A Synthetic M1 is a rolling window of exactly 12 S5 candles (60 seconds).

    Yields:
        dict: The synthetic M1 candle conforming to the OANDA base JSON structure.
    """
    window = deque(maxlen=12)

    with s5_jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            c_dict = json.loads(line)

            # Basic validation
            if "mid" not in c_dict or "time" not in c_dict:
                continue

            window.append(c_dict)

            if len(window) == 12:
                # Construct Synthetic M1
                time_anchor = window[-1][
                    "time"
                ]  # Time marks the close of the 60s window

                mid_o = float(
                    window[0]["mid"]["o"]
                )  # Open is the open of the T-11 candle
                mid_c = float(
                    window[-1]["mid"]["c"]
                )  # Close is the close of the T-0 candle

                # High/Low are absolute extremities within the rolling minute
                mid_h = max(float(x["mid"]["h"]) for x in window)
                mid_l = max(float(x["mid"]["l"]) for x in window)

                volume = sum(int(x.get("volume", 0)) for x in window)

                synthetic_candle = {
                    "time": time_anchor,
                    "volume": volume,
                    "mid": {
                        "o": f"{mid_o:.5f}",
                        "h": f"{mid_h:.5f}",
                        "l": f"{mid_l:.5f}",
                        "c": f"{mid_c:.5f}",
                    },
                    "complete": True,
                }

                yield synthetic_candle
