#!/usr/bin/env python3

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lwasunproc.calibration import DEFAULT_SETTINGS_PATH, load_equalizer_settings


def dsig_to_antpol(dsig):
    ant = dsig // 2
    pol = "X" if dsig % 2 == 0 else "Y"
    return ant, pol


def main():
    parser = argparse.ArgumentParser(description="Report equalizer group membership from an LWA settings file.")
    parser.add_argument(
        "--settings",
        default=str(DEFAULT_SETTINGS_PATH),
        help="Path to the settings .mat file.",
    )
    args = parser.parse_args()

    settings = load_equalizer_settings(args.settings)

    for idx in range(7):
        key = "eq%d" % idx
        dsigs = [int(x) for x in settings.get(key, [])]
        antpol = ["%d%s" % dsig_to_antpol(dsig) for dsig in dsigs]

        print("%s uses coef[%d]" % (key, idx))
        print("count:", len(dsigs))
        print("digital signals:")
        print(" ".join(str(x) for x in dsigs))
        print("antenna/pol:")
        print(" ".join(antpol))
        print()


if __name__ == "__main__":
    main()
