"""Visualization helpers for OVRO-LWA solar products.

This module mirrors the useful plotting surface from ``ovrolwasolar`` while
avoiding the CASA and suncasa runtime dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
from astropy.io import fits
from astropy.time import Time
from matplotlib import gridspec
from matplotlib.patches import Ellipse
from sunpy import map as smap


OVRO_LWA_LOCATION = EarthLocation(lat=37.23977727 * u.deg, lon=-118.2816667 * u.deg, height=1183 * u.m)


def _header_get(header, key: str, default=None):
    if hasattr(header, "get"):
        value = header.get(key)
        if value is not None:
            return value
        upper_value = header.get(key.upper())
        if upper_value is not None:
            return upper_value
        return default
    return default


def get_solar_altaz_multiple_times(times, location: EarthLocation = OVRO_LWA_LOCATION):
    """Return solar azimuth and altitude in degrees for one or more times."""
    obstime = Time(times)
    solar_altaz = get_sun(obstime).transform_to(AltAz(obstime=obstime, location=location))
    return solar_altaz.az.to_value(u.deg), solar_altaz.alt.to_value(u.deg)


def _row_flag_fraction(flag_data: np.ndarray, row_idx: int, nrows: int) -> float:
    if flag_data.shape[0] == nrows:
        return float(np.mean(np.abs(flag_data[row_idx])))
    if flag_data.shape[-1] == nrows:
        return float(np.mean(np.abs(flag_data[..., row_idx])))
    return float(np.mean(np.abs(flag_data)))


def inspection_bl_flag(ms_file: str | Path):
    """
    Plot the baseline flagging fraction for a Measurement Set.

    This uses ``casacore.tables`` lazily instead of CASA's ``casatools``.
    """
    from casacore.tables import table  # type: ignore

    ms_path = str(ms_file)
    with table(ms_path, readonly=True, ack=False) as tb:
        ant1 = tb.getcol("ANTENNA1")
        ant2 = tb.getcol("ANTENNA2")
        flag_data = tb.getcol("FLAG")

    nants = int(max(np.max(ant1), np.max(ant2))) + 1
    img_cross = np.zeros((nants, nants))
    nrows = len(ant1)
    for idx in range(nrows):
        flag_fraction = _row_flag_fraction(flag_data, idx, nrows)
        img_cross[ant1[idx], ant2[idx]] = flag_fraction
        img_cross[ant2[idx], ant1[idx]] = flag_fraction

    return plt.imshow(img_cross, cmap="viridis", origin="lower", norm=mcolors.PowerNorm(0.5))


def _safe_channel(meta: dict, name: str, idx: int, default: float) -> float:
    arr = meta.get(name)
    if arr is None:
        return default
    try:
        return float(arr[idx])
    except (IndexError, TypeError, ValueError):
        return default


def _extent_from_fov(fov: float) -> list[float]:
    half = fov / 2.0
    return [-half, half, -half, half]


def _set_sunpy_map_fov(ax, solar_map, fov: float, x_shift: float = 0.0, y_shift: float = 0.0) -> None:
    half = fov / 2.0
    bottom_left = SkyCoord((-half - x_shift) * u.arcsec, (-half - y_shift) * u.arcsec, frame=solar_map.coordinate_frame)
    top_right = SkyCoord((half - x_shift) * u.arcsec, (half - y_shift) * u.arcsec, frame=solar_map.coordinate_frame)
    x0, y0 = solar_map.world_to_pixel(bottom_left)
    x1, y1 = solar_map.world_to_pixel(top_right)
    limits = [x0.value, x1.value, y0.value, y1.value]
    if np.all(np.isfinite(limits)):
        ax.set_xlim(sorted([x0.value, x1.value]))
        ax.set_ylim(sorted([y0.value, y1.value]))


def _hide_wcs_axis_labels(ax, panel_index: int) -> None:
    ax.coords[0].set_format_unit(u.arcsec)
    ax.coords[1].set_format_unit(u.arcsec)
    ax.coords[0].set_axislabel("")
    ax.coords[1].set_axislabel("")
    if panel_index not in [8, 9, 10, 11]:
        ax.coords[0].set_ticklabel_visible(False)
        ax.coords[0].set_ticks_visible(False)
    if panel_index not in [0, 4, 8]:
        ax.coords[1].set_ticklabel_visible(False)
        ax.coords[1].set_ticks_visible(False)


def slow_pipeline_default_plot(
    fname,
    freqs_plt: Sequence[float] = (34.1, 38.7, 43.2, 47.8, 52.4, 57.0, 61.6, 66.2, 70.8, 75.4, 80.0, 84.5),
    fov: float = 7998,
    add_logo: bool = True,
    apply_refraction_param: bool = False,
    spec_fits=None,
    spec_dur: float = 600.0,
    spec_cmap: str = "viridis",
    spec_vmin=None,
    spec_vmax=None,
    spec_norm: str = "log",
    spec_frange: Sequence[float] = (30.0, 88.0),
    apply_fiducial_primary_beam: bool = False,
    badants_arr=None,
):
    """
    Plot the default 12-panel slow-pipeline FITS product.

    ``spec_fits`` is intentionally ignored here. The lwasolarproc copy assumes
    the spectrogram panel is disabled, so it does not import ``suncasa.dspec``.
    """
    from . import ndfits

    del add_logo, spec_fits, spec_dur, spec_cmap, spec_vmin, spec_vmax, spec_norm, spec_frange

    meta, rdata = ndfits.read(fname)
    header = meta.get("header", {})

    if not apply_fiducial_primary_beam:
        obstime = Time(_header_get(header, "DATE-OBS"))
        _, alt = get_solar_altaz_multiple_times(obstime)
        beam_gain = np.sin(np.deg2rad(alt)) ** 1.6
        if np.all(np.isfinite(beam_gain)) and np.all(beam_gain != 0):
            rdata[0, ...] /= beam_gain

    date_obs = _header_get(header, "DATE-OBS", "")
    rfrcor = bool(_header_get(header, "REFCOR", False) or _header_get(header, "rfrcor", False))

    fig = plt.figure(figsize=(8, 6.5))
    gs = gridspec.GridSpec(3, 4, left=0.07, right=0.98, top=0.94, bottom=0.10, wspace=0.02, hspace=0.02)

    freqs_mhz = np.asarray(meta["ref_cfreqs"], dtype=float) / 1e6
    do_badants_label = badants_arr is not None and len(badants_arr) == len(freqs_mhz)
    badants_arr = np.asarray(badants_arr) if do_badants_label else None

    axes = []
    for i in range(12):
        if np.min(np.abs(freqs_mhz - freqs_plt[i])) < 2.0:
            bd = int(np.argmin(np.abs(freqs_mhz - freqs_plt[i])))
            image = np.squeeze(rdata[0, bd, :, :] / 1e6)
            solar_map = smap.Map(image, header.copy())
            ax = fig.add_subplot(gs[i], projection=solar_map)
            ax.set_facecolor("black")
            ax.text(0.02, 0.98, f"{freqs_plt[i]:.0f} MHz", color="w", ha="left", va="top", fontsize=11, transform=ax.transAxes)

            if apply_refraction_param and not rfrcor:
                x_shift = _safe_channel(meta, "refra_shift_x", bd, 0.0)
                y_shift = _safe_channel(meta, "refra_shift_y", bd, 0.0)
            else:
                x_shift = 0.0
                y_shift = 0.0

            vmaxplt = np.nanpercentile(image, 99.9)
            if not np.isfinite(vmaxplt):
                vmaxplt = None
            solar_map.plot(axes=ax, cmap="inferno", vmin=0, vmax=vmaxplt, annotate=False)
            solar_map.draw_limb(axes=ax, color="w", alpha=0.5, linewidth=1.2)
            _set_sunpy_map_fov(ax, solar_map, fov, x_shift=x_shift, y_shift=y_shift)

            bmaj = _safe_channel(meta, "bmaj", bd, 0.0)
            bmin = _safe_channel(meta, "bmin", bd, 0.0)
            bpa = _safe_channel(meta, "bpa", bd, 0.0)
            if bmaj > 0 and bmin > 0:
                scale_x = abs(solar_map.scale.axis1.to_value(u.arcsec / u.pix))
                scale_y = abs(solar_map.scale.axis2.to_value(u.arcsec / u.pix))
                nx = float(solar_map.dimensions.x.value)
                ny = float(solar_map.dimensions.y.value)
                beam = Ellipse(
                    (nx * 0.14, ny * 0.14),
                    bmaj * 3600 / scale_x,
                    bmin * 3600 / scale_y,
                    angle=-(90 - bpa),
                    fc="none",
                    lw=2,
                    ec="w",
                )
                ax.add_artist(beam)

            label_max = "nan" if vmaxplt is None else str(np.round(vmaxplt, 2))
            ax.text(
                0.99,
                0.02,
                r"$T_B^{\rm max}=$" + label_max + "MK",
                color="w",
                ha="right",
                va="bottom",
                fontsize=10,
                transform=ax.transAxes,
            )
            if do_badants_label:
                ax.text(
                    0.99,
                    0.98,
                    "$N_{ants}=$" + str(352 - badants_arr[bd]),
                    color="w",
                    ha="right",
                    va="top",
                    fontsize=10,
                    transform=ax.transAxes,
                )
        else:
            ax = fig.add_subplot(gs[i])
            ax.set_facecolor("black")
            ax.text(0.02, 0.98, f"{freqs_plt[i]:.0f} MHz", color="w", ha="left", va="top", fontsize=11, transform=ax.transAxes)
            ax.text(0.5, 0.5, "No Data", color="w", ha="center", va="center", fontsize=18, transform=ax.transAxes)
            ax.set_xlim([-fov / 2, fov / 2])
            ax.set_ylim([-fov / 2, fov / 2])
            if i not in [8, 9, 10, 11]:
                ax.set_xlabel("")
                ax.get_xaxis().set_ticks([])
            if i not in [0, 4, 8]:
                ax.set_ylabel("")
                ax.get_yaxis().set_ticks([])
        if hasattr(ax, "coords"):
            _hide_wcs_axis_labels(ax, i)
        axes.append(ax)

    suffix = "[refraction corrected]" if rfrcor else "[original]"
    fig.suptitle(f"OVRO-LWA {str(date_obs)[:19]} {suffix}", fontsize=12)
    fig.supxlabel("Helioprojective longitude [arcsec]", fontsize=10)
    fig.supylabel("Helioprojective latitude [arcsec]", fontsize=10)
    return fig, axes


def make_allsky_image_plots(
    allsky_fitsfiles,
    vmaxs=(16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5),
    vmins=(-1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
    cmap="viridis",
):
    """Make a 12-panel plot from all-sky FITS files generated by the pipeline."""
    fig = plt.figure(figsize=(8, 6.3))
    gs = gridspec.GridSpec(3, 4, left=0.02, right=0.98, top=0.94, bottom=0.02, wspace=0.02, hspace=0.02)
    axes = []
    bands = ["32MHz", "36MHz", "41MHz", "46MHz", "50MHz", "55MHz", "59MHz", "64MHz", "69MHz", "73MHz", "78MHz", "82MHz"]
    plotted_fits = []
    allsky_fitsfiles = [str(path) for path in allsky_fitsfiles]

    for i, band in enumerate(bands):
        ax = fig.add_subplot(gs[i])
        ax.set_facecolor("black")
        found_band = False
        for fits_file in allsky_fitsfiles:
            if band in fits_file:
                with fits.open(fits_file) as hdu:
                    ax.imshow(hdu[0].data[0, 0], origin="lower", vmin=vmins[i], vmax=vmaxs[i], cmap=cmap)
                ax.get_xaxis().set_visible(False)
                ax.get_yaxis().set_visible(False)
                ax.get_xaxis().set_ticks([])
                ax.get_yaxis().set_ticks([])
                ax.text(0.02, 0.98, f"{band[:2]} MHz", color="w", ha="left", va="top", fontsize=11, transform=ax.transAxes)
                plotted_fits.append(fits_file)
                found_band = True
                break

        if not found_band:
            ax.text(0.5, 0.5, "No Data", color="w", ha="center", va="center", fontsize=18, transform=ax.transAxes)
        axes.append(ax)

    if len(plotted_fits) > 1:
        basename_parts = os.path.basename(plotted_fits[0]).split(".")
        if len(basename_parts) > 2:
            timestr0 = basename_parts[2]
            timestr = timestr0[:13] + ":" + timestr0[13:15] + ":" + timestr0[15:17]
            fig.suptitle("OVRO-LWA All Sky Images " + timestr, fontsize=12)
        else:
            fig.suptitle("OVRO-LWA All Sky Images", fontsize=12)
        return fig, axes
    return -1
