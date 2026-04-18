"""Resource path helpers for bundled lwasolarproc data files."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def resource_path(relative_path: str) -> Path:
    """Return the filesystem path for a bundled resource."""
    return PACKAGE_DIR / relative_path


def aoflagger_strategy_path() -> Path:
    """Return the bundled AOFlagger strategy path."""
    return resource_path("LWA_sun_PZ.lua")


def settings_file_path(filename: str) -> Path:
    """Return a bundled equalizer settings file path."""
    return resource_path(f"settings_mat_file/{filename}")
