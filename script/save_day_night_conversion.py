#!/usr/bin/env python3

import argparse
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lwasunproc.calibration import (
    DEFAULT_NIGHT_SETTINGS_PATH,
    DEFAULT_SETTINGS_PATH,
    build_day_night_conversion_table,
)


def main():
    parser = argparse.ArgumentParser(
        description="Save full 704x3072 day/night equalizer gain conversion tables to an NPZ file."
    )
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
        "--output",
        default="day_night_gain_conversion.npz",
        help="Output NPZ filename.",
    )
    args = parser.parse_args()

    day_gain, night_gain, ratio = build_day_night_conversion_table(
        day_settings_path=Path(args.day_settings),
        night_settings_path=Path(args.night_settings),
    )

    np.savez(
        args.output,
        day_gain=day_gain,
        night_gain=night_gain,
        night_over_day_ratio=ratio,
    )

    print("saved:", args.output)
    print("day_gain shape:", day_gain.shape)
    print("night_gain shape:", night_gain.shape)
    print("night_over_day_ratio shape:", ratio.shape)


if __name__ == "__main__":
    main()
