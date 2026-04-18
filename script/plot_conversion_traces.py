#!/usr/bin/env python3

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def antpol_to_dsig(label):
    pol = label[-1].upper()
    ant = int(label[:-1])
    if pol == "X":
        return 2 * ant
    if pol == "Y":
        return 2 * ant + 1
    raise ValueError("Expected antenna/pol label ending in X or Y, got %s" % label)


def main():
    parser = argparse.ArgumentParser(description="Plot selected conversion-factor traces from a saved NPZ file.")
    parser.add_argument(
        "--npz",
        default="/fast/pipe2026solar/day_night_gain_conversion.npz",
        help="Path to the NPZ file containing night_over_day_ratio.",
    )
    parser.add_argument(
        "--signals",
        nargs="+",
        default=["10X", "10Y", "100X", "100Y", "350X", "350Y"],
        help="Antenna/polarization labels to plot.",
    )
    parser.add_argument(
        "--output",
        default="/fast/pipe2026solar/eq_plots/conversion_factor_selected.png",
        help="Output PNG path.",
    )
    args = parser.parse_args()

    data = np.load(args.npz)
    ratio = data["night_over_day_ratio"]

    plt.figure(figsize=(9, 5))
    for label in args.signals:
        dsig = antpol_to_dsig(label)
        plt.plot(ratio[dsig], label=label, linewidth=1.4)

    plt.xlabel("Channel")
    plt.ylabel("Night / Day gain")
    plt.title("Selected Day-to-Night Conversion Factors")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output, dpi=180)
    plt.close("all")

    print("saved:", output)


if __name__ == "__main__":
    main()
