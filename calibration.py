from pathlib import Path

from .resources import settings_file_path

SETTINGS_DIR = Path(__file__).resolve().parent / "settings_mat_file"
DEFAULT_SETTINGS_PATH = SETTINGS_DIR / "20251120a-settingsAll-day-FW7p5.mat"
DEFAULT_NIGHT_SETTINGS_PATH = SETTINGS_DIR / "20251120f-settingsAll-night-FW7p5.mat"
if not DEFAULT_SETTINGS_PATH.exists():
    DEFAULT_SETTINGS_PATH = settings_file_path("20251120a-settingsAll-day-FW7p5.mat")
if not DEFAULT_NIGHT_SETTINGS_PATH.exists():
    DEFAULT_NIGHT_SETTINGS_PATH = settings_file_path("20251120f-settingsAll-night-FW7p5.mat")
NUM_SIGNALS = 704
ISTART = 561
IEND = 3632
TRANSMITTED_SLICE = slice(ISTART - 1, IEND)


def _require_numpy():
    import numpy as np

    return np


def _load_settings(settings_path=DEFAULT_SETTINGS_PATH):
    from scipy.io import loadmat

    path = Path(settings_path)
    if not path.exists():
        raise FileNotFoundError("Settings file does not exist: %s" % path)
    data = loadmat(path, squeeze_me=True)
    return {
        "coef": data["coef"],
        "eq0": data.get("eq0", []),
        "eq1": data.get("eq1", []),
        "eq2": data.get("eq2", []),
        "eq3": data.get("eq3", []),
        "eq4": data.get("eq4", []),
        "eq5": data.get("eq5", []),
        "eq6": data.get("eq6", []),
    }


def load_equalizer_settings(settings_path=DEFAULT_SETTINGS_PATH):
    """Load ``coef`` and ``eq0``...``eq6`` from a settings ``.mat`` file."""
    return _load_settings(settings_path)


def _normalize_eq_array(values):
    np = _require_numpy()
    if values is None:
        return np.array([], dtype=int)
    arr = np.asarray(values).reshape(-1)
    if arr.size == 0:
        return np.array([], dtype=int)
    return arr.astype(int)


def equalizer_func(ch, settings_path=DEFAULT_SETTINGS_PATH):
    """
    Return the 3072-point equalizer function applied to digital signal ``ch``.

    Parameters
    ----------
    ch : int
        Digital signal number in the 0-based ordering described by Larry.
    settings_path : str or Path
        Path to the LWA settings ``.mat`` file containing ``coef`` and
        ``eq0``...``eq6``.

    Returns
    -------
    numpy.ndarray
        The 3072 transmitted-channel equalizer coefficients for this digital
        signal.
    """
    np = _require_numpy()
    settings = _load_settings(settings_path)
    coef = np.asarray(settings["coef"])

    if coef.shape[0] != 7:
        raise ValueError("Expected coef to have shape (7, N).")
    if coef.shape[1] < IEND:
        raise ValueError("Expected coef to include at least 3632 channels.")

    for eq_index in range(7):
        eq_values = _normalize_eq_array(settings["eq%d" % eq_index])
        if np.any(eq_values == int(ch)):
            return coef[eq_index, TRANSMITTED_SLICE]

    raise ValueError("Digital signal %s is not listed in eq0..eq6." % ch)


def build_gain_table(settings_path=DEFAULT_SETTINGS_PATH, num_signals=NUM_SIGNALS):
    """
    Expand the 7 equalizer functions into a full ``(704, 3072)`` gain table.

    Each row corresponds to one digital signal number and contains the
    transmitted-channel equalizer coefficients applied to that signal.
    """
    np = _require_numpy()
    settings = _load_settings(settings_path)
    coef = np.asarray(settings["coef"], dtype=float)

    if coef.shape[0] != 7:
        raise ValueError("Expected coef to have shape (7, N).")
    if coef.shape[1] < IEND:
        raise ValueError("Expected coef to include at least 3632 channels.")

    gain_table = np.full((num_signals, IEND - ISTART + 1), np.nan, dtype=float)

    for eq_index in range(7):
        eq_values = _normalize_eq_array(settings["eq%d" % eq_index])
        if eq_values.size == 0:
            continue
        if np.any(eq_values < 0) or np.any(eq_values >= num_signals):
            raise ValueError("eq%d contains digital signal indices outside 0..%d." % (eq_index, num_signals - 1))
        gain_table[eq_values, :] = coef[eq_index, TRANSMITTED_SLICE]

    missing = np.where(np.isnan(gain_table).any(axis=1))[0]
    if missing.size:
        raise ValueError("Some digital signals are not assigned to any eq group: %s" % missing.tolist())

    return gain_table


def build_day_night_conversion_table(
    day_settings_path=DEFAULT_SETTINGS_PATH,
    night_settings_path=DEFAULT_NIGHT_SETTINGS_PATH,
    num_signals=NUM_SIGNALS,
):
    """
    Build full day/night gain tables and the per-channel gain ratio.

    Returns
    -------
    tuple of numpy.ndarray
        ``(day_gain, night_gain, night_over_day_ratio)``, all with shape
        ``(704, 3072)`` by default.
    """
    np = _require_numpy()
    day_gain = build_gain_table(day_settings_path, num_signals=num_signals)
    night_gain = build_gain_table(night_settings_path, num_signals=num_signals)

    ratio = np.full_like(day_gain, np.nan, dtype=float)
    np.divide(night_gain, day_gain, out=ratio, where=day_gain != 0)

    return day_gain, night_gain, ratio


def remove_equalization(spec, settings_path=DEFAULT_SETTINGS_PATH):
    """
    Undo equalization on 3072-point self-correlation spectra.

    Parameters
    ----------
    spec : array-like, shape (704, 3072)
        Self-correlation spectra ordered by digital signal number.
    settings_path : str or Path
        Path to the LWA settings ``.mat`` file containing ``coef`` and
        ``eq0``...``eq6``.

    Returns
    -------
    numpy.ndarray
        De-equalized spectra with the same shape as ``spec``.
    """
    np = _require_numpy()
    settings = _load_settings(settings_path)
    coef = np.asarray(settings["coef"])
    spec_arr = np.asarray(spec, dtype=float)

    if spec_arr.ndim != 2:
        raise ValueError("spec must be a 2D array.")
    if spec_arr.shape[1] != (IEND - ISTART + 1):
        raise ValueError("spec must have 3072 frequency channels in axis 1.")

    spec1 = spec_arr.copy()

    for eq_index in range(7):
        eq_values = _normalize_eq_array(settings["eq%d" % eq_index])
        if eq_values.size == 0:
            continue
        correction = coef[eq_index, TRANSMITTED_SLICE] ** 2
        spec1[eq_values, :] = spec_arr[eq_values, :] / correction

    return spec1
