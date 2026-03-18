import json
import logging
import sys
import argparse
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Ensure root is in path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("visualize_profit_smile")


def load_results(json_path: Path) -> pd.DataFrame:
    """Load optimization results from JSON file."""
    if not json_path.exists():
        logger.error(f"File not found: {json_path}")
        return pd.DataFrame()

    data = []
    with open(json_path, "r") as f:
        try:
            raw_data = json.load(f)
            # Handle list of lists or list of dicts
            if isinstance(raw_data, list):
                for entry in raw_data:
                    if isinstance(entry, list) and len(entry) == 2:
                        cfg, res = entry
                    elif (
                        isinstance(entry, dict)
                        and "config" in entry
                        and "result" in entry
                    ):
                        cfg = entry["config"]
                        res = entry["result"]
                    else:
                        continue

                    flat = {}
                    # Config
                    flat["hold_minutes"] = int(cfg.get("hold_minutes", 0))
                    flat["min_repetitions"] = int(cfg.get("min_repetitions", 0))
                    flat["hazard_multiplier"] = float(cfg.get("hazard_multiplier", 1.0))
                    flat["st_percentile"] = float(cfg.get("st_percentile", 0.0))

                    # Result
                    metrics = res.get("metrics", {})
                    flat["sharpe"] = float(metrics.get("sharpe", -999.0))
                    flat["pf"] = float(metrics.get("profit_factor", 0.0))
                    flat["trades"] = int(metrics.get("trades", 0))
                    flat["return"] = float(
                        metrics.get("return_pct", 0.0)
                        or metrics.get("total_return_pct", 0.0)
                    )

                    if flat["trades"] > 10:  # Filter out noise
                        data.append(flat)

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON: {json_path}")
            return pd.DataFrame()

    return pd.DataFrame(data)


def plot_smile(df: pd.DataFrame, output_path: Path, title: str):
    """Generate Profit Smile Plot."""
    if df.empty:
        logger.warning("No data to plot.")
        return

    plt.figure(figsize=(12, 8))
    sns.set_style("darkgrid")

    # Scatter plot: X=Hold, Y=Sharpe, Hue=Repetitions
    scatter = sns.scatterplot(
        data=df,
        x="hold_minutes",
        y="sharpe",
        hue="min_repetitions",
        palette="viridis",
        s=100,
        alpha=0.8,
    )

    # Add a smoothed trend line if enough data
    try:
        sns.regplot(
            data=df,
            x="hold_minutes",
            y="sharpe",
            scatter=False,
            color="red",
            lowess=True,
            line_kws={"linestyle": "--"},
        )
    except:
        pass

    plt.title(f"Profit Smile: {title}", fontsize=16)
    plt.xlabel("Hold Time (Minutes)", fontsize=12)
    plt.ylabel("Sharpe Ratio", fontsize=12)
    plt.axhline(0, color="black", linestyle="-", linewidth=1)
    plt.axhline(1.0, color="green", linestyle=":", linewidth=1, label="Profitable")

    # Annotate top point
    top = df.iloc[df["sharpe"].argmax()]
    plt.annotate(
        f"Top: {top['sharpe']:.2f}\n(Hold={top['hold_minutes']}, Reps={top['min_repetitions']})",
        xy=(top["hold_minutes"], top["sharpe"]),
        xytext=(0, 10),
        textcoords="offset points",
        ha="center",
        arrowprops=dict(facecolor="black", shrink=0.05),
    )

    plt.tight_layout()
    plt.savefig(output_path)
    logger.info(f"Saved plot to {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_file", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("output/research/profit_smile.png")
    )
    args = parser.parse_args()

    df = load_results(args.input_file)
    plot_smile(df, args.output, args.input_file.stem)


if __name__ == "__main__":
    main()
