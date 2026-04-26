"""Realtime task manager for the OVRO-LWA fullband solar pipeline."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shutil
import signal
import sys
import tarfile
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .preprocessing_and_imaging import PipelineConfig, collect_caltables, process_fullband
from .util import compress_fits_to_h5, filter_ovro_timestamps_by_solar_elevation


PRODUCTION_BANDS = [
    "23MHz",
    "32MHz",
    "36MHz",
    "41MHz",
    "46MHz",
    "50MHz",
    "55MHz",
    "59MHz",
    "64MHz",
    "69MHz",
    "73MHz",
    "78MHz",
    "82MHz",
]
TIMESTAMP_RE = re.compile(r"(?P<stamp>\d{8}_\d{6})_(?P<band>\d+MHz)\.ms(?:\.tar)?$")


@dataclass(frozen=True)
class RealtimeTask:
    timestamp: str
    discovered_at: str


@dataclass(frozen=True)
class WorkerConfig:
    slow_root: Path
    source_layout: str
    proc_tmp: Path
    proc_out: Path
    caltable_dir: Path
    bands: tuple[str, ...]
    worker_id: int
    pipeline_jobs: int
    threads: int
    fch_pols: str
    cleanup_failed: bool
    worker_rm_tmp: bool


@dataclass(frozen=True)
class WorkerResult:
    timestamp: str
    worker_id: int
    status: str
    elapsed_s: float
    copied_bands: tuple[str, ...]
    output_paths: tuple[str, ...]
    work_dir: str
    error: str = ""


@contextlib.contextmanager
def redirect_process_fds(handle):
    """Redirect child-process stdout/stderr file descriptors to an open log file."""
    handle.flush()
    stdout_fd = os.dup(1)
    stderr_fd = os.dup(2)
    try:
        os.dup2(handle.fileno(), 1)
        os.dup2(handle.fileno(), 2)
        yield
    finally:
        handle.flush()
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.close(stdout_fd)
        os.close(stderr_fd)


def setup_worker_logging(log_path: Path) -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )


class SystemdNotifier:
    def __init__(self) -> None:
        try:
            from systemd import daemon  # type: ignore
        except Exception:
            self._daemon = None
        else:
            self._daemon = daemon

    def notify(self, message: str) -> None:
        if self._daemon is not None:
            self._daemon.notify(message)


def parse_bands(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = list(value)
    bands = tuple(part.strip() for part in raw if part.strip())
    if not bands:
        raise ValueError("At least one band must be configured.")
    invalid = [band for band in bands if not re.fullmatch(r"\d+MHz", band)]
    if invalid:
        raise ValueError(f"Invalid band labels: {invalid}")
    return bands


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def format_output_timestamp(timestamp: str) -> str:
    return parse_timestamp(timestamp).strftime("%Y-%m-%dT%H%M%SZ")


def normalize_mode(mode: str) -> str:
    aliases = {
        "backlog-mode": "backlog",
        "realtime-mode": "realtime",
        "event-data-proc-mode": "event",
        "event-mode": "event",
    }
    return aliases.get(mode, mode)


def timestamp_sequence(start_timestamp: str, end_timestamp: str, cadence_s: float) -> list[str]:
    start = parse_timestamp(start_timestamp)
    end = parse_timestamp(end_timestamp)
    if end < start:
        raise ValueError("--end-timestamp must be at or after --start-timestamp")
    if cadence_s <= 0:
        raise ValueError("--cadence-s must be positive")

    stamps: list[str] = []
    current = start
    step = timedelta(seconds=cadence_s)
    while current <= end:
        stamps.append(current.strftime("%Y%m%d_%H%M%S"))
        current += step
    return stamps


def timestamp_from_ms_name(path: Path) -> str | None:
    match = TIMESTAMP_RE.match(path.name)
    if match is None:
        return None
    return match.group("stamp")


def source_ms_path(slow_root: Path, band: str, timestamp: str, source_layout: str = "structured") -> Path:
    if source_layout == "flat":
        return slow_root / f"{timestamp}_{band}.ms"
    dt = parse_timestamp(timestamp)
    return slow_root / band / dt.strftime("%Y-%m-%d") / dt.strftime("%H") / f"{timestamp}_{band}.ms"


def source_ms_tar_path(slow_root: Path, band: str, timestamp: str, source_layout: str = "structured") -> Path:
    return source_ms_path(slow_root, band, timestamp, source_layout).with_suffix(".ms.tar")


def is_dir_safe(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError as exc:
        logging.debug("Cannot stat directory candidate %s: %s", path, exc)
        return False


def is_file_safe(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError as exc:
        logging.debug("Cannot stat file candidate %s: %s", path, exc)
        return False


def source_ms_input_path(slow_root: Path, band: str, timestamp: str, source_layout: str = "structured") -> Path | None:
    ms_path = source_ms_path(slow_root, band, timestamp, source_layout)
    if is_dir_safe(ms_path):
        return ms_path
    tar_path = source_ms_tar_path(slow_root, band, timestamp, source_layout)
    if is_file_safe(tar_path):
        return tar_path
    return None


def available_band_paths(
    slow_root: Path,
    bands: Sequence[str],
    timestamp: str,
    source_layout: str = "structured",
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for band in bands:
        path = source_ms_input_path(slow_root, band, timestamp, source_layout)
        if path is not None:
            paths[band] = path
    return paths


def trigger_hour_dirs(slow_root: Path, trigger_band: str, lookback_hours: int) -> list[Path]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    dirs: list[Path] = []
    for delta in range(max(0, lookback_hours) + 1):
        dt = now - timedelta(hours=delta)
        hour_dir = slow_root / trigger_band / dt.strftime("%Y-%m-%d") / dt.strftime("%H")
        if is_dir_safe(hour_dir):
            dirs.append(hour_dir)
    return dirs


def discover_trigger_timestamps(slow_root: Path, trigger_band: str, lookback_hours: int) -> list[str]:
    stamps: set[str] = set()
    for hour_dir in trigger_hour_dirs(slow_root, trigger_band, lookback_hours):
        for path in sorted(hour_dir.glob(f"*_{trigger_band}.ms*")):
            stamp = timestamp_from_ms_name(path)
            if stamp is not None:
                stamps.add(stamp)
    return sorted(stamps)


def timestamp_in_range(timestamp: str, start_timestamp: str | None, end_timestamp: str | None) -> bool:
    if start_timestamp is not None and timestamp < start_timestamp:
        return False
    if end_timestamp is not None and timestamp > end_timestamp:
        return False
    return True


def discover_flat_timestamps(
    data_root: Path,
    bands: Sequence[str],
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> list[str]:
    band_set = set(bands)
    stamps: set[str] = set()
    if not is_dir_safe(data_root):
        return []
    for path in sorted(data_root.iterdir()):
        match = TIMESTAMP_RE.match(path.name)
        if match is None or match.group("band") not in band_set:
            continue
        stamp = match.group("stamp")
        if timestamp_in_range(stamp, start_timestamp, end_timestamp):
            stamps.add(stamp)
    return sorted(stamps)


def hour_range(start: datetime, end: datetime) -> list[datetime]:
    current = start.replace(minute=0, second=0, microsecond=0)
    final = end.replace(minute=0, second=0, microsecond=0)
    hours: list[datetime] = []
    while current <= final:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def discover_structured_timestamps(
    slow_root: Path,
    trigger_band: str,
    start_timestamp: str,
    end_timestamp: str,
) -> list[str]:
    start = parse_timestamp(start_timestamp)
    end = parse_timestamp(end_timestamp)
    stamps: set[str] = set()
    for dt in hour_range(start, end):
        hour_dir = slow_root / trigger_band / dt.strftime("%Y-%m-%d") / dt.strftime("%H")
        if not is_dir_safe(hour_dir):
            continue
        for path in sorted(hour_dir.glob(f"*_{trigger_band}.ms*")):
            stamp = timestamp_from_ms_name(path)
            if stamp is not None and timestamp_in_range(stamp, start_timestamp, end_timestamp):
                stamps.add(stamp)
    return sorted(stamps)


def atomic_copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return dst


def atomic_compress_fits(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    result = compress_fits_to_h5(src, tmp)
    if result is None:
        raise RuntimeError(f"Compression produced no HDF output for {src}")
    os.replace(tmp, dst)
    return dst


def ensure_output_dirs(proc_out: Path) -> dict[str, Path]:
    dirs = {
        "mfs": proc_out / "mfs",
        "fch": proc_out / "fch",
        "fig": proc_out / "fig",
        "log": proc_out / "log",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def realtime_output_paths(proc_out: Path, timestamp: str) -> dict[str, Path]:
    stamp = format_output_timestamp(timestamp)
    prefix = f"ovro-lwa-352.lev1.5"
    return {
        "mfs_i_fits": proc_out / "mfs" / f"{prefix}_mfs_10s.{stamp}.image_I.fits",
        "mfs_i_hdf": proc_out / "mfs" / f"{prefix}_mfs_10s.{stamp}.image_I.hdf",
        "mfs_v_fits": proc_out / "mfs" / f"{prefix}_mfs_10s.{stamp}.image_V.fits",
        "mfs_v_hdf": proc_out / "mfs" / f"{prefix}_mfs_10s.{stamp}.image_V.hdf",
        "fch_i_fits": proc_out / "fch" / f"{prefix}_fch_10s.{stamp}.image_I.fits",
        "fch_i_hdf": proc_out / "fch" / f"{prefix}_fch_10s.{stamp}.image_I.hdf",
        "fch_v_fits": proc_out / "fch" / f"{prefix}_fch_10s.{stamp}.image_V.fits",
        "fch_v_hdf": proc_out / "fch" / f"{prefix}_fch_10s.{stamp}.image_V.hdf",
        "mfs_i_png": proc_out / "fig" / f"{prefix}_mfs_10s.{stamp}.image_I.png",
        "mfs_v_png": proc_out / "fig" / f"{prefix}_mfs_10s.{stamp}.image_V.png",
    }


def copy_available_ms_inputs(
    *,
    slow_root: Path,
    source_layout: str,
    bands: Sequence[str],
    timestamp: str,
    input_dir: Path,
) -> tuple[str, ...]:
    input_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for band, src in sorted(available_band_paths(slow_root, bands, timestamp, source_layout).items()):
        copy_ms_input(src, input_dir)
        copied.append(band)
    return tuple(copied)


def copy_ms_input(src: Path, input_dir: Path) -> Path:
    if src.name.endswith(".ms.tar"):
        return copy_and_extract_ms_tar(src, input_dir)
    dst = input_dir / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def copy_and_extract_ms_tar(src: Path, input_dir: Path) -> Path:
    archive_dst = input_dir / src.name
    expected_ms = input_dir / src.name.removesuffix(".tar")
    extract_dir = input_dir / f".{src.name}.extract-{os.getpid()}"
    if expected_ms.exists():
        shutil.rmtree(expected_ms)
    if archive_dst.exists():
        archive_dst.unlink()
    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    shutil.copy2(src, archive_dst)
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_dst, "r") as tar:
            safe_members = list(_safe_tar_members(tar, extract_dir))
            tar.extractall(extract_dir, members=safe_members, filter="data")

        extracted_expected = extract_dir / expected_ms.name
        if extracted_expected.is_dir():
            extracted_expected.rename(expected_ms)
            return expected_ms

        extracted = sorted(path for path in extract_dir.rglob("*.ms") if path.is_dir())
        if len(extracted) == 1:
            extracted[0].rename(expected_ms)
            return expected_ms
        raise FileNotFoundError(f"Archive did not extract a single Measurement Set directory: {src}")
    finally:
        archive_dst.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)


def _safe_tar_members(tar: tarfile.TarFile, destination: Path) -> Sequence[tarfile.TarInfo]:
    destination = destination.resolve()
    safe_members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsupported tar member type in {tar.name}: {member.name}")
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"Unsafe tar member path in {tar.name}: {member.name}")
        target = (destination / member.name).resolve()
        if not str(target).startswith(str(destination) + os.sep) and target != destination:
            raise ValueError(f"Tar member escapes destination in {tar.name}: {member.name}")
        safe_members.append(member)
    return safe_members


def publish_outputs(task_dir: Path, proc_out: Path, timestamp: str) -> tuple[str, ...]:
    combined_dir = task_dir / "run" / "combined"
    run_dir = task_dir / "run"
    source_products = {
        "mfs_i_fits": combined_dir / "combined_mfs_I.fits",
        "mfs_v_fits": combined_dir / "combined_mfs_V.fits",
        "fch_i_fits": combined_dir / "combined_fch_I.fits",
        "fch_v_fits": combined_dir / "combined_fch_V.fits",
        "mfs_i_png": combined_dir / "combined_mfs_I.default_plot.png",
        "mfs_v_png": combined_dir / "combined_mfs_V.default_plot.png",
    }
    required_keys = ("mfs_i_fits", "mfs_v_fits", "fch_i_fits", "mfs_i_png", "mfs_v_png")
    missing = [str(source_products[key]) for key in required_keys if not source_products[key].exists()]
    if missing:
        raise FileNotFoundError(f"Missing combined products: {missing}")

    outputs = realtime_output_paths(proc_out, timestamp)
    published: list[Path] = []
    published.append(atomic_copy(source_products["mfs_i_fits"], outputs["mfs_i_fits"]))
    published.append(atomic_compress_fits(outputs["mfs_i_fits"], outputs["mfs_i_hdf"]))
    published.append(atomic_copy(source_products["mfs_v_fits"], outputs["mfs_v_fits"]))
    published.append(atomic_compress_fits(outputs["mfs_v_fits"], outputs["mfs_v_hdf"]))
    published.append(atomic_copy(source_products["fch_i_fits"], outputs["fch_i_fits"]))
    published.append(atomic_compress_fits(outputs["fch_i_fits"], outputs["fch_i_hdf"]))
    if source_products["fch_v_fits"].exists():
        published.append(atomic_copy(source_products["fch_v_fits"], outputs["fch_v_fits"]))
        published.append(atomic_compress_fits(outputs["fch_v_fits"], outputs["fch_v_hdf"]))
    published.append(atomic_copy(source_products["mfs_i_png"], outputs["mfs_i_png"]))
    published.append(atomic_copy(source_products["mfs_v_png"], outputs["mfs_v_png"]))
    summary_path = run_dir / "preprocessing_and_imaging_summary.tsv"
    if summary_path.exists():
        published.append(atomic_copy(summary_path, proc_out / "log" / f"{timestamp}.summary.tsv"))
    combined_summary = combined_dir / "combined_products.tsv"
    if combined_summary.exists():
        published.append(atomic_copy(combined_summary, proc_out / "log" / f"{timestamp}.combined_products.tsv"))
    return tuple(str(path) for path in published)


def run_worker_task(timestamp: str, config: WorkerConfig) -> WorkerResult:
    start = time.perf_counter()
    task_dir = config.proc_tmp / f"worker_{config.worker_id}" / timestamp
    log_dir = config.proc_out / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    task_log = log_dir / f"{timestamp}.worker_{config.worker_id}.log"
    copied_bands: tuple[str, ...] = ()

    try:
        if task_dir.exists():
            shutil.rmtree(task_dir)
        input_dir = task_dir / "input_ms"
        run_dir = task_dir / "run"
        task_dir.mkdir(parents=True, exist_ok=True)

        setup_worker_logging(task_log)
        with task_log.open("a", encoding="utf-8") as handle:
            with (
                redirect_process_fds(handle),
                contextlib.redirect_stdout(handle),
                contextlib.redirect_stderr(handle),
            ):
                copied_bands = copy_available_ms_inputs(
                    slow_root=config.slow_root,
                    source_layout=config.source_layout,
                    bands=config.bands,
                    timestamp=timestamp,
                    input_dir=input_dir,
                )
                if not copied_bands:
                    raise RuntimeError(f"No available bands copied for {timestamp}")

                caltables = collect_caltables(caltable_dir=config.caltable_dir)
                freqs = [int(band.removesuffix("MHz")) for band in copied_bands]
                pipeline_config = PipelineConfig(
                    work_dir=run_dir,
                    threads=config.threads,
                    copy_ms=False,
                    plot_mfs_i=True,
                    fch_pols=config.fch_pols,
                )
                results = process_fullband(
                    input_dir,
                    caltables,
                    pipeline_config,
                    jobs=min(config.pipeline_jobs, len(copied_bands)),
                    freqs=freqs,
                    min_freq=None,
                    max_freq=None,
                )
                failures = [result for result in results if result.status != "ok"]
                if failures:
                    detail = "; ".join(f"{result.freq_mhz}MHz: {result.error}" for result in failures)
                    raise RuntimeError(f"Fullband pipeline failures: {detail}")

                output_paths = publish_outputs(task_dir, config.proc_out, timestamp)

        if config.worker_rm_tmp:
            shutil.rmtree(task_dir)
        return WorkerResult(
            timestamp=timestamp,
            worker_id=config.worker_id,
            status="ok",
            elapsed_s=time.perf_counter() - start,
            copied_bands=copied_bands,
            output_paths=output_paths,
            work_dir=str(task_dir),
        )
    except Exception as exc:
        if (config.worker_rm_tmp or config.cleanup_failed) and task_dir.exists():
            shutil.rmtree(task_dir)
        return WorkerResult(
            timestamp=timestamp,
            worker_id=config.worker_id,
            status="failed",
            elapsed_s=time.perf_counter() - start,
            copied_bands=copied_bands,
            output_paths=(),
            work_dir=str(task_dir),
            error=str(exc),
        )


class RealtimeManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.mode = normalize_mode(args.mode)
        self.slow_root = args.slow_root.expanduser().resolve()
        self.source_layout = "flat" if self.mode == "event" else "structured"
        self.proc_tmp = args.proc_tmp.expanduser().resolve()
        self.proc_out = args.proc_out.expanduser().resolve()
        self.caltable_dir = args.caltable_dir.expanduser().resolve()
        self.bands = parse_bands(args.bands)
        self.trigger_band = args.trigger_band
        self.ready_min_bands = args.ready_min_bands
        self.queue_length = args.queue_length
        self.dispatch_min_queue = args.dispatch_min_queue
        self.dispatch_stagger_s = args.dispatch_stagger_s
        self.el_valid = args.el_valid
        self.scan_interval = args.scan_interval
        self.scan_lookback_hours = args.scan_lookback_hours
        self.start_timestamp = args.start_timestamp
        self.end_timestamp = args.end_timestamp
        self.cadence_s = args.cadence_s
        self.workers = args.workers
        self.pipeline_jobs = args.pipeline_jobs
        self.threads = args.threads
        self.fch_pols = args.fch_pols
        self.cleanup_failed = args.cleanup_failed
        self.worker_rm_tmp = args.worker_rm_tmp
        self.once = args.once
        self.max_tasks = args.max_tasks
        self.notifier = SystemdNotifier()

        self.queue: deque[RealtimeTask] = deque()
        self.queued: set[str] = set()
        self.running: set[str] = set()
        self.done: set[str] = set()
        self.failed: set[str] = set()
        self.stop_requested = False
        self.completed_count = 0
        self.scanned_once = False
        self.static_scan_index = 0
        self.static_scan_exhausted = False
        if self.mode == "backlog":
            self.static_timestamps = discover_structured_timestamps(
                self.slow_root,
                self.trigger_band,
                self.start_timestamp,
                self.end_timestamp,
            )
        elif self.mode == "event":
            self.static_timestamps = discover_flat_timestamps(
                self.slow_root,
                self.bands,
                self.start_timestamp,
                self.end_timestamp,
            )
        else:
            self.static_timestamps = []
        self.last_enqueued_timestamp: str | None = None
        self.last_dispatch_monotonic: float | None = None

        self.output_dirs = ensure_output_dirs(self.proc_out)
        for worker_id in range(self.workers):
            (self.proc_tmp / f"worker_{worker_id}").mkdir(parents=True, exist_ok=True)
        if self.mode in {"backlog", "event"}:
            logging.info("Discovered %d existing timestamp(s) for %s mode", len(self.static_timestamps), self.mode)

    def request_stop(self, signum: int, _frame: object) -> None:
        logging.info("Received signal %s; stopping after running tasks finish", signum)
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def scan_once(self) -> None:
        if self.mode in {"backlog", "event"}:
            self.scan_static_range_once()
            return
        self.scan_realtime_once()

    def scan_realtime_once(self) -> None:
        if len(self.queue) >= self.queue_length:
            logging.debug("Queue already full; skipping scan")
            return
        discovered = discover_trigger_timestamps(self.slow_root, self.trigger_band, self.scan_lookback_hours)
        if self.start_timestamp is not None:
            discovered = [timestamp for timestamp in discovered if timestamp >= self.start_timestamp]
        before_elevation = len(discovered)
        discovered = filter_ovro_timestamps_by_solar_elevation(discovered, min_elevation_deg=self.el_valid)
        skipped_elevation = before_elevation - len(discovered)
        if skipped_elevation > 0:
            logging.info(
                "Skipped %d timestamp(s) with Sun elevation below %.1f deg at OVRO",
                skipped_elevation,
                self.el_valid,
            )

        candidates: list[tuple[str, dict[str, Path]]] = []
        for timestamp in discovered:
            if timestamp in self.queued or timestamp in self.running or timestamp in self.done or timestamp in self.failed:
                continue
            available = available_band_paths(self.slow_root, self.bands, timestamp, self.source_layout)
            if len(available) < self.ready_min_bands:
                continue
            candidates.append((timestamp, available))

        candidates = self.filter_candidates_by_min_cadence(candidates)
        remaining_slots = self.queue_length - len(self.queue)
        selected = candidates[-remaining_slots:] if remaining_slots > 0 else []
        for timestamp, available in selected:
            self.queue_task(timestamp, available)
        if len(candidates) > len(selected):
            logging.info(
                "Skipped %d older ready timestamp(s) to keep realtime queue on latest data",
                len(candidates) - len(selected),
            )

    def scan_static_range_once(self) -> None:
        if len(self.queue) >= self.queue_length or self.static_scan_exhausted:
            return
        while len(self.queue) < self.queue_length and self.static_scan_index < len(self.static_timestamps):
            timestamp = self.static_timestamps[self.static_scan_index]
            self.static_scan_index += 1
            if timestamp in self.queued or timestamp in self.running or timestamp in self.done or timestamp in self.failed:
                continue
            available = available_band_paths(self.slow_root, self.bands, timestamp, self.source_layout)
            if len(available) < self.ready_min_bands:
                self.failed.add(timestamp)
                logging.error(
                    "Missing data for %s: visible bands %d/%d below ready-min-bands=%d",
                    timestamp,
                    len(available),
                    len(self.bands),
                    self.ready_min_bands,
                )
                continue
            self.queue_task(timestamp, available)
        if self.static_scan_index >= len(self.static_timestamps):
            self.static_scan_exhausted = True

    def cadence_allows(self, timestamp: str) -> bool:
        if self.last_enqueued_timestamp is None:
            return True
        delta_s = (parse_timestamp(timestamp) - parse_timestamp(self.last_enqueued_timestamp)).total_seconds()
        return delta_s >= self.cadence_s

    def filter_candidates_by_min_cadence(
        self,
        candidates: Sequence[tuple[str, dict[str, Path]]],
    ) -> list[tuple[str, dict[str, Path]]]:
        selected: list[tuple[str, dict[str, Path]]] = []
        last_timestamp = self.last_enqueued_timestamp
        skipped = 0
        for timestamp, available in sorted(candidates, key=lambda item: item[0]):
            if last_timestamp is not None:
                delta_s = (parse_timestamp(timestamp) - parse_timestamp(last_timestamp)).total_seconds()
                if delta_s < self.cadence_s:
                    skipped += 1
                    continue
            selected.append((timestamp, available))
            last_timestamp = timestamp
        if skipped:
            logging.info("Skipped %d ready timestamp(s) below minimum cadence %.1fs", skipped, self.cadence_s)
        return selected

    def queue_task(self, timestamp: str, available: Mapping[str, Path]) -> None:
        if len(self.queue) >= self.queue_length:
            logging.info("Queue reached capacity; leaving later timestamps for future scans")
            return
        if not self.cadence_allows(timestamp):
            logging.info(
                "Skipped %s below minimum cadence %.1fs after last queued timestamp %s",
                timestamp,
                self.cadence_s,
                self.last_enqueued_timestamp,
            )
            return
        task = RealtimeTask(
            timestamp=timestamp,
            discovered_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.queue.append(task)
        self.queued.add(timestamp)
        self.last_enqueued_timestamp = timestamp
        logging.info(
            "Queued %s with %d visible bands: %s",
            timestamp,
            len(available),
            ",".join(sorted(available)),
        )

    def dispatch_threshold(self) -> int:
        if self.mode in {"backlog", "event"} and self.static_scan_exhausted:
            return 1
        return self.dispatch_min_queue

    def scan_is_complete(self) -> bool:
        return self.mode in {"backlog", "event"} and self.static_scan_exhausted

    def can_start_more_tasks(self, futures: Mapping[Future[WorkerResult], tuple[int, str]]) -> bool:
        return self.max_tasks is None or self.completed_count + len(futures) < self.max_tasks

    def dispatch_ready(
        self,
        executor: ProcessPoolExecutor,
        idle_workers: list[int],
        futures: dict[Future[WorkerResult], tuple[int, str]],
    ) -> None:
        if len(self.queue) < self.dispatch_threshold():
            return
        while idle_workers and self.queue:
            if not self.can_start_more_tasks(futures):
                break
            multiple_idle = len(idle_workers) > 1
            now = time.monotonic()
            if (
                multiple_idle
                and self.dispatch_stagger_s > 0
                and self.last_dispatch_monotonic is not None
                and now - self.last_dispatch_monotonic < self.dispatch_stagger_s
            ):
                break
            worker_id = idle_workers.pop(0)
            task = self.queue.popleft()
            self.queued.remove(task.timestamp)
            self.running.add(task.timestamp)
            worker_config = WorkerConfig(
                slow_root=self.slow_root,
                source_layout=self.source_layout,
                proc_tmp=self.proc_tmp,
                proc_out=self.proc_out,
                caltable_dir=self.caltable_dir,
                bands=self.bands,
                worker_id=worker_id,
                pipeline_jobs=self.pipeline_jobs,
                threads=self.threads,
                fch_pols=self.fch_pols,
                cleanup_failed=self.cleanup_failed,
                worker_rm_tmp=self.worker_rm_tmp,
            )
            future = executor.submit(run_worker_task, task.timestamp, worker_config)
            futures[future] = (worker_id, task.timestamp)
            self.last_dispatch_monotonic = now
            logging.info("Dispatched %s to worker_%d", task.timestamp, worker_id)
            if multiple_idle and self.dispatch_stagger_s > 0:
                break

    def handle_finished(
        self,
        futures: dict[Future[WorkerResult], tuple[int, str]],
        idle_workers: list[int],
    ) -> None:
        if not futures:
            return
        done, _ = wait(list(futures), timeout=0, return_when=FIRST_COMPLETED)
        for future in done:
            worker_id, timestamp = futures.pop(future)
            idle_workers.append(worker_id)
            try:
                result = future.result()
            except Exception as exc:
                self.running.discard(timestamp)
                self.failed.add(timestamp)
                self.completed_count += 1
                logging.exception("Worker_%d crashed while processing %s: %s", worker_id, timestamp, exc)
                continue
            self.running.discard(result.timestamp)
            self.completed_count += 1
            if result.status == "ok":
                self.done.add(result.timestamp)
                logging.info(
                    "Completed %s on worker_%d in %.2fs with bands=%s",
                    result.timestamp,
                    worker_id,
                    result.elapsed_s,
                    ",".join(result.copied_bands),
                )
                for output_path in result.output_paths:
                    logging.info("Published %s", output_path)
            else:
                self.failed.add(result.timestamp)
                logging.error(
                    "Failed %s on worker_%d in %.2fs: %s",
                    result.timestamp,
                    worker_id,
                    result.elapsed_s,
                    result.error,
                )

    def should_stop_loop(self, futures: Mapping[Future[WorkerResult], tuple[int, str]]) -> bool:
        if self.stop_requested:
            return not futures
        if self.max_tasks is not None and self.completed_count >= self.max_tasks:
            return not futures
        if self.mode in {"backlog", "event"}:
            return self.static_scan_exhausted and not futures and not self.queue
        if self.once:
            return not futures and (not self.queue or len(self.queue) < self.dispatch_min_queue)
        return False

    def run(self) -> int:
        self.install_signal_handlers()
        self.notifier.notify("READY=1")
        logging.info(
            "Task manager started; mode=%s source_layout=%s data_root=%s bands=%s",
            self.mode,
            self.source_layout,
            self.slow_root,
            ",".join(self.bands),
        )
        idle_workers = list(range(self.workers))
        futures: dict[Future[WorkerResult], tuple[int, str]] = {}

        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            while True:
                can_scan = (
                    not self.stop_requested
                    and (self.mode in {"backlog", "event"} or not self.once or not self.scanned_once)
                    and self.can_start_more_tasks(futures)
                )
                if can_scan:
                    self.scan_once()
                    self.scanned_once = True
                if not self.stop_requested:
                    self.dispatch_ready(executor, idle_workers, futures)
                self.handle_finished(futures, idle_workers)

                self.notifier.notify(
                    f"STATUS=mode={self.mode} queue={len(self.queue)} running={len(futures)} "
                    f"done={len(self.done)} failed={len(self.failed)}"
                )
                if self.should_stop_loop(futures):
                    break
                time.sleep(self.scan_interval)

        logging.info("Task manager stopped; done=%d failed=%d", len(self.done), len(self.failed))
        return 1 if self.failed else 0

def setup_logging(proc_out: Path) -> None:
    log_dir = proc_out / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "realtime_task_manage.log"
    logging.Formatter.converter = time.gmtime
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage OVRO-LWA fullband solar processing tasks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["realtime", "realtime-mode", "backlog", "backlog-mode", "event", "event-mode", "event-data-proc-mode"],
        default="realtime",
        help="Task source mode. backlog uses the structured slow-data tree over a fixed time range; realtime follows latest elevated slow-data frames; event uses a flat data directory over a fixed time range.",
    )
    parser.add_argument("--slow-root", "--data-root", "--data-dir", dest="slow_root", type=Path, default=Path("/lustre/pipeline/slow"))
    parser.add_argument("--proc-tmp", type=Path, default=Path("./proc_tmp"))
    parser.add_argument("--proc-out", type=Path, default=Path("./proc_out"))
    parser.add_argument("--caltable-dir", type=Path, default=Path("/fast/rtpipe/caltab_h5parm"))
    parser.add_argument("--bands", default=",".join(PRODUCTION_BANDS))
    parser.add_argument("--trigger-band", default="55MHz")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--queue-length", type=int, help="Maximum queued timestamps. Defaults to workers + 3.")
    parser.add_argument("--dispatch-min-queue", type=int, default=3)
    parser.add_argument(
        "--dispatch-stagger-s",
        type=float,
        default=15.0,
        help="Minimum seconds between dispatches when more than one worker is idle.",
    )
    parser.add_argument("--el-valid", type=float, default=12.0, help="Queue only when Sun elevation at OVRO is at least this many degrees.")
    parser.add_argument("--ready-min-bands", type=int, default=7)
    parser.add_argument("--scan-interval", type=float, default=5.0)
    parser.add_argument("--scan-lookback-hours", type=int, default=1)
    parser.add_argument("--start-timestamp", help="Only consider timestamps at or after YYYYMMDD_HHMMSS.")
    parser.add_argument("--end-timestamp", help="Only consider timestamps at or before YYYYMMDD_HHMMSS.")
    parser.add_argument("--cadence-s", type=float, default=10.0, help="Minimum allowed seconds between enqueued timestamps in all modes.")
    parser.add_argument("--pipeline-jobs", type=int, default=13)
    parser.add_argument("--threads", type=int, default=18)
    parser.add_argument("--fch-pols", default="I", help="Comma-separated polarizations for the fine-channel WSClean pass, for example I or I,V.")
    parser.add_argument("--cleanup-failed", action="store_true")
    parser.add_argument(
        "--worker-rm-tmp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove a worker's per-job proc_tmp directory before the worker becomes available for another job.",
    )
    parser.add_argument("--once", action="store_true", help="Scan once and exit after dispatchable work finishes.")
    parser.add_argument("--max-tasks", type=int, help="Stop after this many worker tasks complete.")
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    args.mode = normalize_mode(args.mode)
    if args.mode not in {"realtime", "backlog", "event"}:
        raise ValueError(f"Invalid mode: {args.mode}")
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.queue_length is None:
        args.queue_length = args.workers + 3
    if args.queue_length < 1:
        raise ValueError("--queue-length must be at least 1")
    if args.dispatch_min_queue < 1:
        raise ValueError("--dispatch-min-queue must be at least 1")
    if args.dispatch_stagger_s < 0:
        raise ValueError("--dispatch-stagger-s must be non-negative")
    if not (-90.0 <= args.el_valid <= 90.0):
        raise ValueError("--el-valid must be between -90 and 90 degrees")
    if args.ready_min_bands < 1:
        raise ValueError("--ready-min-bands must be at least 1")
    if args.start_timestamp is not None:
        parse_timestamp(args.start_timestamp)
    if args.end_timestamp is not None:
        parse_timestamp(args.end_timestamp)
    if args.cadence_s <= 0:
        raise ValueError("--cadence-s must be positive")
    if args.start_timestamp is not None and args.end_timestamp is not None:
        if parse_timestamp(args.end_timestamp) < parse_timestamp(args.start_timestamp):
            raise ValueError("--end-timestamp must be at or after --start-timestamp")
    if args.mode == "backlog":
        if args.start_timestamp is None or args.end_timestamp is None:
            raise ValueError("--start-timestamp and --end-timestamp are required for backlog mode")
    bands = parse_bands(args.bands)
    if args.mode in {"realtime", "backlog"} and args.trigger_band not in bands:
        raise ValueError("--trigger-band must be included in --bands for realtime/backlog modes")
    if args.ready_min_bands > len(bands):
        raise ValueError("--ready-min-bands cannot exceed the number of configured bands")
    if not args.slow_root.exists():
        raise FileNotFoundError(f"Slow-data root does not exist: {args.slow_root}")
    if not args.caltable_dir.exists():
        raise FileNotFoundError(f"Caltable directory does not exist: {args.caltable_dir}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    setup_logging(args.proc_out.expanduser().resolve())
    manager = RealtimeManager(args)
    return manager.run()


if __name__ == "__main__":
    raise SystemExit(main())
