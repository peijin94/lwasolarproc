import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Union

ALL_BANDS_MHZ = [
    "13MHz",
    "18MHz",
    "23MHz",
    "27MHz",
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

_ALLOWED_FILE_TYPES = {".ms", ".ms.tar"}


def _normalize_ftype(ftype: str) -> str:
    normalized = ftype.strip()
    if normalized == ",ms,tar":
        normalized = ".ms.tar"
    if normalized not in _ALLOWED_FILE_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_FILE_TYPES))
        raise ValueError(f"Unsupported ftype {ftype!r}. Expected one of: {allowed}")
    return normalized


def _normalize_bands(bands: Optional[Iterable[str]]) -> List[str]:
    normalized = list(ALL_BANDS_MHZ if bands is None else bands)
    invalid = [band for band in normalized if band not in ALL_BANDS_MHZ]
    if invalid:
        raise ValueError(f"Unknown bands: {invalid}. Expected values from ALL_BANDS_MHZ.")
    return normalized


def copy_data_from_datetime_str(
    datetime_str: str,
    dst: Union[str, Path] = "./",
    bands: Optional[Iterable[str]] = None,
    ftype: str = ".ms",
    prefix: Union[str, Path] = "/lustre/pipeline/slow",
) -> List[Path]:
    """
    Copy available band products for a timestamp into ``dst``.

    Missing source paths are skipped silently so callers can request all bands
    even when some products were not generated.
    """
    dt = datetime.strptime(datetime_str, "%Y%m%d_%H%M%S")
    dst_path = Path(dst).expanduser().resolve()
    dst_path.mkdir(parents=True, exist_ok=True)

    prefix_path = Path(prefix)
    selected_bands = _normalize_bands(bands)
    normalized_ftype = _normalize_ftype(ftype)

    copied_paths = []  # type: List[Path]
    day_dir = dt.strftime("%Y-%m-%d")
    hour_dir = dt.strftime("%H")

    for band in selected_bands:
        src = prefix_path / band / day_dir / hour_dir / f"{datetime_str}_{band}{normalized_ftype}"
        if not src.exists():
            continue

        target = dst_path / src.name
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
        copied_paths.append(target)

    return copied_paths


def _default_hdf5_path(fits_file: Union[str, Path]) -> Path:
    path = Path(fits_file)
    if path.suffix.lower() == ".fits":
        return Path.cwd() / f"{path.stem}.hdf"
    return Path.cwd() / f"{path.name}.hdf"


def _decode_h5_names(names) -> list[str]:
    decoded = []
    for name in names:
        if isinstance(name, bytes):
            decoded.append(name.decode())
        else:
            decoded.append(str(name))
    return decoded


def compress_fits_to_h5(
    fits_file: Union[str, Path],
    hdf5_file: Union[str, Path, None] = None,
    beam_ratio: float = 3.0,
    smaller_than_src: bool = True,
    theoretical_beam_thresh: bool = True,
    longest_baseline: float = 3000,
    purge_corrupted: bool = False,
    purge_thresh: float = 1.5,
) -> Path | None:
    """
    Compress an OVRO-LWA FITS cube into an HDF5 file.

    The HDF5 layout mirrors the upstream ``ovrolwasolar`` helper: each
    polarization/channel image is stored as a compressed dataset, and the FITS
    image header plus channel table are attached to ``ch_vals``.
    """
    import h5py
    import numpy as np
    from astropy import units as u
    from astropy.io import fits
    from scipy.ndimage import zoom

    fits_path = Path(fits_file)
    hdf5_path = Path(hdf5_file) if hdf5_file is not None else _default_hdf5_path(fits_path)

    with fits.open(fits_path) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        table_hdu = hdul[1]
        table_data = table_hdu.data
        table_names = list(table_data.dtype.names or [])
        if not table_names:
            raise ValueError(f"No channel table columns found in {fits_path}")

        ch_vals = np.array([table_data[name] for name in table_names])
        freqs = np.asarray(table_data["cfreqs"])
        bmin_name = table_hdu.header.get("TTYPE4", "bmin")
        if bmin_name not in table_names:
            bmin_name = "bmin" if "bmin" in table_names else table_names[min(3, len(table_names) - 1)]

        thresh_arr = np.asarray(table_data[bmin_name], dtype=float) * 3600.0
        if theoretical_beam_thresh:
            beam_size_thresh = (3e8 / freqs) / longest_baseline / np.pi * 180.0 * 3600.0
            thresh_arr = np.maximum(thresh_arr, beam_size_thresh)
            thresh_arr[~(thresh_arr > 0)] = beam_size_thresh[~(thresh_arr > 0)]

        unit_angle = u.Unit(header["CUNIT2"])
        downsize_ratio = thresh_arr / beam_ratio / (header["CDELT2"] * unit_angle.to(u.arcsec))
        if smaller_than_src:
            downsize_ratio[downsize_ratio < 1] = 1

        hdf5_path.parent.mkdir(parents=True, exist_ok=True)
        count_avail = 0
        with h5py.File(hdf5_path, "w") as h5:
            for pol in range(data.shape[0]):
                for ch_idx in range(len(downsize_ratio)):
                    dataset_name = f"FITS_pol{pol}ch{ch_idx:04d}"
                    if purge_corrupted and (-np.min(data[0, ch_idx, :, :]) * purge_thresh > np.max(data[0, ch_idx, :, :])):
                        logging.warning("Pol %s Ch %s is corrupted, skipped", pol, ch_idx)
                        downsized_data = np.zeros((1, 1))
                    else:
                        count_avail += 1
                        zoom_factor = 1 / downsize_ratio[ch_idx]
                        if np.isclose(zoom_factor, 1.0):
                            downsized_data = np.array(data[pol, ch_idx, :, :], copy=True)
                        else:
                            downsized_data = zoom(
                                data[pol, ch_idx, :, :],
                                zoom_factor,
                                order=3,
                                prefilter=False,
                            )
                    h5.create_dataset(dataset_name, data=downsized_data, compression="gzip", compression_opts=9)

            dset = h5.create_dataset("ch_vals", data=ch_vals)
            dset.attrs["arr_name"] = table_names
            dset.attrs["original_shape"] = data.shape
            dset.attrs["original_dtype"] = str(data.dtype)
            for key, value in header.items():
                try:
                    dset.attrs[key] = value
                except TypeError:
                    dset.attrs[key] = str(value)

    if count_avail == 0:
        logging.warning("No available data in the fits file %s", fits_path)
        hdf5_path.unlink(missing_ok=True)
        return None
    return hdf5_path


def recover_fits_from_h5(
    hdf5_file: Union[str, Path],
    fits_out: Union[str, Path, None] = None,
    return_data: bool = False,
    return_meta_only: bool = False,
):
    """
    Recover a FITS cube from an HDF5 file created by ``compress_fits_to_h5``.

    If ``return_data`` is true, returns ``(meta, data)`` without writing a FITS
    file. If ``return_meta_only`` is true, returns only the metadata dict.
    """
    import h5py
    import numpy as np
    from astropy.io import fits
    from scipy.ndimage import zoom

    hdf5_path = Path(hdf5_file)
    if fits_out is None and not return_data and not return_meta_only:
        fits_out = Path.cwd() / hdf5_path.name.replace(".hdf", ".fits").replace(".h5", ".fits")
    fits_out_path = Path(fits_out) if fits_out is not None else None

    with h5py.File(hdf5_path, "r") as h5:
        attrs = dict(h5["ch_vals"].attrs)
        datashape = tuple(int(v) for v in attrs["original_shape"])
        arr_names = _decode_h5_names(attrs["arr_name"])
        data_dtype = np.dtype(attrs.get("original_dtype", "float64"))
        attrs.pop("arr_name", None)
        attrs.pop("original_shape", None)
        attrs.pop("original_dtype", None)
        header = fits.Header(attrs)
        ch_vals = {name: h5["ch_vals"][i] for i, name in enumerate(arr_names)}
        attaching_columns = [fits.Column(name=key, format="E", array=ch_vals[key]) for key in ch_vals]
        meta = {"header": header, **{col.name: col.array for col in attaching_columns}}
        if return_meta_only:
            return meta

        recover_data = np.zeros(datashape, dtype=data_dtype)
        for pol in range(datashape[0]):
            for ch_idx in range(datashape[1]):
                tmp_small = h5[f"FITS_pol{pol}ch{ch_idx:04d}"][:]
                if tmp_small.shape[0] == 1:
                    recover_data[pol, ch_idx, :, :] = tmp_small[0, 0]
                elif tmp_small.shape == datashape[-2:]:
                    recover_data[pol, ch_idx, :, :] = tmp_small
                else:
                    recover_data[pol, ch_idx, :, :] = zoom(
                        tmp_small,
                        datashape[-1] / tmp_small.shape[-1],
                        order=3,
                        prefilter=False,
                    )

        if return_data:
            return meta, recover_data

        if fits_out_path is None:
            raise ValueError("fits_out must be provided unless return_data or return_meta_only is true")
        fits_out_path.parent.mkdir(parents=True, exist_ok=True)
        hdu_list = fits.HDUList(
            [fits.PrimaryHDU(recover_data, header), fits.BinTableHDU.from_columns(attaching_columns)]
        )
        hdu_list.writeto(fits_out_path, overwrite=True)
        return fits_out_path


def check_h5_fits_consistency(
    fits_file: Union[str, Path],
    hdf5_file: Union[str, Path, None] = None,
    ignore_corrupted: bool = False,
    work_dir: Union[str, Path] = "./",
    tolerance: float = 1e-3,
    ignore_ratio: float = 2,
    auto_tol: bool = True,
) -> int:
    """
    Check whether a compressed HDF5 file recovers to the source FITS content.

    Return codes match the upstream helper: ``0`` passes, ``1`` indicates a
    missing recovered header key, ``2`` a header mismatch, ``4`` a data mismatch,
    and ``-1`` an exception during the check.
    """
    import numpy as np
    from astropy.io import fits

    fits_path = Path(fits_file)
    hdf5_path = Path(hdf5_file) if hdf5_file is not None else fits_path.with_suffix(".hdf")
    tmp_path = Path(work_dir) / "tmp.fits"

    pass_check = 0
    try:
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        recover_fits_from_h5(hdf5_path, fits_out=tmp_path)
        with fits.open(tmp_path) as hdu_tmp, fits.open(fits_path) as hdu:
            header_tmp = hdu_tmp[0].header
            header = hdu[0].header
            for key in header.keys():
                if key not in header_tmp.keys():
                    logging.warning("Key %s not in the recovered fits header", key)
                    pass_check = 1
                elif header[key] != header_tmp[key]:
                    logging.warning("Key %s not consistent in the recovered fits header", key)
                    pass_check = 2

            data_tmp = hdu_tmp[0].data
            data = hdu[0].data
            checked_items = 0
            for pol in range(data.shape[0]):
                for ch_idx in range(data.shape[1]):
                    if ignore_corrupted and (-np.min(data[0, ch_idx, :, :]) * ignore_ratio > np.max(data[0, ch_idx, :, :])):
                        continue
                    checked_items += 1
                    local_tolerance = tolerance
                    if auto_tol:
                        denom = np.max(np.abs(data[0, ch_idx, :, :]))
                        if denom != 0:
                            local_tolerance = max(tolerance, -np.min(data[0, ch_idx, :, :]) / denom / 3)
                    denom = np.max(np.abs(data[pol, ch_idx, :, :]))
                    diff = np.mean(np.abs(data[pol, ch_idx, :, :] - data_tmp[pol, ch_idx, :, :]))
                    if denom != 0 and diff / denom > local_tolerance:
                        logging.warning(
                            "Pol %s Ch %s not consistent. Difference: %s for Tol: %s",
                            pol,
                            ch_idx,
                            diff / denom,
                            local_tolerance,
                        )
                        pass_check = 4
                        break
            logging.info("Checked %s items in the fits file", checked_items)
    except Exception:
        pass_check = -1
        logging.exception("Error checking consistency between %s and %s", fits_path, hdf5_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return pass_check
