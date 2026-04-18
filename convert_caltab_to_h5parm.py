#!/usr/bin/env python3
"""
Convert CASA bandpass caltables into H5Parm files that DP3 can read.

This script is tailored to the local LWA caltables in `./caltab`, which store
one complex gain solution per antenna, channel, and polarization in the
`CPARAM` column.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from casacore.tables import table
from losoto.h5parm import h5parm
import tables


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "caltab"
DEFAULT_OUTPUT_DIR = ROOT / "caltab_h5parm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert CASA caltables to DP3-compatible H5Parm files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--pattern", default="*.bcal")
    parser.add_argument("--solset", default="sol000")
    parser.add_argument("--amplitude-soltab", default="amplitude000")
    parser.add_argument("--phase-soltab", default="phase000")
    parser.add_argument("--reference-source", default="pointing")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def list_caltables(input_dir: Path, pattern: str) -> list[Path]:
    if not input_dir.is_dir():
        raise SystemExit(f"Input caltable directory not found: {input_dir}")
    caltables = sorted(path.resolve() for path in input_dir.glob(pattern) if path.is_dir())
    if not caltables:
        raise SystemExit(f"No caltables matched {pattern!r} in {input_dir}")
    return caltables


def read_caltable_metadata(caltable: Path, reference_source: str) -> dict[str, object]:
    with table(str(caltable), readonly=True) as main:
        colnames = set(main.colnames())
        if "CPARAM" not in colnames:
            raise SystemExit(f"{caltable} does not have a CPARAM column")
        if "ANTENNA1" not in colnames:
            raise SystemExit(f"{caltable} does not have an ANTENNA1 column")

        cparam = np.asarray(main.getcol("CPARAM"), dtype=np.complex64)
        flags = np.asarray(main.getcol("FLAG"), dtype=bool)
        ant1 = np.asarray(main.getcol("ANTENNA1"), dtype=np.int32)
        times = np.asarray(main.getcol("TIME"), dtype=np.float64)
        spw_ids = np.asarray(main.getcol("SPECTRAL_WINDOW_ID"), dtype=np.int32)

    if cparam.ndim != 3:
        raise SystemExit(f"Unexpected CPARAM shape for {caltable}: {cparam.shape}")
    if len(np.unique(spw_ids)) != 1:
        raise SystemExit(f"{caltable} has multiple SPECTRAL_WINDOW_ID values; not supported yet.")

    unique_times = np.unique(times)
    if unique_times.size != 1:
        raise SystemExit(f"{caltable} has {unique_times.size} distinct times; not supported yet.")

    with table(str(caltable / "ANTENNA"), readonly=True) as ant_tab:
        antenna_names = [str(name) for name in ant_tab.getcol("NAME")]
        antenna_positions = np.asarray(ant_tab.getcol("POSITION"), dtype=np.float32)
        if antenna_positions.ndim == 2 and antenna_positions.shape[0] == 3:
            antenna_positions = antenna_positions.T.copy()
        elif antenna_positions.ndim == 2 and antenna_positions.shape[1] == 3:
            antenna_positions = antenna_positions.copy()
        else:
            antenna_positions = antenna_positions.reshape(-1, 3).astype(np.float32)

    with table(str(caltable / "SPECTRAL_WINDOW"), readonly=True) as spw_tab:
        chan_freq = np.asarray(spw_tab.getcol("CHAN_FREQ"), dtype=np.float64)
        if chan_freq.ndim == 2:
            chan_freq = chan_freq[0]
        chan_freq = chan_freq.reshape(-1)

    with table(str(caltable / "FIELD"), readonly=True) as field_tab:
        phase_dir = np.asarray(field_tab.getcol("PHASE_DIR"), dtype=np.float32)
        if phase_dir.ndim == 3:
            source_dir = phase_dir[0, 0, :]
        elif phase_dir.ndim == 2:
            source_dir = phase_dir[0, :]
        else:
            source_dir = phase_dir.reshape(-1)[:2]

    n_antennas = len(antenna_names)
    n_freq = chan_freq.size
    n_pol = cparam.shape[2]
    if n_pol != 2:
        raise SystemExit(f"{caltable} has {n_pol} pols in CPARAM; expected 2 for XX/YY.")

    gains = np.ones((1, n_antennas, n_freq, n_pol), dtype=np.complex128)
    weights = np.ones((1, n_antennas, n_freq, n_pol), dtype=np.float32)

    for row_index, antenna_index in enumerate(ant1):
        gains[0, antenna_index, :, :] = cparam[row_index, :, :]
        weights[0, antenna_index, :, :] = (~flags[row_index, :, :]).astype(np.float32)

    amplitude = np.abs(gains)
    phase = np.angle(gains)

    return {
        "antenna_names": antenna_names,
        "antenna_positions": antenna_positions,
        "times": unique_times.astype(np.float64),
        "freqs": chan_freq.astype(np.float64),
        "pols": np.asarray(["XX", "YY"]),
        "source_name": reference_source,
        "source_dir": np.asarray(source_dir, dtype=np.float32),
        "amplitude": amplitude.astype(np.float64),
        "phase": phase.astype(np.float64),
        "weights": weights.astype(np.float32),
    }


def write_h5parm(
    output_path: Path,
    metadata: dict[str, object],
    *,
    solset_name: str,
    amplitude_soltab: str,
    phase_soltab: str,
    overwrite: bool,
) -> None:
    if output_path.exists():
        if not overwrite:
            raise SystemExit(f"Output already exists: {output_path}. Use --overwrite to replace it.")
        output_path.unlink()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with h5parm(str(output_path), readonly=False) as h5:
        solset = h5.makeSolset(solset_name, addTables=False)

        antenna_descriptor = np.dtype([("name", "S16"), ("position", np.float32, 3)])
        antenna_table = h5.H.create_table(
            solset.obj,
            "antenna",
            antenna_descriptor,
            title="Antenna names and positions",
            expectedrows=len(metadata["antenna_names"]),
        )
        antenna_rows = np.empty(len(metadata["antenna_names"]), dtype=antenna_descriptor)
        antenna_rows["name"] = [name.encode("ascii", "ignore")[:16] for name in metadata["antenna_names"]]
        antenna_rows["position"] = metadata["antenna_positions"]
        antenna_table.append(antenna_rows)
        antenna_table.flush()

        source_descriptor = np.dtype([("name", "S128"), ("dir", np.float32, 2)])
        source_table = h5.H.create_table(
            solset.obj,
            "source",
            source_descriptor,
            title="Source names and directions",
            expectedrows=1,
        )
        source_rows = np.empty(1, dtype=source_descriptor)
        source_rows["name"][0] = str(metadata["source_name"]).encode("ascii", "ignore")[:128]
        source_rows["dir"][0] = metadata["source_dir"]
        source_table.append(source_rows)
        source_table.flush()

        axes_names = ["time", "ant", "freq", "pol"]
        axes_vals = [
            metadata["times"],
            np.asarray(metadata["antenna_names"]),
            metadata["freqs"],
            metadata["pols"],
        ]

        solset.makeSoltab(
            soltype="amplitude",
            soltabName=amplitude_soltab,
            axesNames=axes_names,
            axesVals=axes_vals,
            vals=metadata["amplitude"],
            weights=metadata["weights"],
            parmdbType="Gain",
            weightDtype="f32",
        )
        solset.makeSoltab(
            soltype="phase",
            soltabName=phase_soltab,
            axesNames=axes_names,
            axesVals=axes_vals,
            vals=metadata["phase"],
            weights=metadata["weights"],
            parmdbType="Gain",
            weightDtype="f32",
        )


def convert_one(
    caltable: Path,
    output_dir: Path,
    *,
    solset_name: str,
    amplitude_soltab: str,
    phase_soltab: str,
    reference_source: str,
    overwrite: bool,
    dry_run: bool,
) -> Path:
    output_path = output_dir / f"{caltable.stem}.h5"
    print(f"[convert] {caltable.name} -> {output_path}")
    if dry_run:
        return output_path

    metadata = read_caltable_metadata(caltable, reference_source)
    write_h5parm(
        output_path,
        metadata,
        solset_name=solset_name,
        amplitude_soltab=amplitude_soltab,
        phase_soltab=phase_soltab,
        overwrite=overwrite,
    )
    return output_path


def main() -> int:
    args = parse_args()
    args.input_dir = args.input_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    caltables = list_caltables(args.input_dir, args.pattern)

    print("Selected caltables:")
    for caltable in caltables:
        print(f"  {caltable.name}")

    outputs = []
    for caltable in caltables:
        outputs.append(
            convert_one(
                caltable,
                args.output_dir,
                solset_name=args.solset,
                amplitude_soltab=args.amplitude_soltab,
                phase_soltab=args.phase_soltab,
                reference_source=args.reference_source,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        )

    print("")
    print("Generated H5Parm files:")
    for output in outputs:
        print(f"  {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
