from .calibration import (
    DEFAULT_NIGHT_SETTINGS_PATH,
    DEFAULT_SETTINGS_PATH,
    build_day_night_conversion_table,
    build_gain_table,
    equalizer_func,
    load_equalizer_settings,
    remove_equalization,
)
from .util import (
    ALL_BANDS_MHZ,
    check_h5_fits_consistency,
    compress_fits_to_h5,
    copy_data_from_datetime_str,
    recover_fits_from_h5,
)
from .wsclean_helper import WSCleanOptions, build_wsclean_command, run_wsclean


def __getattr__(name):
    if name in {"PipelineConfig", "process_fullband"}:
        from .preprocessing_and_imaging import PipelineConfig, process_fullband

        return {"PipelineConfig": PipelineConfig, "process_fullband": process_fullband}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ALL_BANDS_MHZ",
    "check_h5_fits_consistency",
    "compress_fits_to_h5",
    "copy_data_from_datetime_str",
    "PipelineConfig",
    "recover_fits_from_h5",
    "WSCleanOptions",
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_NIGHT_SETTINGS_PATH",
    "build_wsclean_command",
    "build_gain_table",
    "build_day_night_conversion_table",
    "equalizer_func",
    "load_equalizer_settings",
    "process_fullband",
    "remove_equalization",
    "run_wsclean",
]
