import shutil
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
