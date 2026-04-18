"""
Utilities for constructing and running WSClean imaging commands.

The interface intentionally keeps WSClean options as Python keyword arguments:
underscores are converted to command-line dashes, ``True`` means a flag option,
and ``False`` removes a default option. This mirrors the useful behavior of the
OVRO-LWA solar deconvolution wrapper while keeping commands as argument lists.
"""

from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence


LOGGER = logging.getLogger(__name__)


@dataclass
class WSCleanOptions:
    """Common WSClean options used by the LWA solar preprocessing pipeline."""

    wsclean_bin: str = "wsclean"
    threads: int = 8
    mem_percent: int = 15
    size: int = 512
    scale: str = "1.5arcmin"
    data_column: str = "DATA"
    pol: str = "I"
    weight: Sequence[str] = ("briggs", "0")
    niter: int = 5000
    mgain: float = 0.85
    auto_mask: float = 6.0
    auto_threshold: float = 1.0
    horizon_mask: str = "10deg"
    multiscale: bool = True
    multiscale_scale_bias: float = 0.7
    multiscale_max_scales: int = 6
    local_rms: bool = True
    taper_inner_tukey: str | None = None
    field: str | None = None
    intervals_out: int | None = None
    minuv_l: float | None = None
    no_reorder: bool = False
    no_dirty: bool = False
    no_update_model_required: bool = False
    no_negative: bool | None = None
    join_polarizations: bool | None = None
    quiet: bool = False
    extra_options: Mapping[str, object] = dataclass_field(default_factory=dict)


def find_smallest_fftw_size(n: float) -> int:
    """Return the smallest integer above ``n`` with only 2, 3, 5, and 7 factors."""

    if n <= 1:
        return 1
    max_a = int(math.ceil(math.log(n) / math.log(2))) + 1
    max_b = int(math.ceil(math.log(n) / math.log(3))) + 1
    max_c = int(math.ceil(math.log(n) / math.log(5))) + 1
    max_d = int(math.ceil(math.log(n) / math.log(7))) + 1

    smallest = None
    for a in range(max_a + 1):
        for b in range(max_b + 1):
            for c in range(max_c + 1):
                for d in range(max_d + 1):
                    value = (2**a) * (3**b) * (5**c) * (7**d)
                    if value > n and (smallest is None or value < smallest):
                        smallest = value
    return int(smallest if smallest is not None else math.ceil(n))


def auto_image_geometry(
    ms_path: str | Path,
    *,
    telescope_size_m: float = 3200.0,
    im_fov_arcsec: float = 182.0 * 3600.0,
    pix_scale_factor: float = 1.5,
) -> tuple[int, str]:
    """
    Estimate an FFT-friendly image size and pixel scale from the MS frequency.

    This is optional and imports ``casacore`` lazily so pure command-building
    paths do not require table access.
    """

    from casacore.tables import table  # type: ignore
    import numpy as np  # type: ignore

    spw_path = Path(ms_path) / "SPECTRAL_WINDOW"
    with table(str(spw_path), readonly=True, ack=False) as spw:
        chan_freq = np.asarray(spw.getcol("CHAN_FREQ"), dtype=float)
    freq_hz = float(np.nanmedian(chan_freq))
    scale_arcsec = 1.22 * (3.0e8 / freq_hz) / telescope_size_m
    scale_arcsec = scale_arcsec * 180.0 / math.pi * 3600.0 / pix_scale_factor
    size = find_smallest_fftw_size(im_fov_arcsec / scale_arcsec)
    return size, f"{scale_arcsec / 60.0}arcmin"


def _set_option(options: MutableMapping[str, object], key: str, value: object) -> None:
    if value is None:
        return
    if value is False:
        options.pop(key, None)
        return
    options[key] = "" if value is True else value


def _append_option(cmd: list[str], key: str, value: object) -> None:
    flag = "-" + key.replace("_", "-")
    if value == "":
        cmd.append(flag)
        return
    if isinstance(value, (list, tuple)):
        cmd.append(flag)
        cmd.extend(str(item) for item in value)
        return
    cmd.extend([flag, str(value)])


def build_wsclean_command(
    ms_path: str | Path,
    output_prefix: str | Path,
    options: WSCleanOptions | None = None,
    **extra_options: object,
) -> list[str]:
    """Build a WSClean command list without executing it."""

    opts = options or WSCleanOptions()
    values: dict[str, object] = {
        "j": opts.threads,
        "mem": opts.mem_percent,
        "mgain": opts.mgain,
        "niter": opts.niter,
        "auto_mask": opts.auto_mask,
        "auto_threshold": opts.auto_threshold,
        "weight": list(opts.weight),
        "horizon_mask": opts.horizon_mask,
        "pol": opts.pol,
        "size": [opts.size, opts.size],
        "scale": opts.scale,
        "data_column": opts.data_column,
    }
    _set_option(values, "multiscale", opts.multiscale)
    if opts.multiscale:
        values["multiscale_scale_bias"] = opts.multiscale_scale_bias
        values["multiscale_max_scales"] = opts.multiscale_max_scales
    _set_option(values, "local_rms", opts.local_rms)
    _set_option(values, "taper_inner_tukey", opts.taper_inner_tukey)
    _set_option(values, "field", opts.field)
    _set_option(values, "intervals_out", opts.intervals_out)
    _set_option(values, "minuv_l", opts.minuv_l)
    _set_option(values, "no_reorder", opts.no_reorder)
    _set_option(values, "no_dirty", opts.no_dirty)
    _set_option(values, "no_update_model_required", opts.no_update_model_required)
    _set_option(values, "quiet", opts.quiet)

    pols = {part.strip().upper() for part in opts.pol.split(",") if part.strip()}
    if opts.join_polarizations is None:
        join_polarizations = "I" in pols and bool(pols.intersection({"Q", "U", "V"}))
    else:
        join_polarizations = opts.join_polarizations
    _set_option(values, "join_polarizations", join_polarizations)

    if opts.no_negative is None:
        no_negative = pols in ({"I"}, {"XX"}, {"YY"}, {"XX", "YY"})
    else:
        no_negative = opts.no_negative
    _set_option(values, "no_negative", no_negative)

    for key, value in opts.extra_options.items():
        _set_option(values, key, value)
    for key, value in extra_options.items():
        _set_option(values, key, value)

    cmd = [opts.wsclean_bin]
    for key, value in values.items():
        _append_option(cmd, key, value)
    cmd.extend(["-name", str(output_prefix), str(ms_path)])
    return cmd


def run_wsclean(
    ms_path: str | Path,
    output_prefix: str | Path,
    options: WSCleanOptions | None = None,
    *,
    dry_run: bool = False,
    cwd: str | Path | None = None,
    check: bool = True,
    **extra_options: object,
) -> subprocess.CompletedProcess[str] | list[str]:
    """
    Run WSClean and return the completed process.

    In ``dry_run`` mode the command list is returned instead.
    """

    cmd = build_wsclean_command(ms_path, output_prefix, options, **extra_options)
    LOGGER.info("Running WSClean: %s", " ".join(cmd))
    if dry_run:
        return cmd
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
    )


def predict_model(
    ms_path: str | Path,
    image_prefix: str | Path,
    *,
    pol: str = "I",
    wsclean_bin: str = "wsclean",
    threads: int = 2,
    mem_percent: int = 2,
    field: str = "all",
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str] | list[str]:
    """Run WSClean prediction for an existing image model."""

    cmd = [
        wsclean_bin,
        "-j",
        str(threads),
        "-mem",
        str(mem_percent),
        "-no-reorder",
        "-predict",
        "-pol",
        pol,
        "-field",
        field,
        "-name",
        str(image_prefix),
        str(ms_path),
    ]
    LOGGER.info("Running WSClean predict: %s", " ".join(cmd))
    if dry_run:
        return cmd
    return subprocess.run(cmd, check=check, text=True)


def expected_image_fits(output_prefix: str | Path, pol: str = "I") -> Path:
    """
    Return the most common WSClean image FITS path for a single-pol run.

    For comma-separated multi-pol imaging, use ``expected_image_fits_paths``.
    """

    prefix = Path(output_prefix)
    if "," in pol:
        first_pol = pol.split(",", 1)[0].strip()
        return prefix.with_name(f"{prefix.name}-{first_pol}-image.fits")
    return prefix.with_name(f"{prefix.name}-image.fits")


def expected_image_fits_paths(output_prefix: str | Path, pol: str = "I") -> list[Path]:
    """Return expected WSClean image FITS paths for one or more polarizations."""

    prefix = Path(output_prefix)
    pols = [part.strip() for part in pol.split(",") if part.strip()]
    if len(pols) <= 1:
        return [expected_image_fits(prefix, pols[0] if pols else pol)]
    return [prefix.with_name(f"{prefix.name}-{part}-image.fits") for part in pols]
