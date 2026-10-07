"""Read-only QA queries and a local, short-lived fine-channel image cache."""

from __future__ import annotations

import fcntl
import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

IMAGE_FREQUENCIES = (30, 45, 60, 75)
CACHE_SECONDS = 600
DEFAULT_QA_DB = Path("/fast/rtpipe/qa/qa.db")
DEFAULT_IMAGE_DB = Path("/fast/rtpipe/qa/images.db")

IMAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    timestamp TEXT NOT NULL,
    target_mhz INTEGER NOT NULL,
    freq_mhz REAL NOT NULL,
    date_obs TEXT,
    fits BLOB NOT NULL,
    npz BLOB NOT NULL,
    PRIMARY KEY (timestamp, target_mhz)
);
"""


def parse_timestamp(value: str) -> datetime:
    """Accept UTC YYYYMMDDTHHMMSS, or the pipeline's YYYYMMDD_HHMMSS."""
    value = value.removesuffix("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d_%H%M%S"):
        try:
            result = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            if result.strftime(fmt) == value:
                return result
        except ValueError:
            pass
    raise ValueError("timestamp must be YYYYMMDDTHHMMSS (UTC), or newest")


def public_timestamp(value: str) -> str:
    return parse_timestamp(value).strftime("%Y%m%dT%H%M%S")


def cutoff_timestamp(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(seconds=CACHE_SECONDS)).strftime("%Y%m%d_%H%M%S")


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def nearest_row(conn: sqlite3.Connection, query: str, params: tuple,
                requested: str | None) -> sqlite3.Row | None:
    """Find adjacent timestamps; break an equal-distance tie toward newer."""
    if requested is None or requested == "newest":
        return conn.execute(query + " ORDER BY timestamp DESC, id DESC LIMIT 1", params).fetchone()
    dt = parse_timestamp(requested)
    stamp = dt.strftime("%Y%m%d_%H%M%S")
    candidates = [conn.execute(query + f" AND timestamp {op} ? ORDER BY timestamp {order}, id DESC LIMIT 1",
                               (*params, stamp)).fetchone()
                  for op, order in (("<=", "DESC"), (">=", "ASC"))]
    return min((r for r in candidates if r is not None),
               key=lambda r: (abs((parse_timestamp(r["timestamp"]) - dt).total_seconds()),
                              -parse_timestamp(r["timestamp"]).timestamp()), default=None)


def response_metadata(timestamp: str, requested: str | None) -> dict[str, Any]:
    delta = None
    if requested is not None and requested != "newest":
        delta = abs((parse_timestamp(timestamp) - parse_timestamp(requested)).total_seconds())
    return {"timestamp": public_timestamp(timestamp), "requested": requested or "newest",
            "delta_seconds": delta}


def query_qa(path: Path, kind: str, requested: str | None = None) -> dict[str, Any] | None:
    table = {"flagging": "qa_band", "flux": "qa_sources"}[kind]
    with closing(open_readonly(path)) as conn:
        run = nearest_row(conn, f"SELECT id, timestamp FROM qa_runs WHERE EXISTS "
                          f"(SELECT 1 FROM {table} WHERE run_id = qa_runs.id)", (), requested)
        if run is None:
            return None
        order = "freq_mhz" if kind == "flagging" else "freq_mhz, source"
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order}", (run["id"],))]
    for row in rows:
        row.pop("id")
        row.pop("run_id")
        if kind == "flagging":
            row["bad_ant"] = row.get("n_bad_ant")
            row["flagging_ratio"] = row["flagged_frac"]
            row["ant_list"] = json.loads(row["ant_list"]) if row.get("ant_list") is not None else None
    return {**response_metadata(run["timestamp"], requested), "rows": rows}


def image_npz(data: Any, header: Any, target_mhz: int, freq_mhz: float) -> bytes:
    import numpy as np

    plane = np.squeeze(data)
    if plane.ndim != 2:
        raise ValueError(f"Expected a single 2D fine-channel image, got {plane.shape}")
    buffer = io.BytesIO()
    np.savez_compressed(buffer, image=plane, target_mhz=target_mhz, freq_mhz=freq_mhz,
                        date_obs=header.get("DATE-OBS", ""), bunit=header.get("BUNIT", ""),
                        header=header.tostring())
    return buffer.getvalue()


def cache_fch_images(path: Path, timestamp: str, helio_dir: Path,
                     *, now: datetime | None = None) -> int:
    """Cache level-1 helioprojective planes before a worker removes its files.

    A local file lock serializes worker writes, including first-time WAL setup;
    all four planes and expiry are committed together. Retention uses
    observation UTC, not arrival time.
    """
    from astropy import units as u
    from astropy.io import fits

    stamp = parse_timestamp(timestamp).strftime("%Y%m%d_%H%M%S")
    cutoff = cutoff_timestamp(now)
    rows = []
    if stamp >= cutoff:
        candidates = []
        for source in sorted(helio_dir.glob("*.helio.fits")):
            header = fits.getheader(source)
            freq = (float(header["CRVAL3"]) * u.Unit(header.get("CUNIT3", "Hz"))).to_value(u.MHz)
            if freq > 0:
                candidates.append((freq, source))
        if candidates:
            for target in IMAGE_FREQUENCIES:
                freq, source = min(candidates, key=lambda item: abs(item[0] - target))
                data, header = fits.getdata(source, header=True)
                rows.append((stamp, target, freq, header.get("DATE-OBS"), source.read_bytes(),
                             image_npz(data, header, target, freq)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(sqlite3.connect(path, timeout=30)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(IMAGE_SCHEMA)
            with conn:
                conn.execute("DELETE FROM images WHERE timestamp < ?", (cutoff,))
                conn.executemany("INSERT OR REPLACE INTO images VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def query_images(path: Path, requested: str | None = None, *, target_mhz: int | None = None,
                 fmt: str | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    if target_mhz is not None and (target_mhz not in IMAGE_FREQUENCIES or fmt not in {"fits", "npz"}):
        raise ValueError("Unsupported image frequency or format")
    with closing(open_readonly(path)) as conn:
        frame = nearest_row(conn, "SELECT DISTINCT timestamp, 0 AS id FROM images WHERE timestamp >= ?",
                            (cutoff_timestamp(now),), requested)
        if frame is None:
            return None
        if target_mhz is not None:
            row = conn.execute(f"SELECT freq_mhz, date_obs, {fmt} AS data FROM images "
                               "WHERE timestamp = ? AND target_mhz = ?", (frame["timestamp"], target_mhz)).fetchone()
            return {**response_metadata(frame["timestamp"], requested), **dict(row)} if row else None
        rows = [dict(r) for r in conn.execute(
            "SELECT target_mhz, freq_mhz, date_obs FROM images WHERE timestamp = ? ORDER BY target_mhz",
            (frame["timestamp"],))]
    return {**response_metadata(frame["timestamp"], requested), "images": rows}
