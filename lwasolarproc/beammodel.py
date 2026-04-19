"""TTCal-compatible OVRO-LWA beam models.

The beam model convention follows ``TTCal.jl``: a model is callable as
``beam(frequency_hz, azimuth_rad, elevation_rad)`` and returns a 2x2 Jones
matrix.  Frequency is measured in Hz, and azimuth/elevation are measured in
radians.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import factorial
from typing import Literal

import numpy as np


BeamName = Literal["constant", "sine", "memo178", "lwa178", "zernike"]


def jones_matrix(xx, xy=0.0, yx=0.0, yy=1.0) -> np.ndarray:
    """Return a broadcastable 2x2 complex Jones matrix.

    Scalar inputs return an array with shape ``(2, 2)``.  Array inputs return
    ``(..., 2, 2)`` with the leading dimensions broadcast from the inputs.
    """
    xx, xy, yx, yy = np.broadcast_arrays(xx, xy, yx, yy)
    out = np.empty(xx.shape + (2, 2), dtype=np.complex128)
    out[..., 0, 0] = xx
    out[..., 0, 1] = xy
    out[..., 1, 0] = yx
    out[..., 1, 1] = yy
    return out


def identity_jones(shape=()) -> np.ndarray:
    """Return one or more identity Jones matrices."""
    return jones_matrix(np.ones(shape), np.zeros(shape), np.zeros(shape), np.ones(shape))


class BeamModel:
    """Base class for beam models."""

    def __call__(self, frequency_hz, azimuth_rad, elevation_rad) -> np.ndarray:
        raise NotImplementedError


@dataclass(frozen=True)
class ConstantBeam(BeamModel):
    """Beam model that returns the identity Jones matrix everywhere."""

    def __call__(self, frequency_hz, azimuth_rad, elevation_rad) -> np.ndarray:
        shape = np.broadcast_shapes(
            np.shape(frequency_hz),
            np.shape(azimuth_rad),
            np.shape(elevation_rad),
        )
        return identity_jones(shape)


@dataclass(frozen=True)
class SineBeam(BeamModel):
    """Azimuthally symmetric beam with power gain proportional to sin(el)^alpha."""

    alpha: float = 1.6

    def __call__(self, frequency_hz, azimuth_rad, elevation_rad) -> np.ndarray:
        _, _, elevation_rad = np.broadcast_arrays(frequency_hz, azimuth_rad, elevation_rad)
        s = np.sin(elevation_rad) ** (self.alpha / 2.0)
        return jones_matrix(s, 0.0, 0.0, s)


@dataclass(frozen=True)
class Memo178Beam(BeamModel):
    """LWA Memo 178 parametric dipole beam model."""

    def __call__(self, frequency_hz, azimuth_rad, elevation_rad) -> np.ndarray:
        x = np.sqrt(p178(frequency_hz, azimuth_rad, elevation_rad))
        y = np.sqrt(p178(frequency_hz, np.asarray(azimuth_rad) + np.pi / 2.0, elevation_rad))
        return jones_matrix(x, 0.0, 0.0, y)


@dataclass(frozen=True)
class ZernikeBeam(BeamModel):
    """Zernike-polynomial beam used by the original TTCal test suite."""

    coeff: tuple[float, ...]

    def __init__(self, coeff):
        coeff = tuple(float(value) for value in coeff)
        if len(coeff) != 9:
            raise ValueError("ZernikeBeam expects exactly nine coefficients.")
        object.__setattr__(self, "coeff", coeff)

    def __call__(self, frequency_hz, azimuth_rad, elevation_rad) -> np.ndarray:
        _, azimuth_rad, elevation_rad = np.broadcast_arrays(frequency_hz, azimuth_rad, elevation_rad)
        rho = np.cos(elevation_rad)
        theta = azimuth_rad
        coeff = self.coeff
        value = (
            coeff[0] * zernike(0, 0, rho, theta)
            + coeff[1] * zernike(2, 0, rho, theta)
            + coeff[2] * zernike(4, 0, rho, theta)
            + coeff[3] * zernike(4, 4, rho, theta)
            + coeff[4] * zernike(6, 0, rho, theta)
            + coeff[5] * zernike(6, 4, rho, theta)
            + coeff[6] * zernike(8, 0, rho, theta)
            + coeff[7] * zernike(8, 4, rho, theta)
            + coeff[8] * zernike(8, 8, rho, theta)
        )
        value = np.maximum(value, 0.0)
        gain = np.sqrt(value)
        return jones_matrix(gain, 0.0, 0.0, gain)


_E178 = np.array(
    [
        [-4.529931167425190e01, -3.066691727279789e01, +7.111192148086860e01, +1.131338637814271e01],
        [+1.723596273204143e02, +1.372536555724785e02, -2.664504470520252e02, -3.493942140370373e01],
        [-2.722311453669980e02, -2.368121452949910e02, +4.343953141004854e02, +5.159758406047006e01],
        [+2.408402047219155e02, +2.302040670131884e02, -3.993175413350526e02, -4.152532958513045e01],
        [-1.334589043702679e02, -1.414814115813401e02, +2.312015990805256e02, +2.016753495756661e01],
        [+4.917278320096442e01, +5.820262041648866e01, -8.952191930147437e01, -6.170044495806915e00],
        [-1.244683117802972e01, -1.652458005311876e01, +2.396407975769994e01, +1.185153343629695e00],
        [+2.195017889732819e00, +3.281357788749097e00, -4.500463893128127e00, -1.311234364955677e-01],
        [-2.690339381925372e-01, -4.547844648614167e-01, +5.919912952153034e-01, +4.850030701166225e-03],
        [+2.243604641077215e-02, +4.311447324076417e-02, -5.345256010140458e-02, +7.056536178294128e-04],
        [-1.211643367267544e-03, -2.665185970775750e-03, +3.157455237232649e-03, -1.102532965104807e-04],
        [+3.810179416998095e-05, +9.681257383030349e-05, -1.099243217364324e-04, +6.250327364174989e-06],
        [-5.277176362640874e-07, -1.567746596003443e-06, +1.710529173897111e-06, -1.350506188926788e-07],
    ],
    dtype=float,
)

_H178 = np.array(
    [
        [+4.062920357822495e02, +3.038713068453467e01],
        [-1.706845366994521e03, -1.337217221068207e02],
        [+3.095438045596764e03, +2.567017051330929e02],
        [-3.164514198869798e03, -2.757538828204631e02],
        [+2.035008485840167e03, +1.850998851042284e02],
        [-8.713893456840954e02, -8.227855478515090e01],
        [+2.564477521133275e02, +2.502479051323083e01],
        [-5.262251671400085e01, -5.287316195660030e00],
        [+7.519512104996896e00, +7.754610944702455e-01],
        [-7.337746902310935e-01, -7.744604652809532e-02],
        [+4.663414310713179e-02, +5.024254273102379e-03],
        [-1.740005497709271e-03, -1.908989166200470e-04],
        [+2.892116885178882e-05, +3.223985512686652e-06],
    ],
    dtype=float,
)


def _polyval_ascending(x, coeff: np.ndarray):
    return np.polynomial.polynomial.polyval(x, coeff)


def e178(frequency_hz, elevation_rad):
    """Evaluate the Memo 178 E-plane power pattern."""
    x = np.asarray(frequency_hz, dtype=float) / 10e6
    theta = np.pi / 2.0 - np.asarray(elevation_rad, dtype=float)
    alpha = _polyval_ascending(x, _E178[:, 0])
    beta = _polyval_ascending(x, _E178[:, 1])
    gamma = _polyval_ascending(x, _E178[:, 2])
    delta = _polyval_ascending(x, _E178[:, 3])
    z = theta / (np.pi / 2.0)
    cos_theta = np.cos(theta)
    return (1.0 - z**alpha) * cos_theta**beta + gamma * z * cos_theta**delta


def h178(frequency_hz, elevation_rad):
    """Evaluate the Memo 178 H-plane power pattern."""
    x = np.asarray(frequency_hz, dtype=float) / 10e6
    theta = np.pi / 2.0 - np.asarray(elevation_rad, dtype=float)
    alpha = _polyval_ascending(x, _H178[:, 0])
    beta = _polyval_ascending(x, _H178[:, 1])
    z = theta / (np.pi / 2.0)
    return (1.0 - z**alpha) * np.cos(theta) ** beta


def p178(frequency_hz, azimuth_rad, elevation_rad):
    """Evaluate the Memo 178 azimuthal power interpolation."""
    azimuth_rad = np.asarray(azimuth_rad, dtype=float)
    e_plane = e178(frequency_hz, elevation_rad)
    h_plane = h178(frequency_hz, elevation_rad)
    return np.sqrt((e_plane * np.cos(azimuth_rad)) ** 2 + (h_plane * np.sin(azimuth_rad)) ** 2)


def zernike(n: int, m: int, rho, theta):
    """Evaluate the Zernike polynomial Z_nm at polar coordinates ``rho, theta``."""
    return zernike_radial_part(n, abs(m), rho) * zernike_azimuthal_part(m, theta)


def zernike_radial_part(n: int, m: int, rho):
    """Evaluate the radial part of the Zernike polynomial."""
    if n < 0 or m < 0 or m > n or (n - m) % 2:
        raise ValueError("Zernike radial polynomial requires n >= m >= 0 and even n-m.")

    rho = np.asarray(rho, dtype=float)
    result = np.zeros_like(rho, dtype=float)
    for k in range((n - m) // 2 + 1):
        numerator = (-1) ** k * factorial(n - k)
        denominator = (
            factorial(k)
            * factorial((n + m) // 2 - k)
            * factorial((n - m) // 2 - k)
        )
        result = result + numerator / denominator * rho ** (n - 2 * k)
    return result


def zernike_azimuthal_part(m: int, theta):
    """Evaluate the azimuthal part of the Zernike polynomial."""
    theta = np.asarray(theta, dtype=float)
    if m == 0:
        return np.ones_like(theta, dtype=float)
    if m > 0:
        return np.cos(m * theta)
    return np.sin(-m * theta)


def beam_model(name: BeamName, **kwargs) -> BeamModel:
    """Construct a beam model by TTCal-style name."""
    name = name.lower()
    if name == "constant":
        return ConstantBeam()
    if name == "sine":
        return SineBeam(**kwargs)
    if name in {"memo178", "lwa178"}:
        return Memo178Beam()
    if name == "zernike":
        return ZernikeBeam(**kwargs)
    raise ValueError(f"Unknown beam model: {name}")


def congruence_transform(jones: np.ndarray, visibility: np.ndarray) -> np.ndarray:
    """Apply ``J V J*`` to one or more 2x2 visibility matrices."""
    jones = np.asarray(jones, dtype=np.complex128)
    visibility = np.asarray(visibility, dtype=np.complex128)
    return jones @ visibility @ np.swapaxes(jones.conj(), -1, -2)


__all__ = [
    "BeamModel",
    "BeamName",
    "ConstantBeam",
    "Memo178Beam",
    "SineBeam",
    "ZernikeBeam",
    "beam_model",
    "congruence_transform",
    "e178",
    "h178",
    "identity_jones",
    "jones_matrix",
    "p178",
    "zernike",
    "zernike_azimuthal_part",
    "zernike_radial_part",
]
