"""Resource path helpers for packaged lwasolarproc data files."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def resource_path(relative_path: str) -> Path:
    """Return the filesystem path for a packaged resource."""
    return Path(files("lwasolarproc").joinpath(relative_path))


def aoflagger_strategy_path() -> Path:
    """Return the bundled AOFlagger strategy path."""
    return resource_path("LWA_sun_PZ.lua")


def settings_file_path(filename: str) -> Path:
    """Return a bundled equalizer settings file path."""
    return resource_path(f"settings_mat_file/{filename}")
