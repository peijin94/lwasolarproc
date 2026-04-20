from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord, get_body
from astropy.time import Time


def parse_wsclean_coordinates(ra_str: str, dec_str: str) -> SkyCoord:
    """Parse WSClean RA and DEC strings into a SkyCoord."""

    dec_str_astropy = dec_str.replace(".", ":", 2)
    return SkyCoord(ra_str, dec_str_astropy, unit=(u.hourangle, u.deg))


def load_wsclean_sources(filename: str | Path) -> list[dict[str, Any]]:
    """Load a WSClean component source list."""

    sources: list[dict[str, Any]] = []
    with Path(filename).open("r", encoding="utf-8") as handle:
        for line in handle.readlines()[1:]:
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue

            name, source_type, ra_str, dec_str = parts[:4]
            flux = float(parts[4]) if len(parts) > 4 else 0.0
            try:
                coord = parse_wsclean_coordinates(ra_str, dec_str)
            except (ValueError, IndexError):
                continue

            sources.append(
                {
                    "name": name,
                    "type": source_type,
                    "coord": coord,
                    "flux": flux,
                    "ra_deg": coord.ra.deg,
                    "dec_deg": coord.dec.deg,
                }
            )
    return sources


def distance_to_src_list(sourcelist_fname: str | Path, ra_deg: float, dec_deg: float) -> list[dict[str, Any]]:
    """Calculate angular distances from WSClean components to a target coordinate."""

    sourcelist_file = Path(sourcelist_fname)
    if not sourcelist_file.exists():
        raise FileNotFoundError(f"Sources file {sourcelist_file} not found")

    target_coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    result: list[dict[str, Any]] = []
    for source in load_wsclean_sources(sourcelist_file):
        out = dict(source)
        out["distance_deg"] = float(source["coord"].separation(target_coord).deg)
        result.append(out)
    return result


def get_time_mjd(msname: str | Path) -> float:
    """
    Parse observation time from an MS filename.

    Expected stem format starts with ``YYYYMMDD_HHMMSS``.
    """

    stem = Path(msname).name
    if stem.endswith(".ms"):
        stem = stem[:-3]
    parts = stem.split("_")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse observation time from filename: {Path(msname).name}")

    dt = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
    return float(Time(dt, scale="utc").mjd)


def get_sun_ra_dec(time_mjd: float, observatory: str = "OVRO") -> tuple[float, float]:
    """Return apparent solar RA/DEC in degrees for an observation time."""

    obs_time = Time(time_mjd, format="mjd")
    location = EarthLocation.of_site(observatory)
    sun_coord = get_body("sun", obs_time, location)
    return float(sun_coord.ra.to(u.deg).value), float(sun_coord.dec.to(u.deg).value)


def mask_far_sun_sources(
    sourcelist_fname: str | Path,
    fname_out: str | Path,
    ra_deg: float,
    dec_deg: float,
    distance_deg: float = 8.0,
) -> Path:
    """
    Keep only components farther than ``distance_deg`` from the Sun.

    This output list is intended for subtraction, so near-Sun components are
    excluded from the list.
    """

    distances = distance_to_src_list(sourcelist_fname, ra_deg, dec_deg)
    near_sun_sources = {source["name"] for source in distances if source["distance_deg"] <= distance_deg}

    in_path = Path(sourcelist_fname)
    out_path = Path(fname_out)
    with in_path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()

    with out_path.open("w", encoding="utf-8") as handle:
        for index, line in enumerate(lines):
            if index == 0:
                handle.write(line)
                continue
            name = line.split(",", 1)[0]
            if name not in near_sun_sources:
                handle.write(line)

    return out_path
