"""
Full-band LWA solar preprocessing and WSClean imaging.

This module ports the active test full-band workflow into package code:

1. copy each input Measurement Set into a per-band work directory
2. DP3 applycal from a matching H5Parm caltable
3. DP3 AOFlagger using ``LWA_sun_PZ.lua``
4. DP3 average by frequency
5. TTCalSun solve/application mode, normally ``zest``
6. DP3 average again
7. chgcentre to the Sun
8. WSClean Stokes I/V FITS imaging
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .wsclean_helper import WSCleanOptions, expected_image_fits, run_wsclean
except ImportError:  # pragma: no cover - supports direct script execution.
    from wsclean_helper import WSCleanOptions, expected_image_fits, run_wsclean


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DEFAULT_WORK_DIR = ROOT / "tests" / "_lwasolarproc_fullband"
DEFAULT_AOFLAGGER_STRATEGY = PACKAGE_DIR / "LWA_sun_PZ.lua"
DEFAULT_SOURCES_JSON = ROOT / "TTCalX" / "sources.json"
if not DEFAULT_SOURCES_JSON.exists():
    DEFAULT_SOURCES_JSON = ROOT / "TTCal.jl" / "test" / "sources.json"
DEFAULT_TTCALSUN_BIN = ROOT / "TTCalSun" / "bin" / "ttcalsun_cpu.sh"
DEFAULT_DP3_BIN = "/opt/dp3-6.5.1/bin/DP3"
DEFAULT_CASARC = Path.home() / ".casarc"


@dataclass(frozen=True)
class BandTarget:
    freq_mhz: int
    src_ms: Path
    caltable: Path
    work_dir: Path


@dataclass
class BandResult:
    freq_mhz: int
    status: str
    work_dir: Path
    elapsed_s: float
    products: dict[str, Path] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    error: str = ""


@dataclass
class PipelineConfig:
    work_dir: Path = DEFAULT_WORK_DIR
    dp3_bin: str = DEFAULT_DP3_BIN
    ttcalsun_bin: str = str(DEFAULT_TTCALSUN_BIN)
    sources_json: Path = DEFAULT_SOURCES_JSON
    aoflagger_strategy: Path = DEFAULT_AOFLAGGER_STRATEGY
    casarc: Path | None = DEFAULT_CASARC
    mode: str = "zest"
    beam: str = "lwa178"
    threads: int = 48
    avg_chanbin: int = 4
    maxiter: int = 30
    tolerance: float = 1e-4
    minuvw: float = 30.0
    maxuvw: float | None = None
    peeliter: int = 3
    phase_only_maxiter: int = 0
    column: str = "CORRECTED_DATA"
    observatory: str = "OVRO"
    reuse_workdir: bool = False
    dry_run: bool = False
    copy_ms: bool = True
    run_ttcalsun: bool = True
    image_pols: Sequence[str] = ("I", "V")
    wsclean: WSCleanOptions = field(default_factory=WSCleanOptions)


def shlex_join(cmd: Sequence[object]) -> str:
    import shlex

    return " ".join(shlex.quote(str(part)) for part in cmd)


def run_command(
    cmd: Sequence[str],
    *,
    dry_run: bool = False,
    capture: bool = False,
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str] | None:
    prefix = "[dry-run]" if dry_run else "[run]"
    print(f"{prefix} {shlex_join(cmd)}")
    if dry_run:
        return None
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
        check=True,
    )


def extract_freq_mhz(path: str | Path) -> int:
    match = re.search(r"(?<!\d)(\d+)MHz(?:\.[^.]+)?$", Path(path).name)
    if not match:
        raise ValueError(f"Cannot extract frequency from path: {path}")
    return int(match.group(1))


def discover_ms(
    ms_dir: str | Path,
    *,
    pattern: str = "*.ms",
    freqs: Iterable[int] | None = None,
    min_freq: int | None = 23,
    max_freq: int | None = 82,
) -> list[Path]:
    allowed = set(freqs) if freqs else None
    result: list[Path] = []
    for path in sorted(Path(ms_dir).expanduser().resolve().glob(pattern)):
        if not path.is_dir():
            continue
        freq = extract_freq_mhz(path)
        if allowed is not None and freq not in allowed:
            continue
        if allowed is None and min_freq is not None and freq < min_freq:
            continue
        if allowed is None and max_freq is not None and freq > max_freq:
            continue
        result.append(path)
    if not result:
        raise ValueError(f"No Measurement Sets matched in {ms_dir}")
    return result


def read_path_list(path: str | Path) -> list[Path]:
    items: list[Path] = []
    for line in Path(path).expanduser().read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        items.append(Path(text).expanduser().resolve())
    return items


def collect_caltables(
    caltables: Iterable[str | Path] = (),
    *,
    caltable_list: str | Path | None = None,
    caltable_dir: str | Path | None = None,
    pattern: str = "*.h5",
) -> list[Path]:
    paths = [Path(path).expanduser().resolve() for path in caltables]
    if caltable_list is not None:
        paths.extend(read_path_list(caltable_list))
    if caltable_dir is not None:
        paths.extend(sorted(Path(caltable_dir).expanduser().resolve().glob(pattern)))
    unique = sorted({path for path in paths})
    if not unique:
        raise ValueError("No caltables were provided.")
    missing = [path for path in unique if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing caltable paths: {missing}")
    return unique


def match_caltables_by_freq(caltables: Iterable[Path]) -> dict[int, Path]:
    by_freq: dict[int, list[Path]] = {}
    for path in caltables:
        freq = extract_freq_mhz(path)
        by_freq.setdefault(freq, []).append(path)

    matched: dict[int, Path] = {}
    for freq, paths in by_freq.items():
        if len(paths) != 1:
            raise ValueError(f"Expected one caltable for {freq} MHz, got {paths}")
        matched[freq] = paths[0]
    return matched


def build_targets(
    ms_paths: Iterable[Path],
    caltables_by_freq: Mapping[int, Path],
    work_dir: str | Path,
) -> list[BandTarget]:
    targets: list[BandTarget] = []
    base_work_dir = Path(work_dir).expanduser().resolve()
    for ms_path in sorted(ms_paths, key=extract_freq_mhz):
        freq = extract_freq_mhz(ms_path)
        if freq not in caltables_by_freq:
            raise ValueError(f"No caltable provided for {freq} MHz")
        targets.append(
            BandTarget(
                freq_mhz=freq,
                src_ms=ms_path.resolve(),
                caltable=caltables_by_freq[freq].resolve(),
                work_dir=base_work_dir / f"{freq:02d}MHz",
            )
        )
    return targets


def build_dp3_applycal_command(config: PipelineConfig, ms_path: Path, caltable: Path) -> list[str]:
    return [
        config.dp3_bin,
        f"msin={ms_path}",
        "msin.datacolumn=DATA",
        "msout=.",
        "msout.datacolumn=CORRECTED_DATA",
        "steps=[ac]",
        "ac.type=applycal",
        f"ac.parmdb={caltable}",
        "ac.solset=sol000",
        "ac.steps=[amp,phase]",
        "ac.amp.correction=amplitude000",
        "ac.phase.correction=phase000",
    ]


def build_dp3_aoflagger_command(config: PipelineConfig, ms_path: Path) -> list[str]:
    return [
        config.dp3_bin,
        f"msin={ms_path}",
        "msin.datacolumn=CORRECTED_DATA",
        "msout=.",
        "msout.datacolumn=CORRECTED_DATA",
        "steps=[aoflag]",
        "aoflag.type=aoflagger",
        f"aoflag.strategy={config.aoflagger_strategy.resolve()}",
        "aoflag.keepstatistics=true",
    ]


def build_dp3_averager_command(
    config: PipelineConfig,
    input_ms: Path,
    output_ms: Path,
    input_column: str,
) -> list[str]:
    return [
        config.dp3_bin,
        f"msin={input_ms}",
        f"msin.datacolumn={input_column}",
        f"msout={output_ms}",
        "msout.overwrite=true",
        "steps=[avg]",
        "avg.type=averager",
        f"avg.freqstep={config.avg_chanbin}",
    ]


def build_ttcalsun_command(config: PipelineConfig, ms_path: Path, column: str = "DATA") -> list[str]:
    cmd = [
        config.ttcalsun_bin,
        config.mode,
        f"--column={column}",
        f"--maxiter={config.maxiter}",
        f"--tolerance={config.tolerance}",
        f"--minuvw={config.minuvw}",
        f"--beam={config.beam}",
        f"--peeliter={config.peeliter}",
        f"--phase-only-maxiter={config.phase_only_maxiter}",
        "--timings",
        str(config.sources_json.resolve()),
        str(ms_path),
    ]
    if config.maxuvw is not None:
        cmd.insert(6, f"--maxuvw={config.maxuvw}")
    return cmd


def averaged_ms_path(ms_path: Path, chanbin: int) -> Path:
    return ms_path.with_name(f"{ms_path.stem}_avg{chanbin}.ms")


def casarc_env(config: PipelineConfig) -> dict[str, str]:
    if config.casarc is None:
        return {}
    return {"CASARCFILES": str(config.casarc)}


def prepare_work_ms(target: BandTarget, config: PipelineConfig) -> Path:
    work_ms = target.work_dir / target.src_ms.name
    if not config.copy_ms:
        return target.src_ms
    if work_ms.exists():
        if config.reuse_workdir:
            print(f"[reuse] {work_ms}")
            return work_ms
        raise FileExistsError(f"Work MS already exists: {work_ms}")
    print(f"[copy] {target.src_ms} -> {work_ms}")
    if not config.dry_run:
        target.work_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target.src_ms, work_ms)
    return work_ms


def run_average_channels(
    input_ms: Path,
    input_column: str,
    config: PipelineConfig,
    timings: dict[str, float],
    key: str,
) -> Path:
    output_ms = averaged_ms_path(input_ms, config.avg_chanbin)
    if output_ms.exists():
        if config.reuse_workdir:
            print(f"[reuse] {output_ms}")
            return output_ms
        raise FileExistsError(f"Averaged MS already exists: {output_ms}")
    start = time.perf_counter()
    run_command(
        build_dp3_averager_command(config, input_ms, output_ms, input_column),
        dry_run=config.dry_run,
        extra_env=casarc_env(config),
    )
    timings[key] = time.perf_counter() - start
    return output_ms


def compute_sun_phasecenter(ms_path: Path, observatory: str) -> tuple[str, str, str, str]:
    import astropy.units as u  # type: ignore
    from astropy.coordinates import EarthLocation, get_body  # type: ignore
    from astropy.time import Time  # type: ignore

    stem = ms_path.name[:-3] if ms_path.name.endswith(".ms") else ms_path.name
    parts = stem.split("_")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse observation time from MS name: {ms_path.name}")
    obs_time = Time(datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S"), scale="utc")
    try:
        location = EarthLocation.of_site(observatory)
    except Exception:
        location = None
    sun = get_body("sun", obs_time, location) if location is not None else get_body("sun", obs_time)
    ra_hms = sun.ra.to_string(unit=u.hourangle, sep=":", precision=3, pad=True)
    dec_dms = sun.dec.to_string(unit=u.deg, sep=".", precision=3, alwayssign=True, pad=True)
    ra_rad = float(sun.ra.to(u.rad).value)
    dec_rad = float(sun.dec.to(u.rad).value)
    return f"{ra_hms} {dec_dms}", ra_hms, dec_dms, f"J2000 {ra_rad:.16f}rad {dec_rad:.16f}rad"


def sun_center_ms(input_ms: Path, config: PipelineConfig, timings: dict[str, float]) -> Path:
    output_ms = input_ms.with_name(f"{input_ms.stem}_sun_centered.ms")
    if output_ms.exists():
        if config.reuse_workdir:
            print(f"[reuse] {output_ms}")
            return output_ms
        raise FileExistsError(f"Sun-centered MS already exists: {output_ms}")

    if config.dry_run:
        print(f"[dry-run] chgcentre to Sun: {input_ms} -> {output_ms}")
        return output_ms

    phasecenter, ra_hms, dec_dms, phasecenter_radians = compute_sun_phasecenter(input_ms, config.observatory)
    (input_ms.parent / "sun_phasecenter.txt").write_text(f"{phasecenter}\n{phasecenter_radians}\n")
    print(f"[sun-center] {input_ms.name} -> {output_ms.name} phasecenter={phasecenter!r}")
    start = time.perf_counter()
    shutil.copytree(input_ms, output_ms)
    run_command(["chgcentre", str(output_ms), ra_hms, dec_dms])
    timings["sun_centering_s"] = time.perf_counter() - start
    return output_ms


def image_with_wsclean(
    ms_path: Path,
    output_prefix: Path,
    config: PipelineConfig,
    pol: str,
    timings: dict[str, float],
) -> Path:
    options = WSCleanOptions(
        **{
            **config.wsclean.__dict__,
            "threads": config.threads,
            "pol": pol,
            "data_column": "DATA",
        }
    )
    start = time.perf_counter()
    result = run_wsclean(ms_path, output_prefix, options, dry_run=config.dry_run)
    if config.dry_run:
        print(f"[dry-run] {shlex_join(result)}")
    timings[f"wsclean_{pol.lower()}_s"] = time.perf_counter() - start
    return expected_image_fits(output_prefix, pol)


def process_band(target: BandTarget, config: PipelineConfig) -> BandResult:
    start_total = time.perf_counter()
    timings: dict[str, float] = {}
    products: dict[str, Path] = {}
    try:
        target.work_dir.mkdir(parents=True, exist_ok=True)
        work_ms = prepare_work_ms(target, config)
        products["work_ms"] = work_ms

        start = time.perf_counter()
        run_command(
            build_dp3_applycal_command(config, work_ms, target.caltable),
            dry_run=config.dry_run,
            extra_env=casarc_env(config),
        )
        timings["applycal_s"] = time.perf_counter() - start

        start = time.perf_counter()
        run_command(
            build_dp3_aoflagger_command(config, work_ms),
            dry_run=config.dry_run,
            extra_env=casarc_env(config),
        )
        timings["aoflagger_s"] = time.perf_counter() - start

        pre_mode_ms = run_average_channels(work_ms, config.column, config, timings, "average_before_mode_s")
        products["averaged_before_mode_ms"] = pre_mode_ms

        if config.run_ttcalsun:
            start = time.perf_counter()
            completed = run_command(
                build_ttcalsun_command(config, pre_mode_ms),
                dry_run=config.dry_run,
                capture=True,
            )
            timings[f"ttcalsun_{config.mode}_s"] = time.perf_counter() - start
            stdout = "" if completed is None else completed.stdout
            if not config.dry_run:
                (target.work_dir / f"ttcalsun_{config.mode}.log").write_text(stdout)

        post_mode_ms = run_average_channels(pre_mode_ms, "DATA", config, timings, "average_after_mode_s")
        products["averaged_after_mode_ms"] = post_mode_ms

        sun_ms = sun_center_ms(post_mode_ms, config, timings)
        products["sun_centered_ms"] = sun_ms

        image_dir = target.work_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for pol in config.image_pols:
            prefix = image_dir / f"{sun_ms.stem}_after_{config.mode}_sun_centered_{pol.lower()}"
            fits_path = image_with_wsclean(sun_ms, prefix, config, pol, timings)
            products[f"sun_centered_{pol.lower()}_fits"] = fits_path

        status = "ok"
        error = ""
    except Exception as exc:
        status = "failed"
        error = str(exc)
    return BandResult(
        freq_mhz=target.freq_mhz,
        status=status,
        work_dir=target.work_dir,
        elapsed_s=time.perf_counter() - start_total,
        products=products,
        timings=timings,
        error=error,
    )


def process_fullband(
    ms_dir: str | Path,
    caltables: Iterable[str | Path],
    config: PipelineConfig | None = None,
    *,
    jobs: int = 1,
    freqs: Iterable[int] | None = None,
    min_freq: int | None = 23,
    max_freq: int | None = 82,
    ms_pattern: str = "*.ms",
) -> list[BandResult]:
    cfg = config or PipelineConfig()
    ms_paths = discover_ms(ms_dir, pattern=ms_pattern, freqs=freqs, min_freq=min_freq, max_freq=max_freq)
    caltables_by_freq = match_caltables_by_freq([Path(path).expanduser().resolve() for path in caltables])
    targets = build_targets(ms_paths, caltables_by_freq, cfg.work_dir)

    print("Selected full-band jobs:")
    for target in targets:
        print(f"  {target.freq_mhz:>3} MHz  ms={target.src_ms.name}  caltable={target.caltable.name}")

    max_workers = max(1, min(jobs, len(targets)))
    results: list[BandResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(process_band, target, cfg): target for target in targets}
        while future_map:
            done, _ = wait(future_map, return_when=FIRST_COMPLETED)
            for future in done:
                target = future_map.pop(future)
                result = future.result()
                results.append(result)
                print(
                    f"[done] {target.freq_mhz:>3} MHz status={result.status} "
                    f"elapsed={result.elapsed_s:.2f}s"
                )
    write_summary(cfg.work_dir, results)
    return sorted(results, key=lambda item: item.freq_mhz)


def write_summary(work_dir: Path, results: Sequence[BandResult]) -> Path:
    path = work_dir / "preprocessing_and_imaging_summary.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "freq_mhz",
        "status",
        "elapsed_s",
        "applycal_s",
        "aoflagger_s",
        "average_before_mode_s",
        "ttcalsun_s",
        "average_after_mode_s",
        "sun_centering_s",
        "wsclean_i_s",
        "wsclean_v_s",
        "work_dir",
        "i_fits",
        "v_fits",
        "error",
    ]
    lines = ["\t".join(header)]
    for result in sorted(results, key=lambda item: item.freq_mhz):
        row = [
            str(result.freq_mhz),
            result.status,
            f"{result.elapsed_s:.2f}",
            _fmt_timing(result, "applycal_s"),
            _fmt_timing(result, "aoflagger_s"),
            _fmt_timing(result, "average_before_mode_s"),
            _fmt_ttcalsun_timing(result),
            _fmt_timing(result, "average_after_mode_s"),
            _fmt_timing(result, "sun_centering_s"),
            _fmt_timing(result, "wsclean_i_s"),
            _fmt_timing(result, "wsclean_v_s"),
            str(result.work_dir),
            str(result.products.get("sun_centered_i_fits", "")),
            str(result.products.get("sun_centered_v_fits", "")),
            result.error,
        ]
        lines.append("\t".join(row))
    path.write_text("\n".join(lines) + "\n")
    return path


def _fmt_timing(result: BandResult, key: str) -> str:
    if key not in result.timings:
        return ""
    return f"{result.timings[key]:.2f}"


def _fmt_ttcalsun_timing(result: BandResult) -> str:
    for key in sorted(result.timings):
        if key.startswith("ttcalsun_") and key.endswith("_s"):
            return f"{result.timings[key]:.2f}"
    return ""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run full-band LWA solar preprocessing through WSClean FITS products.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ms-dir", type=Path, required=True, help="Directory containing full-band *.ms inputs.")
    parser.add_argument("--caltable", type=Path, action="append", default=[], help="DP3 H5Parm caltable. Repeat for each band.")
    parser.add_argument("--caltable-list", type=Path, help="Text file containing one caltable path per line.")
    parser.add_argument("--caltable-dir", type=Path, help="Directory of DP3 H5Parm caltables.")
    parser.add_argument("--caltable-pattern", default="*.h5")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--freq", type=int, action="append", default=[], help="Frequency to run. Repeat for subsets.")
    parser.add_argument("--min-freq", type=int, default=23)
    parser.add_argument("--max-freq", type=int, default=82)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--threads", type=int, default=48)
    parser.add_argument("--dp3-bin", default=DEFAULT_DP3_BIN)
    parser.add_argument("--ttcalsun-bin", default=str(DEFAULT_TTCALSUN_BIN))
    parser.add_argument("--sources-json", type=Path, default=DEFAULT_SOURCES_JSON)
    parser.add_argument("--aoflagger-strategy", type=Path, default=DEFAULT_AOFLAGGER_STRATEGY)
    parser.add_argument("--mode", choices=["peel", "shave", "zest", "prune"], default="zest")
    parser.add_argument("--beam", default="lwa178")
    parser.add_argument("--avg-chanbin", type=int, default=4)
    parser.add_argument("--minuvw", type=float, default=30.0)
    parser.add_argument("--maxuvw", type=float)
    parser.add_argument("--maxiter", type=int, default=30)
    parser.add_argument("--tolerance", type=float, default=1e-4)
    parser.add_argument("--peeliter", type=int, default=3)
    parser.add_argument("--phase-only-maxiter", type=int, default=0)
    parser.add_argument("--observatory", default="OVRO")
    parser.add_argument("--wsclean-bin", default="wsclean")
    parser.add_argument("--image-size", type=int, default=512)
    parser.add_argument("--scale", default="1.5arcmin")
    parser.add_argument("--niter", type=int, default=5000)
    parser.add_argument("--weight", nargs=2, default=["briggs", "0"])
    parser.add_argument("--horizon-mask", default="10deg")
    parser.add_argument("--wsclean-mem-percent", type=int, default=15)
    parser.add_argument("--mgain", type=float, default=0.85)
    parser.add_argument("--auto-mask", type=float, default=6.0)
    parser.add_argument("--auto-threshold", type=float, default=1.0)
    parser.add_argument("--multiscale", action="store_true", default=True)
    parser.add_argument("--no-multiscale", action="store_false", dest="multiscale")
    parser.add_argument("--local-rms", action="store_true", default=True)
    parser.add_argument("--no-local-rms", action="store_false", dest="local_rms")
    parser.add_argument("--reuse-workdir", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-ttcalsun", action="store_true")
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        work_dir=args.work_dir.expanduser().resolve(),
        dp3_bin=args.dp3_bin,
        ttcalsun_bin=args.ttcalsun_bin,
        sources_json=args.sources_json.expanduser().resolve(),
        aoflagger_strategy=args.aoflagger_strategy.expanduser().resolve(),
        mode=args.mode,
        beam=args.beam,
        threads=args.threads,
        avg_chanbin=args.avg_chanbin,
        maxiter=args.maxiter,
        tolerance=args.tolerance,
        minuvw=args.minuvw,
        maxuvw=args.maxuvw,
        peeliter=args.peeliter,
        phase_only_maxiter=args.phase_only_maxiter,
        observatory=args.observatory,
        reuse_workdir=args.reuse_workdir,
        dry_run=args.dry_run,
        run_ttcalsun=not args.skip_ttcalsun,
        wsclean=WSCleanOptions(
            wsclean_bin=args.wsclean_bin,
            threads=args.threads,
            mem_percent=args.wsclean_mem_percent,
            size=args.image_size,
            scale=args.scale,
            data_column="DATA",
            pol="I",
            weight=tuple(args.weight),
            niter=args.niter,
            mgain=args.mgain,
            auto_mask=args.auto_mask,
            auto_threshold=args.auto_threshold,
            horizon_mask=args.horizon_mask,
            multiscale=args.multiscale,
            local_rms=args.local_rms,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    caltables = collect_caltables(
        args.caltable,
        caltable_list=args.caltable_list,
        caltable_dir=args.caltable_dir,
        pattern=args.caltable_pattern,
    )
    config = config_from_args(args)
    results = process_fullband(
        args.ms_dir,
        caltables,
        config,
        jobs=args.jobs,
        freqs=args.freq or None,
        min_freq=args.min_freq,
        max_freq=args.max_freq,
    )
    failures = [result for result in results if result.status != "ok"]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
