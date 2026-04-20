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
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .preprocessing_and_imaging import PipelineConfig, collect_caltables, process_fullband
from .util import compress_fits_to_h5


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
TIMESTAMP_RE = re.compile(r"(?P<stamp>\d{8}_\d{6})_(?P<band>\d+MHz)\.ms$")


@dataclass(frozen=True)
class RealtimeTask:
    timestamp: str
    discovered_at: str


@dataclass(frozen=True)
class WorkerConfig:
    slow_root: Path
    proc_tmp: Path
    proc_out: Path
    caltable_dir: Path
    bands: tuple[str, ...]
    worker_id: int
    pipeline_jobs: int
    threads: int
    cleanup_failed: bool


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


def timestamp_from_ms_name(path: Path) -> str | None:
    match = TIMESTAMP_RE.match(path.name)
    if match is None:
        return None
    return match.group("stamp")


def source_ms_path(slow_root: Path, band: str, timestamp: str) -> Path:
    dt = parse_timestamp(timestamp)
    return slow_root / band / dt.strftime("%Y-%m-%d") / dt.strftime("%H") / f"{timestamp}_{band}.ms"


def is_dir_safe(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError as exc:
        logging.debug("Cannot stat directory candidate %s: %s", path, exc)
        return False


def available_band_paths(slow_root: Path, bands: Sequence[str], timestamp: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for band in bands:
        path = source_ms_path(slow_root, band, timestamp)
        if is_dir_safe(path):
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
        for path in sorted(hour_dir.glob(f"*_{trigger_band}.ms")):
            stamp = timestamp_from_ms_name(path)
            if stamp is not None:
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
        "mfs_i_png": proc_out / "fig" / f"{prefix}_mfs_10s.{stamp}.image_I.png",
    }


def copy_available_ms_inputs(
    *,
    slow_root: Path,
    bands: Sequence[str],
    timestamp: str,
    input_dir: Path,
) -> tuple[str, ...]:
    input_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for band, src in sorted(available_band_paths(slow_root, bands, timestamp).items()):
        dst = input_dir / src.name
        shutil.copytree(src, dst)
        copied.append(band)
    return tuple(copied)


def publish_outputs(task_dir: Path, proc_out: Path, timestamp: str) -> tuple[str, ...]:
    combined_dir = task_dir / "run" / "combined"
    run_dir = task_dir / "run"
    source_products = {
        "mfs_i_fits": combined_dir / "combined_mfs_I.fits",
        "mfs_v_fits": combined_dir / "combined_mfs_V.fits",
        "fch_i_fits": combined_dir / "combined_fch_I.fits",
        "mfs_i_png": combined_dir / "combined_mfs_I.default_plot.png",
    }
    missing = [str(path) for path in source_products.values() if not path.exists()]
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
    published.append(atomic_copy(source_products["mfs_i_png"], outputs["mfs_i_png"]))
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

        with task_log.open("w", encoding="utf-8") as handle:
            with contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
                copied_bands = copy_available_ms_inputs(
                    slow_root=config.slow_root,
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
        if config.cleanup_failed and task_dir.exists():
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
        self.slow_root = args.slow_root.expanduser().resolve()
        self.proc_tmp = args.proc_tmp.expanduser().resolve()
        self.proc_out = args.proc_out.expanduser().resolve()
        self.caltable_dir = args.caltable_dir.expanduser().resolve()
        self.bands = parse_bands(args.bands)
        self.trigger_band = args.trigger_band
        self.ready_min_bands = args.ready_min_bands
        self.queue_length = args.queue_length
        self.dispatch_min_queue = args.dispatch_min_queue
        self.scan_interval = args.scan_interval
        self.scan_lookback_hours = args.scan_lookback_hours
        self.workers = args.workers
        self.pipeline_jobs = args.pipeline_jobs
        self.threads = args.threads
        self.cleanup_failed = args.cleanup_failed
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

        self.output_dirs = ensure_output_dirs(self.proc_out)
        for worker_id in range(self.workers):
            (self.proc_tmp / f"worker_{worker_id}").mkdir(parents=True, exist_ok=True)

    def request_stop(self, signum: int, _frame: object) -> None:
        logging.info("Received signal %s; stopping after running tasks finish", signum)
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def scan_once(self) -> None:
        for timestamp in discover_trigger_timestamps(self.slow_root, self.trigger_band, self.scan_lookback_hours):
            if timestamp in self.queued or timestamp in self.running or timestamp in self.done or timestamp in self.failed:
                continue
            available = available_band_paths(self.slow_root, self.bands, timestamp)
            if len(available) < self.ready_min_bands:
                continue
            if len(self.queue) >= self.queue_length:
                logging.warning("Queue full; leaving %s for a future scan", timestamp)
                continue
            task = RealtimeTask(
                timestamp=timestamp,
                discovered_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            self.queue.append(task)
            self.queued.add(timestamp)
            logging.info(
                "Queued %s with %d visible bands: %s",
                timestamp,
                len(available),
                ",".join(sorted(available)),
            )

    def dispatch_ready(
        self,
        executor: ProcessPoolExecutor,
        idle_workers: list[int],
        futures: dict[Future[WorkerResult], tuple[int, str]],
    ) -> None:
        if len(self.queue) < self.dispatch_min_queue:
            return
        while idle_workers and self.queue:
            if self.max_tasks is not None and self.completed_count + len(futures) >= self.max_tasks:
                break
            worker_id = idle_workers.pop(0)
            task = self.queue.popleft()
            self.queued.remove(task.timestamp)
            self.running.add(task.timestamp)
            worker_config = WorkerConfig(
                slow_root=self.slow_root,
                proc_tmp=self.proc_tmp,
                proc_out=self.proc_out,
                caltable_dir=self.caltable_dir,
                bands=self.bands,
                worker_id=worker_id,
                pipeline_jobs=self.pipeline_jobs,
                threads=self.threads,
                cleanup_failed=self.cleanup_failed,
            )
            future = executor.submit(run_worker_task, task.timestamp, worker_config)
            futures[future] = (worker_id, task.timestamp)
            logging.info("Dispatched %s to worker_%d", task.timestamp, worker_id)

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
        if self.once:
            return not futures and (not self.queue or len(self.queue) < self.dispatch_min_queue)
        return False

    def run(self) -> int:
        self.install_signal_handlers()
        self.notifier.notify("READY=1")
        logging.info("Realtime manager started; slow_root=%s bands=%s", self.slow_root, ",".join(self.bands))
        idle_workers = list(range(self.workers))
        futures: dict[Future[WorkerResult], tuple[int, str]] = {}

        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            while True:
                if not self.stop_requested and (self.max_tasks is None or self.completed_count < self.max_tasks):
                    self.scan_once()
                if not self.stop_requested:
                    self.dispatch_ready(executor, idle_workers, futures)
                self.handle_finished(futures, idle_workers)

                self.notifier.notify(
                    f"STATUS=queue={len(self.queue)} running={len(futures)} "
                    f"done={len(self.done)} failed={len(self.failed)}"
                )
                if self.should_stop_loop(futures):
                    break
                time.sleep(self.scan_interval)

        logging.info("Realtime manager stopped; done=%d failed=%d", len(self.done), len(self.failed))
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
        description="Manage realtime OVRO-LWA fullband solar processing tasks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--slow-root", type=Path, default=Path("/lustre/pipeline/slow"))
    parser.add_argument("--proc-tmp", type=Path, default=Path("./proc_tmp"))
    parser.add_argument("--proc-out", type=Path, default=Path("./proc_out"))
    parser.add_argument("--caltable-dir", type=Path, default=Path("/fast/rtpipe/caltab_h5parm"))
    parser.add_argument("--bands", default=",".join(PRODUCTION_BANDS))
    parser.add_argument("--trigger-band", default="55MHz")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--queue-length", type=int, default=8)
    parser.add_argument("--dispatch-min-queue", type=int, default=3)
    parser.add_argument("--ready-min-bands", type=int, default=7)
    parser.add_argument("--scan-interval", type=float, default=5.0)
    parser.add_argument("--scan-lookback-hours", type=int, default=1)
    parser.add_argument("--pipeline-jobs", type=int, default=13)
    parser.add_argument("--threads", type=int, default=18)
    parser.add_argument("--cleanup-failed", action="store_true")
    parser.add_argument("--once", action="store_true", help="Scan once and exit after dispatchable work finishes.")
    parser.add_argument("--max-tasks", type=int, help="Stop after this many worker tasks complete.")
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.queue_length < 1:
        raise ValueError("--queue-length must be at least 1")
    if args.dispatch_min_queue < 1:
        raise ValueError("--dispatch-min-queue must be at least 1")
    if args.ready_min_bands < 1:
        raise ValueError("--ready-min-bands must be at least 1")
    bands = parse_bands(args.bands)
    if args.trigger_band not in bands:
        raise ValueError("--trigger-band must be included in --bands")
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
