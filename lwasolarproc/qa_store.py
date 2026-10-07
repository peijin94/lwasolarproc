"""A-Team flux + leakage QA store (GitHub issue #2).

Worker side measures calibrator flux from the full-sky WSClean source list
(``measure_band_qa`` — pure astropy/numpy, no CASA); the manager process is
the only writer to the SQLite database (``record_run``), so concurrent
workers never touch the DB file. Keep the database on **local disk**
(SQLite locking is fragile on Lustre/NFS).

Schema:
  qa_runs    (id, timestamp, created_utc, code_version, caltable, n_bands)
  qa_sources (id, run_id, freq_mhz, source, measured_jy, expected_jy,
              ratio, n_components, beam, leakage_iv)
  qa_band    (id, run_id, freq_mhz, flagged_frac, n_vis_total,
              n_vis_flagged, n_bad_ant, ant_list)
``ant_list`` is JSON text containing zero-based MS antenna indices; NULL
means the list was not recorded, while [] means no fully flagged antennas.
``leakage_iv`` stays NULL in the standard path (no full-sky V image); it is
recorded when V data are available.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from .beammodel import p178
from .source_list import load_wsclean_sources

SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_runs (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    code_version TEXT,
    caltable TEXT,
    n_bands INTEGER
);
CREATE INDEX IF NOT EXISTS idx_qa_runs_timestamp ON qa_runs (timestamp);
CREATE TABLE IF NOT EXISTS qa_sources (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES qa_runs(id),
    freq_mhz INTEGER NOT NULL,
    source TEXT NOT NULL,
    measured_jy REAL,
    expected_jy REAL,
    ratio REAL,
    n_components INTEGER,
    beam REAL,
    leakage_iv REAL,
    caltable TEXT,
    UNIQUE(run_id, freq_mhz, source)
);
CREATE INDEX IF NOT EXISTS idx_qa_sources_lookup
    ON qa_sources (source, freq_mhz);
CREATE TABLE IF NOT EXISTS qa_band (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES qa_runs(id),
    freq_mhz INTEGER NOT NULL,
    flagged_frac REAL,
    n_vis_total INTEGER,
    n_vis_flagged INTEGER,
    n_bad_ant INTEGER,
    ant_list TEXT,
    UNIQUE(run_id, freq_mhz)
);
"""


# J2000 positions + approximate intrinsic power-law models.
# Models are Baars-scale anchored and good to ~20%: they serve trending and
# order-of-magnitude alarms (warn threshold 3x), not absolute fluxing.
# Cas A fades ~0.7%/yr; reference epoch 2000.0.
ATEAM_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "name": "CasA",
        "ra_deg": 350.866875,
        "dec_deg": 58.811667,
        "pivot_jy": 18500.0,
        "pivot_mhz": 74.0,
        "alpha": -0.77,
        "secular_per_year": -0.007,
        "ref_year": 2000.0,
    },
    {
        "name": "CygA",
        "ra_deg": 299.868152,
        "dec_deg": 40.733916,
        "pivot_jy": 17000.0,
        "pivot_mhz": 74.0,
        "alpha": -0.70,
        "secular_per_year": 0.0,
        "ref_year": 2000.0,
    },
    {
        "name": "TauA",
        "ra_deg": 83.633083,
        "dec_deg": 22.014500,
        "pivot_jy": 1500.0,
        "pivot_mhz": 74.0,
        "alpha": -0.30,
        "secular_per_year": 0.0,
        "ref_year": 2000.0,
    },
    {
        "name": "VirA",
        "ra_deg": 187.705930,
        "dec_deg": 12.391123,
        "pivot_jy": 1200.0,
        "pivot_mhz": 74.0,
        "alpha": -0.85,
        "secular_per_year": 0.0,
        "ref_year": 2000.0,
    },
)

MATCH_RADIUS_DEG = 1.5
# Sources below this elevation are skipped entirely (beam too low, models
# unreliable, often rising/setting mid-observation).
MIN_ELEVATION_DEG = 30.0


def intrinsic_flux_jy(source: Mapping[str, Any], freq_mhz: float,
                      year: float) -> float:
    """Approximate intrinsic flux density (see caveats above)."""
    secular = 1.0 + float(source.get("secular_per_year", 0.0)) * (
        year - float(source.get("ref_year", 2000.0)))
    return float(source["pivot_jy"]) * max(secular, 0.1) * (
        freq_mhz / float(source["pivot_mhz"])) ** float(source["alpha"])


def source_altaz(ra_deg: float, dec_deg: float, time_mjd: float,
                 observatory: str = "OVRO") -> tuple[float, float]:
    """(azimuth, elevation) in radians for a sky position and time."""
    location = EarthLocation.of_site(observatory)
    altaz = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg).transform_to(
        AltAz(obstime=Time(time_mjd, format="mjd"), location=location))
    return float(altaz.az.to(u.rad).value), float(altaz.alt.to(u.rad).value)


def expected_apparent_jy(source: Mapping[str, Any], freq_mhz: float,
                         time_mjd: float,
                         observatory: str = "OVRO") -> tuple[float | None, float | None]:
    """    (expected apparent Jy, beam power); (None, None) below the horizon."""
    year = float(Time(time_mjd, format="mjd").decimalyear)
    az, el = source_altaz(source["ra_deg"], source["dec_deg"], time_mjd,
                          observatory)
    if np.degrees(el) < MIN_ELEVATION_DEG:
        return None, None
    beam = float(p178(freq_mhz * 1e6, az, el))
    beam = max(min(beam, 1.0), 0.0)
    return intrinsic_flux_jy(source, freq_mhz, year) * beam, beam


def measure_band_qa(source_list_path: str | Path, freq_mhz: int,
                    time_mjd: float | None,
                    match_radius_deg: float = MATCH_RADIUS_DEG,
                    observatory: str = "OVRO") -> list[dict[str, Any]]:
    """Sum cleaned component flux near each A-Team source.

    Returns one row dict per source (keys match ``qa_sources`` columns plus
    ``timestamp``-level fields filled by the caller).
    """
    components = load_wsclean_sources(source_list_path)
    rows: list[dict[str, Any]] = []
    for source in ATEAM_SOURCES:
        if time_mjd is not None:
            _az, _el = source_altaz(source["ra_deg"], source["dec_deg"],
                                    time_mjd, observatory)
            if float(np.degrees(_el)) < MIN_ELEVATION_DEG:
                continue  # too low: no check at all
        target = SkyCoord(ra=source["ra_deg"] * u.deg,
                          dec=source["dec_deg"] * u.deg)
        nearby = [c for c in components
                  if float(c["coord"].separation(target).deg) <= match_radius_deg]
        measured = float(sum(c["flux"] for c in nearby)) if nearby else 0.0
        expected, beam = (None, None)
        if time_mjd is not None:
            expected, beam = expected_apparent_jy(source, float(freq_mhz),
                                                  time_mjd, observatory)
        ratio = (measured / expected) if expected else None
        rows.append({
            "freq_mhz": int(freq_mhz),
            "source": source["name"],
            "measured_jy": measured,
            "expected_jy": expected,
            "ratio": ratio,
            "n_components": len(nearby),
            "beam": beam,
            "leakage_iv": None,
        })
    return rows


def measure_flagged_fraction(ms_path: str | Path) -> dict[str, Any]:
    """Flagged sample fraction and fully flagged MS antenna indices.

    An antenna is bad iff *every* sample on *every* baseline it participates
    in is flagged. Needs python-casacore (worker/full-env only; imported
    lazily so the module stays importable without it, e.g. in the container).
    """
    from casacore.tables import table

    with table(str(ms_path), readonly=True, ack=False) as tb:
        flags = tb.getcol("FLAG")
        ant1 = tb.getcol("ANTENNA1")
        ant2 = tb.getcol("ANTENNA2")
    total = int(flags.size)
    flagged = int(np.count_nonzero(flags))
    row_all_flagged = np.all(flags, axis=(1, 2))
    n_ant = int(max(int(ant1.max(initial=-1)), int(ant2.max(initial=-1))) + 1)
    rows_per_ant = np.bincount(np.concatenate([ant1, ant2]),
                               minlength=n_ant)
    flagged_per_ant = np.bincount(np.concatenate(
        [ant1[row_all_flagged], ant2[row_all_flagged]]), minlength=n_ant)
    ant_list = np.flatnonzero(
        (rows_per_ant > 0) & (flagged_per_ant == rows_per_ant)).tolist()
    return {"n_vis_total": total, "n_vis_flagged": flagged,
            "flagged_frac": (flagged / total) if total else None,
            "n_bad_ant": len(ant_list), "ant_list": ant_list}


def init_db(path: str | Path) -> sqlite3.Connection:
    """Create (or open) the QA database; WAL mode for concurrent readers."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    # lightweight migrations for DBs created by older versions
    existing = {row[1] for row in
                conn.execute("PRAGMA table_info(qa_band)").fetchall()}
    if "n_bad_ant" not in existing:
        conn.execute("ALTER TABLE qa_band ADD COLUMN n_bad_ant INTEGER;")
    if "ant_list" not in existing:
        conn.execute("ALTER TABLE qa_band ADD COLUMN ant_list TEXT;")
    conn.commit()
    return conn


def record_run(conn: sqlite3.Connection, timestamp: str,
               rows: Sequence[Mapping[str, Any]],
               code_version: str | None = None,
               caltable: str | None = None,
               band_rows: Sequence[Mapping[str, Any]] | None = None) -> int:
    """Insert one run + its per-band/per-source rows. Returns run id.

    Natural key is (timestamp, freq_mhz, source): re-recording the same
    timestamp replaces its rows instead of duplicating them.
    """
    created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    band_set = {r["freq_mhz"] for r in rows} | {
        b["freq_mhz"] for b in (band_rows or [])}
    cur = conn.execute("SELECT id FROM qa_runs WHERE timestamp = ?", (timestamp,))
    hit = cur.fetchone()
    if hit is not None:
        run_id = int(hit[0])
        conn.execute("UPDATE qa_runs SET created_utc = ?, code_version = ?,"
                     " caltable = ?, n_bands = ? WHERE id = ?",
                     (created, code_version, caltable,
                      len(band_set), run_id))
        conn.execute("DELETE FROM qa_sources WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM qa_band WHERE run_id = ?", (run_id,))
    else:
        cur = conn.execute(
            "INSERT INTO qa_runs (timestamp, created_utc, code_version, caltable, n_bands)"
            " VALUES (?, ?, ?, ?, ?)",
            (timestamp, created, code_version,
             caltable, len(band_set)),
        )
        run_id = int(cur.lastrowid)
    cals = sorted({str(r.get("caltable")) for r in rows if r.get("caltable")})
    if cals:
        conn.execute("UPDATE qa_runs SET caltable = ? WHERE id = ?",
                     (",".join(cals), run_id))
    conn.executemany(
        "INSERT INTO qa_sources (run_id, freq_mhz, source, measured_jy,"
        " expected_jy, ratio, n_components, beam, leakage_iv, caltable)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, r["freq_mhz"], r["source"], r.get("measured_jy"),
          r.get("expected_jy"), r.get("ratio"), r.get("n_components"),
          r.get("beam"), r.get("leakage_iv"), r.get("caltable")) for r in rows],
    )
    if band_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO qa_band (run_id, freq_mhz, flagged_frac,"
            " n_vis_total, n_vis_flagged, n_bad_ant, ant_list)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, b["freq_mhz"], b.get("flagged_frac"),
              b.get("n_vis_total"), b.get("n_vis_flagged"),
              b.get("n_bad_ant"),
              json.dumps(b["ant_list"]) if b.get("ant_list") is not None else None)
             for b in band_rows],
        )
    conn.commit()
    return run_id


def recent(conn: sqlite3.Connection, source: str | None = None,
           freq_mhz: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Latest QA rows, optionally filtered. Newest first."""
    query = ("SELECT q.timestamp, s.freq_mhz, s.source, s.measured_jy,"
             " s.expected_jy, s.ratio, s.n_components, s.beam, s.leakage_iv,"
             " s.caltable FROM qa_sources s JOIN qa_runs q ON s.run_id = q.id")
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append("s.source = ?")
        params.append(source)
    if freq_mhz is not None:
        clauses.append("s.freq_mhz = ?")
        params.append(freq_mhz)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY q.timestamp DESC, s.freq_mhz LIMIT ?"
    params.append(limit)
    cols = ("timestamp", "freq_mhz", "source", "measured_jy", "expected_jy",
            "ratio", "n_components", "beam", "leakage_iv", "caltable")
    return [dict(zip(cols, row)) for row in conn.execute(query, params)]


def recent_bands(conn: sqlite3.Connection,
                 freq_mhz: int | None = None,
                 limit: int = 50) -> list[dict[str, Any]]:
    """Latest per-band flagging rows, optionally filtered. Newest first."""
    query = ("SELECT q.timestamp, b.freq_mhz, b.flagged_frac,"
             " b.n_vis_total, b.n_vis_flagged, b.n_bad_ant, b.ant_list"
             " FROM qa_band b JOIN qa_runs q ON b.run_id = q.id")
    params: list[Any] = []
    if freq_mhz is not None:
        query += " WHERE b.freq_mhz = ?"
        params.append(freq_mhz)
    query += " ORDER BY q.timestamp DESC, b.freq_mhz LIMIT ?"
    params.append(limit)
    cols = ("timestamp", "freq_mhz", "flagged_frac", "n_vis_total",
            "n_vis_flagged", "n_bad_ant", "ant_list")
    rows = [dict(zip(cols, row)) for row in conn.execute(query, params)]
    for row in rows:
        if row["ant_list"] is not None:
            row["ant_list"] = json.loads(row["ant_list"])
    return rows


def code_version() -> str:
    """Best-effort repo SHA of this package (for the qa_runs record)."""
    try:
        import subprocess

        repo = Path(__file__).resolve().parents[1]
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        sha = out.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return sha + ("-dirty" if dirty else "") if sha else "unknown"
    except Exception:
        return "unknown"
