import io
import json
import multiprocessing
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import numpy as np
import pytest
from astropy.io import fits

from lwasolarproc.api_service import make_server
from lwasolarproc.api_store import cache_fch_images, parse_timestamp, query_images, query_qa
from lwasolarproc.qa_store import SCHEMA, init_db, measure_flagged_fraction, recent_bands, record_run


def make_planes(directory, date_obs="2026-10-07T22:00:00"):
    directory.mkdir(parents=True, exist_ok=True)
    for i, freq in enumerate((29.9, 45.1, 60.2, 74.8)):
        h = fits.Header({"CRVAL3": freq * 1e6, "CUNIT3": "Hz", "DATE-OBS": date_obs,
                         "BUNIT": "K", "PB_CORR": True, "PBGAIN": 0.7})
        data = np.full((1, 1, 8, 8), i + 1, dtype=np.float32)
        data[0, 0, 0, 0] = np.nan
        fits.writeto(directory / f"{i}.helio.fits", data, h, overwrite=True)
    return directory


def test_qa_migration_and_ant_list(tmp_path):
    path = tmp_path / "qa.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA.replace("    ant_list TEXT,\n", ""))
        c.execute("INSERT INTO qa_runs (id, timestamp, created_utc) VALUES (1, '20261007_215900', '')")
        c.execute("INSERT INTO qa_band (run_id, freq_mhz, n_bad_ant) VALUES (1, 59, 2)")
    with closing(init_db(path)) as c:
        assert recent_bands(c)[0]["ant_list"] is None
        band = {"freq_mhz": 59, "flagged_frac": 0.25, "n_bad_ant": 2, "ant_list": [0, 17]}
        record_run(c, "20261007_220000", [], band_rows=[band])
        assert recent_bands(c)[0]["ant_list"] == [0, 17]
        record_run(c, "20261007_220000", [], band_rows=[{**band, "n_bad_ant": 0, "ant_list": []}])
        assert recent_bands(c)[0]["ant_list"] == []
        assert c.execute("SELECT count(*) FROM qa_runs").fetchone()[0] == 2
    row = query_qa(path, "flagging")["rows"][0]
    assert row["bad_ant"] == 0
    assert row["ant_list"] == []
    assert row["flagging_ratio"] == 0.25


def test_measure_ant_indices(tmp_path):
    tables = pytest.importorskip("casacore.tables")
    desc = tables.maketabdesc([tables.makearrcoldesc("FLAG", False, ndim=2, shape=[2, 2]),
                              tables.makescacoldesc("ANTENNA1", 0),
                              tables.makescacoldesc("ANTENNA2", 0)])
    path = tmp_path / "flags.ms"
    with tables.table(str(path), desc, nrow=4, ack=False) as t:
        t.putcol("ANTENNA1", [0, 0, 1, 4])
        t.putcol("ANTENNA2", [1, 2, 2, 4])
        flags = np.ones((4, 2, 2), dtype=bool)
        flags[2, 0, 0] = False
        t.putcol("FLAG", flags)
    result = measure_flagged_fraction(path)
    assert result["ant_list"] == [0, 4]
    assert result["n_bad_ant"] == 2
    assert result["flagged_frac"] == 15 / 16


@pytest.mark.parametrize("timestamp", ["20261007T220000", "20261007T220000Z", "20261007_220000"])
def test_timestamp_formats(timestamp):
    assert parse_timestamp(timestamp) == datetime(2026, 10, 7, 22, tzinfo=timezone.utc)


@pytest.mark.parametrize("timestamp", ["2026107T220000", "20261307T220000", "20261007T256000", ""])
def test_invalid_timestamp(timestamp):
    with pytest.raises(ValueError):
        parse_timestamp(timestamp)


def seed_qa(path):
    with closing(init_db(path)) as c:
        for stamp, count in [("20261007_220000", 2), ("20261007_220100", 3)]:
            record_run(c, stamp, [{"freq_mhz": 59, "source": "CygA", "measured_jy": 12000,
                                   "expected_jy": 10000, "ratio": 1.2}],
                       band_rows=[{"freq_mhz": 59, "n_bad_ant": count, "ant_list": list(range(count)),
                                   "flagged_frac": 0.25}])


def test_qa_nearest_latest_and_missing(tmp_path):
    path = tmp_path / "qa.db"
    seed_qa(path)
    for kind in ("flagging", "flux"):
        assert query_qa(path, kind)["timestamp"] == "20261007T220100"
        assert query_qa(path, kind, "newest")["timestamp"] == "20261007T220100"
        assert query_qa(path, kind, "20261007T220010")["timestamp"] == "20261007T220000"
        tied = query_qa(path, kind, "20261007T220030")
        assert tied["timestamp"] == "20261007T220100"
        assert tied["delta_seconds"] == 30
        assert query_qa(path, kind, "20200101T000000")["timestamp"] == "20261007T220000"
    assert query_qa(path, "flux")["rows"][0]["ratio"] == 1.2
    with closing(init_db(tmp_path / "empty.db")):
        assert query_qa(tmp_path / "empty.db", "flagging") is None


def test_cache_npz_and_expiration(tmp_path):
    now = datetime(2026, 10, 7, 22, 1, tzinfo=timezone.utc)
    planes = make_planes(tmp_path / "planes")
    path = tmp_path / "images.db"
    assert cache_fch_images(path, "20261007_220000", planes, now=now) == 4
    images = query_images(path, now=now)
    assert images["timestamp"] == "20261007T220000"
    assert [r["target_mhz"] for r in images["images"]] == [30, 45, 60, 75]
    row = query_images(path, "20261007T220010", target_mhz=45, fmt="npz", now=now)
    assert row["delta_seconds"] == 10
    with np.load(io.BytesIO(row["data"]), allow_pickle=False) as arrays:
        assert arrays["image"].shape == (8, 8)
        assert np.isnan(arrays["image"][0, 0])
        assert arrays["image"][1, 1] == 2
        assert arrays["freq_mhz"] == 45.1
        assert str(arrays["bunit"]) == "K"
        assert fits.Header.fromstring(str(arrays["header"]))["PB_CORR"]
    row = query_images(path, target_mhz=30, fmt="fits", now=now)
    assert fits.getheader(io.BytesIO(row["data"]))["CRVAL3"] == 29.9e6
    later = now + timedelta(minutes=10)
    assert query_images(path, now=later) is None
    cache_fch_images(path, "20261007_220100", planes, now=later)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT DISTINCT timestamp FROM images").fetchall() == [("20261007_220100",)]
    # A late completion cannot reintroduce expired observations.
    assert cache_fch_images(path, "20261007_215000", planes, now=later) == 0


def write_cache(args):
    path, stamp, planes, now = args
    return cache_fch_images(path, stamp, planes, now=now)


def test_concurrent_workers_and_out_of_order_completion(tmp_path):
    now = datetime(2026, 10, 7, 22, 1, tzinfo=timezone.utc)
    planes = make_planes(tmp_path / "planes")
    path = tmp_path / "images.db"
    stamps = ["20261007_220040", "20261007_220010", "20261007_220030", "20261007_220020"]
    with multiprocessing.get_context("fork").Pool(4) as pool:
        assert pool.map(write_cache, [(path, stamp, planes, now) for stamp in stamps]) == [4] * 4
    assert query_images(path, now=now)["timestamp"] == "20261007T220040"
    assert query_images(path, "20261007T220025", now=now)["timestamp"] == "20261007T220030"
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM images").fetchone()[0] == 16


def test_http_endpoints(tmp_path):
    qa, images = tmp_path / "qa.db", tmp_path / "images.db"
    seed_qa(qa)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    cache_fch_images(images, stamp, make_planes(tmp_path / "planes"))
    server = make_server(qa, images, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        def read(path):
            with urlopen(url + path, timeout=5) as response:
                return json.load(response)

        assert read("/health")["status"] == "ok"
        assert read("/flagging?timestamp=20261007T220010")["rows"][0]["ant_list"] == [0, 1]
        assert read("/flux?timestamp=newest")["rows"][0]["measured_jy"] == 12000
        metadata = read("/images")
        assert len(metadata["images"]) == 4
        with urlopen(url + metadata["images"][0]["npz_url"]) as response:
            assert response.headers["X-Observation-Timestamp"] == now.strftime("%Y%m%dT%H%M%S")
            with np.load(io.BytesIO(response.read()), allow_pickle=False) as arrays:
                assert arrays["image"].shape == (8, 8)
        for path, code in [("/flagging?timestamp=bad", 400), ("/images/31.npz", 404),
                           ("/flux?timestamp=newest&timestamp=newest", 400), ("/unknown", 404)]:
            with pytest.raises(HTTPError) as exc:
                read(path)
            assert exc.value.code == code
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_worker_caches_before_publish_and_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lwasolarproc import realtime_task_manage as manager

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    task_dir = tmp_path / "proc" / "worker_0" / stamp
    monkeypatch.setattr(manager, "copy_available_ms_inputs", lambda **kwargs: ("59MHz",))
    monkeypatch.setattr(manager, "collect_caltables", lambda **kwargs: [])
    monkeypatch.setattr(manager, "measure_flagged_fraction", lambda path:
                        {"n_bad_ant": 1, "ant_list": [17], "flagged_frac": 0.2})

    def process(*args, **kwargs):
        make_planes(task_dir / "run/combined/helio/fch_I")
        return [SimpleNamespace(status="ok", freq_mhz=59, products={"work_ms": tmp_path / "fake.ms"})]

    monkeypatch.setattr(manager, "process_fullband", process)
    image_db = tmp_path / "images.db"

    def publish(*args, **kwargs):
        assert query_images(image_db)["timestamp"] == stamp.replace("_", "T")
        return ()

    monkeypatch.setattr(manager, "publish_outputs", publish)
    config = manager.WorkerConfig(slow_root=tmp_path, source_layout="flat", proc_tmp=tmp_path / "proc",
                                  proc_out=tmp_path / "out", log_dir=tmp_path / "log", ingest_lustre=False,
                                  caltable_dir=tmp_path, bands=("59MHz",), worker_id=0, pipeline_jobs=1,
                                  threads=1, fch_pols="I", do_refraction=False, logging=False,
                                  cleanup_failed=False, worker_rm_tmp=True, image_cache_db=image_db)
    result = manager.run_worker_task(stamp, config)
    assert result.status == "ok", result.error
    assert result.qa_bands[0]["ant_list"] == [17]
    assert not task_dir.exists()
    assert len(query_images(image_db)["images"]) == 4
