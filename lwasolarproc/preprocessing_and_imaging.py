"""
Full-band LWA solar preprocessing and WSClean imaging.

This module ports the active test full-band workflow into package code:

1. copy each input Measurement Set into a per-band work directory
2. DP3 applycal from a matching H5Parm caltable
3. DP3 AOFlagger using ``LWA_sun_PZ.lua``
4. DP3 average by frequency
5. run a phase-only selfcal with WSClean model visibilities and DP3 gaincal
6. remove bright sources farther than the configured Sun exclusion radius
7. chgcentre to the Sun
8. DP3 average again
9. WSClean MFS Stokes I/V and optional fine-channel Stokes products
10. convert J2000 FITS products to helioprojective coordinates and combine them
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable as IterableABC
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

try:
    from .resources import aoflagger_strategy_path
    from .source_list import distance_to_src_list, get_sun_ra_dec, get_time_mjd, mask_far_sun_sources
    from .wsclean_helper import WSCleanOptions, expected_image_fits, expected_image_fits_paths, run_wsclean
except ImportError:  # pragma: no cover - supports direct script execution.
    from resources import aoflagger_strategy_path
    from source_list import distance_to_src_list, get_sun_ra_dec, get_time_mjd, mask_far_sun_sources
    from wsclean_helper import WSCleanOptions, expected_image_fits, expected_image_fits_paths, run_wsclean


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
ROOT = WORKSPACE_ROOT
DEFAULT_WORK_DIR = WORKSPACE_ROOT / "tests" / "_lwasolarproc_fullband"
DEFAULT_AOFLAGGER_STRATEGY = aoflagger_strategy_path()
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
    products: dict[str, object] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    error: str = ""


@dataclass
class PipelineConfig:
    work_dir: Path = DEFAULT_WORK_DIR
    dp3_bin: str = DEFAULT_DP3_BIN
    aoflagger_strategy: Path = DEFAULT_AOFLAGGER_STRATEGY
    casarc: Path | None = DEFAULT_CASARC
    threads: int = 18
    avg_chanbin: int = 4
    column: str = "CORRECTED_DATA"
    observatory: str = "OVRO"
    reuse_workdir: bool = False
    dry_run: bool = False
    copy_ms: bool = True
    run_phase_selfcal: bool = True
    selfcal_solint: int = 0
    selfcal_caltype: str = "diagonalphase"
    selfcal_uvlambdamin: float = 30.0
    selfcal_maxiter: int = 500
    selfcal_tolerance: float = 1e-5
    selfcal_use_model_column: bool = True
    selfcal_model_column: str = "MODEL_DATA"
    selfcal_image_size: int = 4096
    selfcal_scale: str = "2arcmin"
    selfcal_niter: int = 800
    selfcal_mgain: float = 0.9
    selfcal_horizon_mask: str = "5deg"
    selfcal_cleanup_images: bool = True
    run_bright_source_removal: bool = True
    bright_source_min_distance_deg: float = 6.0
    bright_source_image_size: int = 4096
    bright_source_scale: str = "2arcmin"
    bright_source_niter: int = 1500
    bright_source_mgain: float = 0.9
    bright_source_horizon_mask: str = "0.1deg"
    bright_source_cleanup_images: bool = True
    model_auto_pix_fov: bool = True
    model_auto_telescope_size_m: float = 2500.0
    model_auto_im_fov_arcsec: float = 182.0 * 3600.0
    model_auto_pix_scale_factor: float = 2.2
    model_auto_min_size: int | None = None
    mfs_pols: str = "I,V"
    run_fine_channel: bool = True
    fch_pols: str = "I"
    fch_channels_out: int = 12
    fch_deconvolution_channels: int | None = None
    fch_fit_spectral_pol: int | None = None
    postprocess_products: bool = True
    postprocess_cutout_size: int = 256
    postprocess_usebeam: str = "Memo178Beam"
    postprocess_beam_correction: bool = True
    postprocess_to_kelvin: bool = True
    plot_mfs_i: bool = True
    plot_mfs_v: bool = True
    plot_dpi: int = 150
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
    try:
        return subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=capture,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        if capture:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
        raise


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
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
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
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
        f"msin={ms_path}",
        "msin.datacolumn=CORRECTED_DATA",
        "msout=.",
        "msout.datacolumn=CORRECTED_DATA",
        "steps=[aoflag]",
        "aoflag.type=aoflagger",
        f"aoflag.strategy={config.aoflagger_strategy.resolve()}",
        "aoflag.keepstatistics=false",
    ]


def build_dp3_averager_command(
    config: PipelineConfig,
    input_ms: Path,
    output_ms: Path,
    input_column: str,
) -> list[str]:
    return [
        config.dp3_bin,
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
        f"msin={input_ms}",
        f"msin.datacolumn={input_column}",
        f"msout={output_ms}",
        "msout.overwrite=true",
        "steps=[avg]",
        "avg.type=averager",
        f"avg.freqstep={config.avg_chanbin}",
    ]


def build_dp3_subtract_sources_command(
    config: PipelineConfig,
    input_ms: Path,
    output_ms: Path,
    source_list: Path,
) -> list[str]:
    return [
        config.dp3_bin,
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
        f"msin={input_ms}",
        "msin.datacolumn=DATA",
        f"msout={output_ms}",
        "msout.overwrite=true",
        "steps=[predict]",
        "predict.type=predict",
        f"predict.sourcedb={source_list}",
        "predict.operation=subtract",
    ]


def build_dp3_gaincal_command(
    config: PipelineConfig,
    input_ms: Path,
    solution_path: Path,
) -> list[str]:
    return [
        config.dp3_bin,
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
        f"msin={input_ms}",
        "msin.datacolumn=DATA",
        "msout=.",
        "steps=[gaincal]",
        "gaincal.type=gaincal",
        f"gaincal.solint={config.selfcal_solint}",
        f"gaincal.caltype={config.selfcal_caltype}",
        f"gaincal.uvlambdamin={config.selfcal_uvlambdamin}",
        f"gaincal.maxiter={config.selfcal_maxiter}",
        f"gaincal.tolerance={config.selfcal_tolerance}",
        f"gaincal.usemodelcolumn={'true' if config.selfcal_use_model_column else 'false'}",
        f"gaincal.modelcolumn={config.selfcal_model_column}",
        f"gaincal.parmdb={solution_path}",
    ]


def build_dp3_phase_applycal_command(
    config: PipelineConfig,
    input_ms: Path,
    output_ms: Path,
    solution_path: Path,
) -> list[str]:
    return [
        config.dp3_bin,
        "verbosity=quiet",
        "showcounts=false",
        "showprogress=false",
        "showtimings=false",
        f"msin={input_ms}",
        "msin.datacolumn=DATA",
        f"msout={output_ms}",
        "msout.overwrite=true",
        "msout.datacolumn=DATA",
        "steps=[applycal]",
        "applycal.type=applycal",
        f"applycal.parmdb={solution_path}",
        "applycal.steps=[phase]",
        "applycal.phase.correction=phase000",
    ]


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


def phase_selfcal_ms_path(ms_path: Path) -> Path:
    return ms_path.with_name(f"{ms_path.stem}_phase_selfcal.ms")


def phase_selfcal_solution_path(ms_path: Path) -> Path:
    return ms_path.with_name(f"{ms_path.stem}_phase_selfcal.h5")


def phase_selfcal_wsclean_options(config: PipelineConfig) -> WSCleanOptions:
    return WSCleanOptions(
        wsclean_bin=config.wsclean.wsclean_bin,
        threads=config.threads,
        mem_percent=config.wsclean.mem_percent,
        size=config.selfcal_image_size,
        scale=config.selfcal_scale,
        data_column="DATA",
        pol="I",
        weight=("uniform",),
        niter=config.selfcal_niter,
        mgain=config.selfcal_mgain,
        auto_mask=None,
        auto_threshold=None,
        horizon_mask=config.selfcal_horizon_mask,
        minuv_l=config.wsclean.minuv_l,
        beam_fitting_size=config.wsclean.beam_fitting_size,
        field=None,
        intervals_out=1,
        no_reorder=config.wsclean.no_reorder,
        no_dirty=config.wsclean.no_dirty,
        no_update_model_required=config.wsclean.no_update_model_required,
        no_negative=True,
        quiet=config.wsclean.quiet,
        auto_pix_fov=config.model_auto_pix_fov,
        auto_telescope_size_m=config.model_auto_telescope_size_m,
        auto_im_fov_arcsec=config.model_auto_im_fov_arcsec,
        auto_pix_scale_factor=config.model_auto_pix_scale_factor,
        auto_min_size=config.model_auto_min_size,
    )


def run_phase_selfcal(
    input_ms: Path,
    config: PipelineConfig,
    timings: dict[str, float],
) -> tuple[Path, dict[str, Path]]:
    output_ms = phase_selfcal_ms_path(input_ms)
    if output_ms.exists():
        if config.reuse_workdir:
            print(f"[reuse] {output_ms}")
            return output_ms, {"phase_selfcal_ms": output_ms}
        raise FileExistsError(f"Phase-selfcal MS already exists: {output_ms}")

    solution_path = phase_selfcal_solution_path(input_ms)
    model_prefix = input_ms.parent / f"{input_ms.stem}_phase_selfcal_model"
    print(f"[phase-selfcal] {input_ms.name} -> {output_ms.name}")
    start = time.perf_counter()
    if not config.dry_run:
        clear_model_data_column(input_ms)

    options = phase_selfcal_wsclean_options(config)
    result = run_wsclean(input_ms, model_prefix, options, dry_run=config.dry_run)
    if config.dry_run:
        print(f"[dry-run] {shlex_join(result)}")

    run_command(
        build_dp3_gaincal_command(config, input_ms, solution_path),
        dry_run=config.dry_run,
        extra_env=casarc_env(config),
    )
    run_command(
        build_dp3_phase_applycal_command(config, input_ms, output_ms, solution_path),
        dry_run=config.dry_run,
        extra_env=casarc_env(config),
    )
    timings["phase_selfcal_s"] = time.perf_counter() - start

    if not config.dry_run and config.selfcal_cleanup_images:
        cleanup_wsclean_products(model_prefix, keep_suffixes=set())

    return output_ms, {
        "phase_selfcal_ms": output_ms,
        "phase_selfcal_solution": solution_path,
        "phase_selfcal_model_prefix": model_prefix,
    }


def source_removal_ms_path(ms_path: Path) -> Path:
    return ms_path.with_name(f"{ms_path.stem}_bright_sources_removed.ms")


def bright_source_wsclean_options(config: PipelineConfig) -> WSCleanOptions:
    return WSCleanOptions(
        wsclean_bin=config.wsclean.wsclean_bin,
        threads=config.threads,
        mem_percent=config.wsclean.mem_percent,
        size=config.bright_source_image_size,
        scale=config.bright_source_scale,
        data_column="DATA",
        pol="I",
        weight=("uniform",),
        niter=config.bright_source_niter,
        mgain=config.bright_source_mgain,
        auto_mask=8,
        auto_threshold=3,
        horizon_mask=config.bright_source_horizon_mask,
        minuv_l=config.wsclean.minuv_l,
        beam_fitting_size=config.wsclean.beam_fitting_size,
        field="all",
        intervals_out=1,
        no_reorder=config.wsclean.no_reorder,
        no_dirty=config.wsclean.no_dirty,
        no_update_model_required=config.wsclean.no_update_model_required,
        no_negative=True,
        quiet=config.wsclean.quiet,
        auto_pix_fov=config.model_auto_pix_fov,
        auto_telescope_size_m=config.model_auto_telescope_size_m,
        auto_im_fov_arcsec=config.model_auto_im_fov_arcsec,
        auto_pix_scale_factor=config.model_auto_pix_scale_factor,
        auto_min_size=config.model_auto_min_size,
    )


def cleanup_wsclean_products(output_prefix: Path, keep_suffixes: set[str]) -> None:
    for path in output_prefix.parent.glob(f"{output_prefix.name}-*"):
        if path.suffix == ".txt":
            continue
        if any(path.name.endswith(suffix) for suffix in keep_suffixes):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def clear_model_data_column(ms_path: Path, chunk_rows: int = 512) -> bool:
    """
    Zero the Measurement Set ``MODEL_DATA`` column in place if it exists.

    This is used around bright-source removal so stale or WSClean-generated
    model visibilities do not leak into DP3 prediction/subtraction.
    """
    from casacore.tables import table  # type: ignore

    with table(str(ms_path), readonly=False, ack=False) as tb:
        if "MODEL_DATA" not in tb.colnames():
            print(f"[model-column] {ms_path.name}: MODEL_DATA not present, skipping clear")
            return False

        nrows = tb.nrows()
        if nrows == 0:
            print(f"[model-column] {ms_path.name}: MODEL_DATA present but table is empty")
            return True

        sample = np.asarray(tb.getcol("MODEL_DATA", startrow=0, nrow=1))
        zero_shape = sample.shape[1:]
        dtype = sample.dtype

        for start_row in range(0, nrows, chunk_rows):
            nrow = min(chunk_rows, nrows - start_row)
            zeros = np.zeros((nrow,) + zero_shape, dtype=dtype)
            tb.putcol("MODEL_DATA", zeros, startrow=start_row, nrow=nrow)

    print(f"[model-column] {ms_path.name}: cleared MODEL_DATA")
    return True


def run_bright_source_removal(
    input_ms: Path,
    config: PipelineConfig,
    timings: dict[str, float],
) -> tuple[Path, dict[str, Path | int | float]]:
    output_ms = source_removal_ms_path(input_ms)
    if output_ms.exists():
        if config.reuse_workdir:
            print(f"[reuse] {output_ms}")
            return output_ms, {"bright_source_removed_ms": output_ms}
        raise FileExistsError(f"Bright-source-subtracted MS already exists: {output_ms}")

    output_prefix = input_ms.parent / f"{input_ms.stem}_bright_source_model"
    source_list = output_prefix.with_name(f"{output_prefix.name}-sources.txt")
    far_source_list = input_ms.parent / f"{input_ms.stem}_far_from_sun_sources.txt"
    source_distance_csv = input_ms.parent / f"{input_ms.stem}_source_distances.csv"

    print(
        f"[bright-source-removal] {input_ms.name} -> {output_ms.name} "
        f"exclude_sun_radius={config.bright_source_min_distance_deg:g}deg"
    )
    start = time.perf_counter()
    if not config.dry_run:
        clear_model_data_column(input_ms)
    options = bright_source_wsclean_options(config)
    result = run_wsclean(input_ms, output_prefix, options, dry_run=config.dry_run, save_source_list=True)
    if config.dry_run:
        print(f"[dry-run] {shlex_join(result)}")
        print(
            "[dry-run] bright source source-list masking and DP3 subtract: "
            f"{source_list} -> {far_source_list} -> {output_ms}"
        )
        timings["bright_source_removal_s"] = time.perf_counter() - start
        return output_ms, {
            "bright_source_model_prefix": output_prefix,
            "bright_source_source_list": source_list,
            "bright_source_far_source_list": far_source_list,
        }

    if not source_list.exists():
        raise FileNotFoundError(f"WSClean source list was not created: {source_list}")

    sun_ra, sun_dec = get_sun_ra_dec(get_time_mjd(input_ms), config.observatory)
    distances = sorted(
        mask_source_distances(source_list, source_distance_csv, sun_ra, sun_dec),
        key=lambda item: float(item["distance_deg"]),
    )
    mask_far_sun_sources(
        source_list,
        far_source_list,
        sun_ra,
        sun_dec,
        distance_deg=config.bright_source_min_distance_deg,
    )

    clear_model_data_column(input_ms)
    run_command(
        build_dp3_subtract_sources_command(config, input_ms, output_ms, far_source_list),
        dry_run=False,
        extra_env=casarc_env(config),
    )
    timings["bright_source_removal_s"] = time.perf_counter() - start

    if config.bright_source_cleanup_images:
        cleanup_wsclean_products(
            output_prefix,
            keep_suffixes={"-sources.txt"},
        )

    return output_ms, {
        "bright_source_removed_ms": output_ms,
        "bright_source_model_prefix": output_prefix,
        "bright_source_source_list": source_list,
        "bright_source_far_source_list": far_source_list,
        "bright_source_distance_table": source_distance_csv,
        "bright_source_all_count": len(distances),
        "bright_source_subtracted_count": count_source_rows(far_source_list),
        "sun_ra_deg": sun_ra,
        "sun_dec_deg": sun_dec,
    }


def mask_source_distances(source_list: Path, output_csv: Path, sun_ra: float, sun_dec: float) -> list[dict[str, object]]:
    distances = distance_to_src_list(source_list, sun_ra, sun_dec)
    lines = ["name,flux_jy,distance_deg,ra_deg,dec_deg"]
    for source in sorted(distances, key=lambda item: float(item["distance_deg"])):
        lines.append(
            f"{source['name']},{source.get('flux', 0.0)},{source['distance_deg']},"
            f"{source['ra_deg']},{source['dec_deg']}"
        )
    output_csv.write_text("\n".join(lines) + "\n")
    return distances


def count_source_rows(source_list: Path) -> int:
    with source_list.open("r", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


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


def make_wsclean_options(config: PipelineConfig, **overrides: object) -> WSCleanOptions:
    values = {**config.wsclean.__dict__, "threads": config.threads, "data_column": "DATA"}
    values.update(overrides)
    return WSCleanOptions(**values)


def run_mfs_wsclean(
    ms_path: Path,
    output_prefix: Path,
    config: PipelineConfig,
    timings: dict[str, float],
) -> dict[str, Path]:
    options = make_wsclean_options(config, pol=config.mfs_pols, channels_out=None)
    start = time.perf_counter()
    result = run_wsclean(ms_path, output_prefix, options, dry_run=config.dry_run)
    if config.dry_run:
        print(f"[dry-run] {shlex_join(result)}")
    timings["wsclean_mfs_s"] = time.perf_counter() - start
    pols = [part.strip().upper() for part in config.mfs_pols.split(",") if part.strip()]
    return {
        pol.lower(): path
        for pol, path in zip(pols, expected_image_fits_paths(output_prefix, config.mfs_pols), strict=False)
    }


def parse_pol_list(pols: str) -> list[str]:
    return [part.strip().upper() for part in pols.split(",") if part.strip()]


def collect_channel_image_fits(output_prefix: Path, pol: str, *, single_pol_run: bool) -> list[Path]:
    pol = pol.upper()
    patterns = [
        f"{output_prefix.name}-[0-9][0-9][0-9][0-9]-{pol}-image.fits",
        f"{output_prefix.name}-{pol}-[0-9][0-9][0-9][0-9]-image.fits",
    ]
    if single_pol_run:
        patterns.append(f"{output_prefix.name}-[0-9][0-9][0-9][0-9]-image.fits")

    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(output_prefix.parent.glob(pattern))
    single = expected_image_fits(output_prefix, pol)
    if single.exists():
        candidates.append(single)
    return sorted({path for path in candidates if "-MFS-" not in path.name})


def run_fine_channel_wsclean(
    ms_path: Path,
    output_prefix: Path,
    config: PipelineConfig,
    timings: dict[str, float],
) -> dict[str, list[Path]]:
    extra_options = dict(config.wsclean.extra_options)
    if config.fch_deconvolution_channels:
        extra_options["deconvolution_channels"] = config.fch_deconvolution_channels
        if config.fch_fit_spectral_pol:
            extra_options["join_channels"] = True
            extra_options["fit_spectral_pol"] = config.fch_fit_spectral_pol

    options = make_wsclean_options(
        config,
        pol=config.fch_pols,
        channels_out=config.fch_channels_out,
        extra_options=extra_options,
    )
    start = time.perf_counter()
    result = run_wsclean(ms_path, output_prefix, options, dry_run=config.dry_run)
    if config.dry_run:
        print(f"[dry-run] {shlex_join(result)}")
    timings["wsclean_fch_i_s"] = time.perf_counter() - start
    pols = parse_pol_list(config.fch_pols)
    if config.dry_run:
        return {pol.lower(): [expected_image_fits(output_prefix, pol)] for pol in pols}

    products: dict[str, list[Path]] = {}
    single_pol_run = len(pols) <= 1
    for pol in pols:
        paths = collect_channel_image_fits(output_prefix, pol, single_pol_run=single_pol_run)
        if not paths:
            raise FileNotFoundError(f"No fine-channel WSClean FITS products found for prefix {output_prefix} pol={pol}")
        products[pol.lower()] = paths
    return products


def process_band(target: BandTarget, config: PipelineConfig) -> BandResult:
    start_total = time.perf_counter()
    timings: dict[str, float] = {}
    products: dict[str, object] = {}
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

        pre_selfcal_ms = run_average_channels(work_ms, config.column, config, timings, "average_before_selfcal_s")
        products["averaged_before_selfcal_ms"] = pre_selfcal_ms

        calibrated_ms = pre_selfcal_ms
        if config.run_phase_selfcal:
            calibrated_ms, phase_selfcal_products = run_phase_selfcal(pre_selfcal_ms, config, timings)
            products.update(phase_selfcal_products)

        source_removed_ms = calibrated_ms
        if config.run_bright_source_removal:
            source_removed_ms, source_removal_products = run_bright_source_removal(calibrated_ms, config, timings)
            products.update(source_removal_products)

        sun_ms = sun_center_ms(source_removed_ms, config, timings)
        products["sun_centered_ms"] = sun_ms

        post_selfcal_ms = run_average_channels(sun_ms, "DATA", config, timings, "average_after_selfcal_s")
        products["averaged_after_selfcal_ms"] = post_selfcal_ms

        image_dir = target.work_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        mode_label = "phase_selfcal" if config.run_phase_selfcal else "no_selfcal"
        mfs_prefix = image_dir / f"{post_selfcal_ms.stem}_after_{mode_label}_sun_centered_mfs"
        mfs_fits = run_mfs_wsclean(post_selfcal_ms, mfs_prefix, config, timings)
        for pol, fits_path in mfs_fits.items():
            products[f"mfs_{pol}_fits"] = fits_path

        if config.run_fine_channel:
            fch_label = "_".join(pol.lower() for pol in parse_pol_list(config.fch_pols)) or "i"
            fch_prefix = image_dir / f"{post_selfcal_ms.stem}_after_{mode_label}_sun_centered_fch_{fch_label}"
            fch_fits = run_fine_channel_wsclean(post_selfcal_ms, fch_prefix, config, timings)
            for pol, fits_paths in fch_fits.items():
                products[f"fch_{pol}_fits"] = fits_paths

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


def centered_subregion(fits_path: Path, cutout_size: int) -> list[int] | None:
    if cutout_size <= 0:
        return None
    from astropy.io import fits  # type: ignore

    with fits.open(fits_path) as hdul:
        ny, nx = hdul[0].data.shape[-2:]
    if cutout_size > nx or cutout_size > ny:
        raise ValueError(f"Cutout size {cutout_size} exceeds image shape {(ny, nx)} for {fits_path}")
    xmin = (nx - cutout_size) // 2
    ymin = (ny - cutout_size) // 2
    return [xmin, xmin + cutout_size, ymin, ymin + cutout_size]


def _as_paths(value: object) -> list[Path]:
    if value is None or value == "":
        return []
    if isinstance(value, Path):
        return [value]
    if isinstance(value, str):
        return [Path(value)]
    if isinstance(value, IterableABC):
        return [Path(item) for item in value]
    return []


def _import_postprocess_helpers():
    try:
        from . import ndfits
        from .coords import fitsj2000tohelio
    except ImportError:  # pragma: no cover - supports direct script execution.
        import ndfits  # type: ignore
        from coords import fitsj2000tohelio  # type: ignore
    return fitsj2000tohelio, ndfits


def convert_and_combine_fits(
    source_fits: Sequence[Path],
    *,
    label: str,
    output_dir: Path,
    config: PipelineConfig,
) -> Path | None:
    existing = [path for path in source_fits if path.exists()]
    if not existing:
        print(f"[postprocess] skip {label}: no FITS files")
        return None

    fitsj2000tohelio, ndfits = _import_postprocess_helpers()
    helio_dir = output_dir / "helio" / label
    helio_dir.mkdir(parents=True, exist_ok=True)
    converted: list[str] = []
    for index, src in enumerate(sorted(existing), start=1):
        out = helio_dir / f"{index:04d}_{src.stem}.helio.fits"
        subregion = centered_subregion(src, config.postprocess_cutout_size)
        fitsj2000tohelio(
            str(src),
            str(out),
            toK=config.postprocess_to_kelvin,
            verbose=False,
            subregion=subregion,
            usebeam=config.postprocess_usebeam,
            beam_correction=config.postprocess_beam_correction,
        )
        converted.append(str(out))

    combined_path = output_dir / f"combined_{label}.fits"
    if combined_path.exists():
        combined_path.unlink()
    if len(converted) == 1:
        shutil.copyfile(converted[0], combined_path)
        wrapped = str(combined_path)
    else:
        wrapped = ndfits.wrap(converted, outfitsfile=str(combined_path), docompress=False, observatory="OVRO-LWA")
    print(f"[postprocess] {label}: converted={len(converted)} combined={wrapped}")
    return Path(wrapped)


def postprocess_fullband_products(config: PipelineConfig, results: Sequence[BandResult]) -> dict[str, Path]:
    if not config.postprocess_products:
        return {}
    if config.dry_run:
        print("[postprocess] skip: dry-run")
        return {}

    ok_results = [result for result in sorted(results, key=lambda item: item.freq_mhz) if result.status == "ok"]
    groups = {
        "mfs_I": [path for result in ok_results for path in _as_paths(result.products.get("mfs_i_fits"))],
        "mfs_V": [path for result in ok_results for path in _as_paths(result.products.get("mfs_v_fits"))],
        "fch_I": [path for result in ok_results for path in _as_paths(result.products.get("fch_i_fits"))],
        "fch_V": [path for result in ok_results for path in _as_paths(result.products.get("fch_v_fits"))],
    }

    output_dir = config.work_dir / "combined"
    output_dir.mkdir(parents=True, exist_ok=True)
    combined: dict[str, Path] = {}
    for label, paths in groups.items():
        combined_path = convert_and_combine_fits(paths, label=label, output_dir=output_dir, config=config)
        if combined_path is not None:
            combined[label] = combined_path
    plot_outputs: dict[str, Path] = {}
    mfs_i_plot = plot_mfs_i_default(config, combined)
    if mfs_i_plot is not None:
        plot_outputs["mfs_I_plot"] = mfs_i_plot
    mfs_v_plot = plot_mfs_v_default(config, combined)
    if mfs_v_plot is not None:
        plot_outputs["mfs_V_plot"] = mfs_v_plot
    write_combined_summary(output_dir, {**combined, **plot_outputs})
    return combined


def plot_mfs_i_default(config: PipelineConfig, combined: Mapping[str, Path]) -> Path | None:
    if not config.plot_mfs_i:
        return None
    mfs_i = combined.get("mfs_I")
    if mfs_i is None:
        print("[plot] skip mfs_I: combined FITS not found")
        return None

    import matplotlib  # type: ignore

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # type: ignore

    try:
        from .visualization import slow_pipeline_default_plot
    except ImportError:  # pragma: no cover - supports direct script execution.
        from visualization import slow_pipeline_default_plot  # type: ignore

    plot_path = mfs_i.with_name(f"{mfs_i.stem}.default_plot.png")
    fig, _ = slow_pipeline_default_plot(str(mfs_i))
    fig.savefig(plot_path, dpi=config.plot_dpi)
    plt.close(fig)
    print(f"[plot] mfs_I default plot={plot_path}")
    return plot_path


def plot_mfs_v_default(config: PipelineConfig, combined: Mapping[str, Path]) -> Path | None:
    if not config.plot_mfs_v:
        return None
    mfs_i = combined.get("mfs_I")
    mfs_v = combined.get("mfs_V")
    if mfs_i is None or mfs_v is None:
        print("[plot] skip mfs_V: combined I/V FITS not found")
        return None

    import matplotlib  # type: ignore

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # type: ignore

    try:
        from .visualization import slow_pipeline_default_polarization_plot
    except ImportError:  # pragma: no cover - supports direct script execution.
        from visualization import slow_pipeline_default_polarization_plot  # type: ignore

    plot_path = mfs_v.with_name("combined_mfs_V.default_plot.png")
    fig, _ = slow_pipeline_default_polarization_plot(str(mfs_i), str(mfs_v))
    fig.savefig(plot_path, dpi=config.plot_dpi)
    plt.close(fig)
    print(f"[plot] mfs_V default plot={plot_path}")
    return plot_path


def write_combined_summary(output_dir: Path, combined: Mapping[str, Path]) -> Path:
    path = output_dir / "combined_products.tsv"
    lines = ["product\tpath"]
    for label, product_path in sorted(combined.items()):
        lines.append(f"{label}\t{product_path}")
    path.write_text("\n".join(lines) + "\n")
    return path


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
    sorted_results = sorted(results, key=lambda item: item.freq_mhz)
    write_summary(cfg.work_dir, sorted_results)
    postprocess_fullband_products(cfg, sorted_results)
    return sorted_results


def write_summary(work_dir: Path, results: Sequence[BandResult]) -> Path:
    path = work_dir / "preprocessing_and_imaging_summary.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "freq_mhz",
        "status",
        "elapsed_s",
        "applycal_s",
        "aoflagger_s",
        "average_before_selfcal_s",
        "phase_selfcal_s",
        "bright_source_removal_s",
        "sun_centering_s",
        "average_after_selfcal_s",
        "wsclean_mfs_s",
        "wsclean_fch_i_s",
        "bright_source_all_count",
        "bright_source_subtracted_count",
        "work_dir",
        "phase_selfcal_ms",
        "phase_selfcal_solution",
        "bright_source_removed_ms",
        "mfs_i_fits",
        "mfs_v_fits",
        "fch_i_fits",
        "fch_i_fits_count",
        "fch_v_fits",
        "fch_v_fits_count",
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
            _fmt_timing(result, "average_before_selfcal_s"),
            _fmt_timing(result, "phase_selfcal_s"),
            _fmt_timing(result, "bright_source_removal_s"),
            _fmt_timing(result, "sun_centering_s"),
            _fmt_timing(result, "average_after_selfcal_s"),
            _fmt_timing(result, "wsclean_mfs_s"),
            _fmt_timing(result, "wsclean_fch_i_s"),
            str(result.products.get("bright_source_all_count", "")),
            str(result.products.get("bright_source_subtracted_count", "")),
            str(result.work_dir),
            _fmt_product(result.products.get("phase_selfcal_ms")),
            _fmt_product(result.products.get("phase_selfcal_solution")),
            _fmt_product(result.products.get("bright_source_removed_ms")),
            _fmt_product(result.products.get("mfs_i_fits")),
            _fmt_product(result.products.get("mfs_v_fits")),
            _fmt_product(result.products.get("fch_i_fits")),
            str(len(_as_paths(result.products.get("fch_i_fits")))),
            _fmt_product(result.products.get("fch_v_fits")),
            str(len(_as_paths(result.products.get("fch_v_fits")))),
            result.error,
        ]
        lines.append("\t".join(row))
    path.write_text("\n".join(lines) + "\n")
    return path


def _fmt_timing(result: BandResult, key: str) -> str:
    if key not in result.timings:
        return ""
    return f"{result.timings[key]:.2f}"


def _fmt_product(value: object) -> str:
    return ";".join(str(path) for path in _as_paths(value))


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
    parser.add_argument("--threads", type=int, default=18)
    parser.add_argument("--dp3-bin", default=DEFAULT_DP3_BIN)
    parser.add_argument("--aoflagger-strategy", type=Path, default=DEFAULT_AOFLAGGER_STRATEGY)
    parser.add_argument("--avg-chanbin", type=int, default=4)
    parser.add_argument("--observatory", default="OVRO")
    parser.add_argument("--no-phase-selfcal", action="store_true", help="Skip WSClean+DP3 phase selfcal after the first avg4ch step.")
    parser.add_argument("--selfcal-solint", type=int, default=0)
    parser.add_argument("--selfcal-caltype", default="diagonalphase")
    parser.add_argument("--selfcal-uvlambdamin", type=float, default=30.0)
    parser.add_argument("--selfcal-maxiter", type=int, default=500)
    parser.add_argument("--selfcal-tolerance", type=float, default=1e-5)
    parser.add_argument("--selfcal-image-size", type=int, default=4096)
    parser.add_argument("--selfcal-scale", default="2arcmin")
    parser.add_argument("--selfcal-niter", type=int, default=800)
    parser.add_argument("--selfcal-mgain", type=float, default=0.9)
    parser.add_argument("--selfcal-horizon-mask", default="5deg")
    parser.add_argument("--keep-selfcal-images", action="store_true", help="Keep temporary full-sky WSClean products used for phase selfcal.")
    parser.add_argument(
        "--no-bright-source-removal",
        action="store_true",
        help="Skip WSClean source-list based subtraction of bright sources away from the Sun.",
    )
    parser.add_argument(
        "--bright-source-min-distance-deg",
        type=float,
        default=6.0,
        help="Only subtract WSClean components farther than this angular distance from the Sun.",
    )
    parser.add_argument(
        "--bright-source-image-size",
        type=int,
        default=4096,
        help="Full-sky WSClean image size used only to build the source list for subtraction.",
    )
    parser.add_argument(
        "--bright-source-scale",
        default="2arcmin",
        help="Full-sky WSClean pixel scale used only to build the source list for subtraction.",
    )
    parser.add_argument(
        "--bright-source-niter",
        type=int,
        default=1500,
        help="Full-sky WSClean niter used only to build the source list for subtraction.",
    )
    parser.add_argument(
        "--bright-source-mgain",
        type=float,
        default=0.9,
        help="Full-sky WSClean mgain used only to build the source list for subtraction.",
    )
    parser.add_argument(
        "--bright-source-horizon-mask",
        default="0.1deg",
        help="Full-sky WSClean horizon mask used only to build the source list for subtraction.",
    )
    parser.add_argument(
        "--keep-bright-source-images",
        action="store_true",
        help="Keep temporary full-sky WSClean FITS/model products from the source-removal step.",
    )
    parser.add_argument("--wsclean-bin", default="wsclean")
    parser.add_argument("--image-size", type=int, default=384)
    parser.add_argument("--scale", default="1.8arcmin")
    parser.add_argument(
        "--model-auto-pix-fov",
        "--auto-pix-fov",
        dest="model_auto_pix_fov",
        action="store_true",
        default=True,
        help="Use frequency-dependent WSClean geometry for selfcal and bright-source model images.",
    )
    parser.add_argument(
        "--no-model-auto-pix-fov",
        "--no-auto-pix-fov",
        dest="model_auto_pix_fov",
        action="store_false",
        help="Use fixed model image geometry for selfcal and bright-source removal.",
    )
    parser.add_argument(
        "--model-auto-im-fov-deg",
        "--auto-im-fov-deg",
        dest="model_auto_im_fov_deg",
        type=float,
        default=182.0,
        help="Target image field of view for selfcal and bright-source auto geometry.",
    )
    parser.add_argument(
        "--model-auto-telescope-size-m",
        dest="model_auto_telescope_size_m",
        type=float,
        default=2500.0,
        help="Effective telescope size in meters for selfcal and bright-source auto geometry.",
    )
    parser.add_argument(
        "--model-auto-pix-scale-factor",
        "--auto-pix-scale-factor",
        dest="model_auto_pix_scale_factor",
        type=float,
        default=2.2,
        help="Pixels per synthesized beam factor used by selfcal and bright-source auto geometry.",
    )
    parser.add_argument(
        "--model-auto-min-size",
        "--auto-min-size",
        dest="model_auto_min_size",
        type=int,
        default=None,
        help="Optional minimum FFT-friendly image size for selfcal and bright-source auto geometry.",
    )
    parser.add_argument("--niter", type=int, default=10000)
    parser.add_argument("--weight", nargs=2, default=["briggs", "-0.5"])
    parser.add_argument("--horizon-mask", default="5deg")
    parser.add_argument("--wsclean-mem-percent", type=int, default=8)
    parser.add_argument("--mgain", type=float, default=0.8)
    parser.add_argument("--auto-mask", type=float, default=None)
    parser.add_argument("--auto-threshold", type=float, default=3.0)
    parser.add_argument("--minuv-l", type=float, default=10.0)
    parser.add_argument("--beam-fitting-size", type=int, default=2)
    parser.add_argument("--mfs-pols", default="I,V", help="Comma-separated polarizations for the MFS WSClean pass.")
    parser.add_argument("--no-fine-channel", action="store_true", help="Skip the fine-channel WSClean pass.")
    parser.add_argument("--fch-pols", default="I", help="Comma-separated polarizations for the fine-channel WSClean pass, for example I or I,V.")
    parser.add_argument("--fch-channels-out", type=int, default=12, help="WSClean channels-out for the fine-channel pass.")
    parser.add_argument(
        "--fch-deconvolution-channels",
        type=int,
        default=None,
        help="Optional WSClean deconvolution-channels value for the fine-channel pass. Disabled by default.",
    )
    parser.add_argument(
        "--fch-fit-spectral-pol",
        type=int,
        default=None,
        help="Optional WSClean fit-spectral-pol value used only when deconvolution-channels is enabled.",
    )
    parser.add_argument("--no-postprocess", action="store_true", help="Skip J2000-to-helio conversion and combined FITS products.")
    parser.add_argument("--postprocess-cutout-size", type=int, default=256, help="Centered square cutout size. Use 0 for full images.")
    parser.add_argument("--postprocess-usebeam", default="Memo178Beam")
    parser.add_argument("--no-postprocess-beam-correction", action="store_true")
    parser.add_argument("--no-postprocess-kelvin", action="store_true")
    parser.add_argument("--no-plot-mfs-i", action="store_true", help="Skip default visualization for combined_mfs_I.fits.")
    parser.add_argument("--plot-dpi", type=int, default=150)
    parser.add_argument("--multiscale", action="store_true", default=False)
    parser.add_argument("--no-multiscale", action="store_false", dest="multiscale")
    parser.add_argument("--local-rms", action="store_true", default=False)
    parser.add_argument("--no-local-rms", action="store_false", dest="local_rms")
    parser.add_argument("--reuse-workdir", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        work_dir=args.work_dir.expanduser().resolve(),
        dp3_bin=args.dp3_bin,
        aoflagger_strategy=args.aoflagger_strategy.expanduser().resolve(),
        threads=args.threads,
        avg_chanbin=args.avg_chanbin,
        observatory=args.observatory,
        reuse_workdir=args.reuse_workdir,
        dry_run=args.dry_run,
        run_phase_selfcal=not args.no_phase_selfcal,
        selfcal_solint=args.selfcal_solint,
        selfcal_caltype=args.selfcal_caltype,
        selfcal_uvlambdamin=args.selfcal_uvlambdamin,
        selfcal_maxiter=args.selfcal_maxiter,
        selfcal_tolerance=args.selfcal_tolerance,
        selfcal_image_size=args.selfcal_image_size,
        selfcal_scale=args.selfcal_scale,
        selfcal_niter=args.selfcal_niter,
        selfcal_mgain=args.selfcal_mgain,
        selfcal_horizon_mask=args.selfcal_horizon_mask,
        selfcal_cleanup_images=not args.keep_selfcal_images,
        run_bright_source_removal=not args.no_bright_source_removal,
        bright_source_min_distance_deg=args.bright_source_min_distance_deg,
        bright_source_image_size=args.bright_source_image_size,
        bright_source_scale=args.bright_source_scale,
        bright_source_niter=args.bright_source_niter,
        bright_source_mgain=args.bright_source_mgain,
        bright_source_horizon_mask=args.bright_source_horizon_mask,
        bright_source_cleanup_images=not args.keep_bright_source_images,
        model_auto_pix_fov=args.model_auto_pix_fov,
        model_auto_telescope_size_m=args.model_auto_telescope_size_m,
        model_auto_im_fov_arcsec=args.model_auto_im_fov_deg * 3600.0,
        model_auto_pix_scale_factor=args.model_auto_pix_scale_factor,
        model_auto_min_size=args.model_auto_min_size,
        mfs_pols=args.mfs_pols,
        run_fine_channel=not args.no_fine_channel,
        fch_pols=args.fch_pols,
        fch_channels_out=args.fch_channels_out,
        fch_deconvolution_channels=args.fch_deconvolution_channels,
        fch_fit_spectral_pol=args.fch_fit_spectral_pol,
        postprocess_products=not args.no_postprocess,
        postprocess_cutout_size=args.postprocess_cutout_size,
        postprocess_usebeam=args.postprocess_usebeam,
        postprocess_beam_correction=not args.no_postprocess_beam_correction,
        postprocess_to_kelvin=not args.no_postprocess_kelvin,
        plot_mfs_i=not args.no_plot_mfs_i,
        plot_dpi=args.plot_dpi,
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
            minuv_l=args.minuv_l,
            beam_fitting_size=args.beam_fitting_size,
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
