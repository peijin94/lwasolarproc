# lwasolarproc

`lwasolarproc` contains OVRO-LWA solar preprocessing, WSClean imaging, FITS/HDF5 utilities, and visualization helpers.

This repository uses a standard package layout:

```text
lwasolarproc/
  pyproject.toml
  README.MD
  lwasolarproc/
    __init__.py
    preprocessing_and_imaging.py
    visualization.py
```

The package is intended to be installed from this directory:

```bash
cd /fast/rtpipe/lwasolarproc
source /fast/rtpipe/use_lwa.sh
uv pip install --python /fast/rtpipe/env/lwa/bin/python -e . --no-build-isolation
```

If the environment does not already have build tooling, install it first:

```bash
uv pip install --python /fast/rtpipe/env/lwa/bin/python setuptools wheel
```

## Runtime Setup

Use the LWA environment before running package commands:

```bash
source /fast/rtpipe/use_lwa.sh
```

That script activates `/fast/rtpipe/env/lwa`, configures cache directories, and sets a writable SunPy working directory so `sunpy.map` does not try to write under the home directory.

The visualization module uses `sunpy.map` directly. The environment needs SunPy's map extras, including `reproject` and `mpl-animators`.

## Command Line

The package installs a full-band preprocessing and imaging entry point:

```bash
lwasolarproc-fullband --help
```

The command wraps the full-band workflow:

1. copy or reuse Measurement Sets
2. apply H5Parm calibration with DP3
3. run AOFlagger with the bundled `LWA_sun_PZ.lua`
4. average, run TTCalSun, recenter on the Sun, and image with WSClean

Example dry run:

```bash
lwasolarproc-fullband \
  --ms-dir /fast/rtpipe/MS \
  --caltable-dir /fast/rtpipe/caltab_h5 \
  --work-dir /tmp/lwasolarproc_fullband \
  --dry-run
```

## Python API

FITS/HDF5 round trip:

```python
from lwasolarproc import (
    compress_fits_to_h5,
    recover_fits_from_h5,
    check_h5_fits_consistency,
)

h5_path = compress_fits_to_h5("image.fits", "image.hdf")
recover_fits_from_h5(h5_path, fits_out="image.recovered.fits")
assert check_h5_fits_consistency("image.fits", h5_path) == 0
```

Visualization:

```python
import matplotlib
matplotlib.use("Agg")
from lwasolarproc.visualization import slow_pipeline_default_plot

fig, axes = slow_pipeline_default_plot("image_I.fits", add_logo=False)
fig.savefig("image_I.quicklook.png", dpi=150, bbox_inches="tight")
```

Full-band processing from Python:

```python
from pathlib import Path
from lwasolarproc import PipelineConfig, process_fullband

results = process_fullband(
    Path("/fast/rtpipe/MS"),
    [Path("/fast/rtpipe/caltab_h5/example.h5parm")],
    PipelineConfig(work_dir=Path("/tmp/lwasolarproc_fullband")),
)
```

## Package Data

The package bundles reusable processing resources:

- `LWA_sun_PZ.lua`
- `settings_mat_file/*.mat`

Use resource helpers instead of hard-coded checkout paths:

```python
from lwasolarproc import aoflagger_strategy_path, settings_file_path

strategy = aoflagger_strategy_path()
settings = settings_file_path("20251120a-settingsAll-day-FW7p5.mat")
```
