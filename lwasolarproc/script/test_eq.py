#!/usr/bin/env python3

import argparse
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lwasunproc.calibration import (
    DEFAULT_NIGHT_SETTINGS_PATH,
    DEFAULT_SETTINGS_PATH,
    TRANSMITTED_SLICE,
    load_equalizer_settings,
)


def _plot_single_set(coef, label, output_dir, prefix):
    fig_full, ax_full = plt.subplots(figsize=(9, 4.8))
    for idx in range(coef.shape[0]):
        ax_full.plot(coef[idx], label="coef[%d]" % idx, linewidth=1.2)
    ax_full.set_title("%s Equalizer Coefficients: Full 4096 Channels" % label)
    ax_full.set_xlabel("F-engine Channel")
    ax_full.set_ylabel("Coefficient")
    ax_full.grid(True, alpha=0.3)
    ax_full.legend()
    fig_full.tight_layout()
    fig_full.savefig(output_dir / ("%s_coef_full.png" % prefix), dpi=160)

    fig_tx, ax_tx = plt.subplots(figsize=(9, 4.8))
    tx_coef = coef[:, TRANSMITTED_SLICE]
    for idx in range(tx_coef.shape[0]):
        ax_tx.plot(tx_coef[idx], label="coef[%d]" % idx, linewidth=1.2)
    ax_tx.set_title("%s Equalizer Coefficients: Transmitted 3072 Channels" % label)
    ax_tx.set_xlabel("Transmitted Channel Index")
    ax_tx.set_ylabel("Coefficient")
    ax_tx.grid(True, alpha=0.3)
    ax_tx.legend()
    fig_tx.tight_layout()
    fig_tx.savefig(output_dir / ("%s_coef_transmitted.png" % prefix), dpi=160)

    fig_hist, ax_hist = plt.subplots(figsize=(8, 4.8))
    for idx in range(tx_coef.shape[0]):
        ax_hist.hist(tx_coef[idx], bins=80, alpha=0.4, label="coef[%d]" % idx)
    ax_hist.set_title("%s Equalizer Coefficient Distribution" % label)
    ax_hist.set_xlabel("Coefficient")
    ax_hist.set_ylabel("Count")
    ax_hist.grid(True, alpha=0.3)
    ax_hist.legend()
    fig_hist.tight_layout()
    fig_hist.savefig(output_dir / ("%s_coef_hist.png" % prefix), dpi=160)


def _plot_comparison(day_coef, night_coef, output_dir):
    day_tx = day_coef[:, TRANSMITTED_SLICE]
    night_tx = night_coef[:, TRANSMITTED_SLICE]

    fig_cmp, axes = plt.subplots(7, 1, figsize=(10, 16), sharex=True)
    for idx, ax in enumerate(axes):
        ax.plot(day_tx[idx], label="day", linewidth=1.1)
        ax.plot(night_tx[idx], label="night", linewidth=1.1)
        ax.set_ylabel("coef[%d]" % idx)
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend()
    axes[-1].set_xlabel("Transmitted Channel Index")
    fig_cmp.suptitle("Day vs Night Equalizer Coefficients")
    fig_cmp.tight_layout()
    fig_cmp.savefig(output_dir / "day_night_coef_transmitted_compare.png", dpi=160)

    fig_diff, axes = plt.subplots(7, 1, figsize=(10, 16), sharex=True)
    for idx, ax in enumerate(axes):
        ax.plot(night_tx[idx] - day_tx[idx], linewidth=1.1)
        ax.set_ylabel("dcoef[%d]" % idx)
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel("Transmitted Channel Index")
    fig_diff.suptitle("Night Minus Day Equalizer Coefficients")
    fig_diff.tight_layout()
    fig_diff.savefig(output_dir / "day_night_coef_transmitted_diff.png", dpi=160)


def plot_coefficients(day_settings_path, night_settings_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)

    day_settings = load_equalizer_settings(day_settings_path)
    night_settings = load_equalizer_settings(night_settings_path)

    day_coef = np.asarray(day_settings["coef"], dtype=float)
    night_coef = np.asarray(night_settings["coef"], dtype=float)

    _plot_single_set(day_coef, "Day", output_dir, "day")
    _plot_single_set(night_coef, "Night", output_dir, "night")
    _plot_comparison(day_coef, night_coef, output_dir)

    plt.close("all")


def main():
    parser = argparse.ArgumentParser(description="Plot day and night equalizer coefficients from settings .mat files.")
    parser.add_argument(
        "--day-settings",
        default=str(DEFAULT_SETTINGS_PATH),
        help="Path to the day settings .mat file.",
    )
    parser.add_argument(
        "--night-settings",
        default=str(DEFAULT_NIGHT_SETTINGS_PATH),
        help="Path to the night settings .mat file.",
    )
    parser.add_argument(
        "--outdir",
        default="eq_plots",
        help="Directory where PNG plots will be written.",
    )
    args = parser.parse_args()

    plot_coefficients(
        Path(args.day_settings),
        Path(args.night_settings),
        Path(args.outdir),
    )


if __name__ == "__main__":
    main()
