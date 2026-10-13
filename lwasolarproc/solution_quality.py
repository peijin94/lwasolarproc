"""Read-only admission checks for residual phase self-calibration H5Parms."""

from pathlib import Path
from typing import Any, Sequence

import h5py
import numpy as np


def _axes(dataset) -> list[str]:
    value = dataset.attrs["AXES"]
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if not isinstance(value, str):
        raise ValueError("AXES must be a comma-separated string")
    return value.split(",")


def validate_quality_thresholds(min_finite_fraction: float, max_phase_std_rad: float) -> None:
    if not np.isfinite(min_finite_fraction) or not 0 <= min_finite_fraction < 1:
        raise ValueError("min_finite_fraction must be finite and in [0, 1)")
    if not np.isfinite(max_phase_std_rad) or max_phase_std_rad <= 0:
        raise ValueError("max_phase_std_rad must be finite and positive")


def validate_phase_solution(
    path: str | Path,
    *,
    min_finite_fraction: float = 0.5,
    max_phase_std_rad: float = 0.6,
    required_antennas: Sequence[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Check ``sol000/phase000`` without modifying the H5Parm.

    Coverage is the fraction of finite, positive-weight samples for EACH
    antenna/polarization. Scatter is the equal-antenna circular standard
    deviation, evaluated separately for every remaining axis coordinate.
    Both bounds are strict. Invalid files return a rejection, not an exception.
    """
    validate_quality_thresholds(min_finite_fraction, max_phase_std_rad)
    report: dict[str, Any] = {
        "path": str(path), "passed": False, "reasons": [],
        "min_finite_fraction": min_finite_fraction,
        "phase_std_limit_rad": max_phase_std_rad,
    }
    try:
        with h5py.File(path, "r") as handle:
            group = handle["sol000/phase000"]
            val, weight = group["val"], group["weight"]
            axes = _axes(val)
            if (len(axes) != val.ndim or len(set(axes)) != len(axes)
                    or "ant" not in axes or set(axes) - {"time", "freq", "ant", "dir", "pol"}):
                raise ValueError("invalid phase axes")
            if _axes(weight) != axes or weight.shape != val.shape:
                raise ValueError("phase/weight axes or shape mismatch")
            if not val.size or any(size == 0 for size in val.shape):
                raise ValueError("empty phase table")
            if val.dtype.kind not in "fiu" or weight.dtype.kind not in "fiu":
                raise ValueError("phase and weights must be real numeric arrays")
            coordinates = {}
            for axis, size in zip(axes, val.shape, strict=True):
                coord = group[axis][...]
                if coord.shape != (size,):
                    raise ValueError(f"{axis} coordinate shape mismatch")
                coordinates[axis] = ([item.decode() if isinstance(item, bytes) else str(item)
                                      for item in coord] if axis in {"ant", "pol"} else coord)
            if len(set(coordinates["ant"])) != len(coordinates["ant"]):
                raise ValueError("duplicate antenna coordinates")
            values = np.asarray(val[...], dtype=float)
            weights = np.asarray(weight[...], dtype=float)
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        report["reasons"] = [f"unreadable or malformed H5Parm: {exc}"]
        return False, report

    # Canonical layout: independent solution slots x antennas x polarizations.
    original_axes = axes.copy()
    if "pol" not in axes:
        values, weights = values[..., None], weights[..., None]
        axes = [*axes, "pol"]
        coordinates["pol"] = np.array(["scalar"])
    ant_axis, pol_axis = axes.index("ant"), axes.index("pol")
    shape = (-1, values.shape[ant_axis], values.shape[pol_axis])
    values = np.moveaxis(values, (ant_axis, pol_axis), (-2, -1)).reshape(shape)
    weights = np.moveaxis(weights, (ant_axis, pol_axis), (-2, -1)).reshape(shape)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    fractions = np.mean(valid, axis=0)
    antenna_names = coordinates["ant"]
    required = set(antenna_names if required_antennas is None else required_antennas)
    report["required_antennas"] = sorted(required)
    report["ignored_antennas"] = [name for name in antenna_names if name not in required]
    missing = required - set(antenna_names)
    if not required or missing:
        report["reasons"] = (["no active input antennas"] if not required
                             else [f"required input antennas missing: {sorted(missing)}"])
        return False, report
    required_mask = np.array([name in required for name in antenna_names], dtype=bool)
    valid = valid[:, required_mask, :]
    values = values[:, required_mask, :]
    counts = np.sum(valid, axis=1)
    safe_values = np.where(valid, values, 0)
    phasors = np.where(valid, np.exp(1j * safe_values), 0)
    resultant = np.abs(np.sum(phasors, axis=1) / np.maximum(counts, 1))
    scatter = np.sqrt(-2 * np.log(np.clip(resultant, np.finfo(float).tiny, 1)))
    reasons = []
    if not np.all(fractions[required_mask] > min_finite_fraction):
        reasons.append("per-antenna/pol coverage failed")
    if not np.all(counts >= 2):
        reasons.append("each solution slot/pol needs at least two valid antennas")
    if not np.all(scatter < max_phase_std_rad):
        reasons.append("phase scatter failed")
    if np.any(np.all((safe_values == 0) | ~valid, axis=1)):
        reasons.append("all-zero solution slot/pol (identity/non-converged solve)")
    report.update(
        passed=not reasons,
        axes=original_axes,
        antennas=coordinates["ant"],
        polarizations=list(coordinates["pol"]),
        finite_fraction=fractions.tolist(),
        phase_std_rad_per_pol=np.max(scatter, axis=0).tolist(),
        max_phase_std_rad=float(np.max(scatter)),
        min_valid_antennas=int(np.min(counts)),
        reasons=reasons,
    )
    return report["passed"], report
