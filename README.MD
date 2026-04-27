# lwasolarproc

`lwasolarproc` contains OVRO-LWA solar preprocessing, WSClean imaging, FITS/HDF5 utilities, and visualization helpers.

This repository uses a standard package layout:

```text
lwasolarproc/
  pyproject.toml
  README.MD
  lwasolarproc/
    __init__.py
    coords.py
    source_list.py
    preprocessing_and_imaging.py
    visualization.py
    wsclean_helper.py
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
4. average by 4 channels
5. run phase-only selfcal with WSClean model visibilities and DP3 `gaincal`
6. build a WSClean source list and subtract bright sources farther than the Sun exclusion radius
7. recenter on the Sun
8. average by 4 channels again
9. image MFS Stokes I/V and fine-channel Stokes I, with optional fine-channel V, using WSClean
10. convert J2000 FITS products to helioprojective coordinates with beam/K correction
11. combine FITS products and write the default MFS Stokes I and MFS V quicklook plots

## Pipeline Workflow

![lwasolarproc pipeline workflow](lwa-pipe2.jpg)

Diagram sources live under [`flowchart/`](./flowchart):

- [`flowchart/flowchart.mmd`](./flowchart/flowchart.mmd)
- [`flowchart/flowchart.drawio`](./flowchart/flowchart.drawio)
- [`flowchart/task_queue_manage_model.mmd`](./flowchart/task_queue_manage_model.mmd)
- [`flowchart/task_queue_manage_model_simplify.mmd`](./flowchart/task_queue_manage_model_simplify.mmd)
- [`flowchart/task_queue_manage_model_simplify.drawio`](./flowchart/task_queue_manage_model_simplify.drawio)

Example dry run:

```bash
lwasolarproc-fullband \
  --ms-dir /fast/rtpipe/MS \
  --caltable-dir /fast/rtpipe/caltab_h5 \
  --work-dir /tmp/lwasolarproc_fullband \
  --dry-run
```

Phase selfcal is enabled by default. It runs a temporary full-sky WSClean model on the first averaged MS, solves a diagonal phase H5Parm with DP3 `gaincal`, and applies only the `phase000` solution before bright-source subtraction. Use `--no-phase-selfcal` to bypass this stage for control tests.

Bright source removal is enabled by default. It runs a temporary full-sky WSClean pass with `-save-source-list`, writes the all-source list, keeps only components farther than `--bright-source-min-distance-deg` from the Sun, and subtracts those components from the post-selfcal averaged MS with DP3 `predict.operation=subtract`. The temporary full-sky FITS/model products are deleted by default; use `--keep-bright-source-images` if you need to inspect them.

The temporary model-imaging passes used by phase selfcal and bright-source removal use frequency-dependent auto geometry by default. The current model auto-geometry defaults are:

- effective telescope size: `2500 m`
- pixel scale factor: `2.2`
- model field of view: `182 deg`

For the current production bands, this gives model-image sizes from about `1200 x 1200` at `23 MHz` to `4050 x 4050` at `82 MHz`. The final small-FOV solar WSClean products do not use auto geometry; they keep the fixed `384 x 384`, `1.8arcmin` image geometry.

Useful source-removal options:

```bash
lwasolarproc-fullband \
  --ms-dir /fast/rtpipe/MS \
  --caltable-dir /fast/rtpipe/caltab_h5parm \
  --work-dir /fast/rtpipe/tests/_lwasolarproc_fullband \
  --bright-source-min-distance-deg 6 \
  --bright-source-image-size 4096 \
  --bright-source-scale 2arcmin
```

Use `--no-bright-source-removal` to run the older path without source-list subtraction.

Current default final solar WSClean imaging settings use `-quiet`, `-j 18`, `-mem 8`, `-size 384 384`, `-scale 1.8arcmin`, `-weight briggs -0.5`, `-minuv-l 10`, `-auto-threshold 3`, `-niter 10000`, `-mgain 0.8`, `-beam-fitting-size 2`, `-no-reorder`, `-no-update-model-required`, and `-no-dirty`.

Fine-channel imaging defaults to Stokes `I`. Use `--fch-pols I,V` to generate fine-channel Stokes `V` products in the same WSClean pass. Realtime mode accepts the same option and publishes optional FCH `V` FITS/HDF products when `combined_fch_V.fits` is produced.

The 2026-04-19 bright-source fullband benchmark at `/fast/rtpipe/tests/_lwasolarproc_fullband_bright_source_20260419` completed `13/13` bands in `66.97 s` wall time. The per-band average was `56.81 s`, including `21.76 s` for bright source removal, and produced combined MFS I, MFS V, and fine-channel I FITS files plus the default MFS I and MFS V plots.

The 2026-04-24 phase-selfcal validation at `/fast/rtpipe/tests/_lwasolarproc_fullband_phase_selfcal_20260424_200004` completed `13/13` bands with `--jobs 4 --no-fine-channel`. The per-band average was `26.09 s`, including `7.39 s` for phase selfcal, `10.26 s` for bright-source removal, and `3.52 s` for MFS Stokes I,V WSClean. It produced combined MFS I/V FITS files and the default MFS I and V quicklook plots.

## Realtime Task Manager

The package also installs a service-ready realtime manager:

```bash
lwasolarproc-realtime \
  --mode realtime \
  --slow-root /lustre/pipeline/slow \
  --caltable-dir /fast/rtpipe/caltab_h5parm \
  --proc-tmp ./proc_tmp \
  --proc-out ./proc_out
```

The manager has three operating modes:

- `--mode backlog`: process existing trigger-band timestamps from the structured slow-data tree over a fixed UTC range, using `--start-timestamp`, `--end-timestamp`, `--slow-root`, and `--bands`.
- `--mode realtime`: follow live structured slow-data under `/lustre/pipeline/slow`, admit only timestamps with solar elevation above `--el-valid`, and prefer the latest ready frames so the queue does not turn into a stale backlog.
- `--mode event`: process existing timestamps from a flat event folder containing files such as `YYYYMMDD_HHMMSS_BAND.ms` or `YYYYMMDD_HHMMSS_BAND.ms.tar`, using `--data-dir` and `--bands`. `--start-timestamp` and `--end-timestamp` are optional filters; when omitted, the whole event folder is considered.

In structured modes, the manager watches paths like `/lustre/pipeline/slow/55MHz/YYYY-MM-DD/HH/` through the `55MHz` trigger band. In flat event mode, it looks directly in the provided data directory. Inputs may be either unpacked `YYYYMMDD_HHMMSS_BAND.ms` directories or `YYYYMMDD_HHMMSS_BAND.ms.tar` archives. Each worker copies all available bands at job start; for `.ms.tar` inputs it first copies the archive into the worker scratch directory, then untars it there before running the fullband pipeline. Workers publish FITS/HDF/PNG products under dated `proc_out/{fits,hdf,fig}/slow/lev1/YYYY/MM/DD/` trees, write logs under `proc_out/log`, and remove per-job temporary work under `proc_tmp/worker_i/YYYYMMDD_HHMMSS` by default.

Queue admission also applies a solar-elevation gate at OVRO. By default `--el-valid 12` means a timestamp is queued only when the solar elevation is at least `12 deg`. This prevents the realtime pipeline from wasting work on low-elevation frames that tend to produce empty or poor-quality solar images.

`--cadence-s` is a minimum enqueue spacing in every mode. The manager discovers timestamps from actual `.ms` or `.ms.tar` filenames, then skips a ready timestamp if it is closer than `--cadence-s` seconds to the last timestamp that was actually queued. This handles raw data that is nominally `10 s` cadence but sometimes lands at `9 s` or `11 s`, without generating synthetic missing timestamps.

By default the realtime figure outputs now include both:

- `proc_out/fig/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.png`
- `proc_out/fig/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.png`

Realtime FITS/HDF products are published as:

- `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.fits`
- `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.hdf`
- `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.fits`
- `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.hdf`
- `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_I.fits`
- `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_I.hdf`
- optional with `--fch-pols I,V`: `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_V.fits`
- optional with `--fch-pols I,V`: `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_V.hdf`

The `image_V.png` quicklook uses the same 12-panel layout as the MFS I figure and plots Stokes `V` in MK. Each panel annotates `max|V|`, and the color scale is set from the `99.99` percentile of `|V|`. Both default quicklook layouts use a solar field of view of `+-7200 arcsec`.

Current realtime defaults:

- trigger band: `55MHz`
- production bands: `13,18,23,27,32,36,41,46,50,55,59,64,69,73,78,82MHz`
- workers: `4`
- queue length: `workers + 3` (`7` for the default `4` workers)
- dispatch threshold: `3`
- dispatch stagger: `15 s`
- solar elevation gate: `12 deg`
- ready threshold: `7` visible bands
- scan interval: `5 s`
- pipeline jobs: `13`
- WSClean threads: `18`
- fine-channel polarizations: `I` by default, or `I,V` with `--fch-pols I,V`
- worker temporary cleanup: enabled by default with `--worker-rm-tmp`

Dispatch staggering is enabled by default. When more than one worker is idle, the manager dispatches only one new task and waits at least `15` seconds before dispatching the next one. This reduces the chance that several workers try to copy large Measurement Set directories from Lustre at the same time.

Useful realtime options:

```bash
lwasolarproc-realtime \
  --mode realtime \
  --slow-root /lustre/pipeline/slow \
  --caltable-dir /fast/rtpipe/caltab_h5parm \
  --proc-tmp /fast/rtpipe/proc_realtime/proc_tmp \
  --proc-out /fast/rtpipe/proc_realtime/proc_out \
  --workers 5 \
  --queue-length 26 \
  --dispatch-min-queue 3 \
  --dispatch-stagger-s 15 \
  --el-valid 12 \
  --scan-lookback-hours 6 \
  --start-timestamp 20260420_162000 \
  --once
```

Backlog replay example:

```bash
lwasolarproc-realtime \
  --mode backlog \
  --slow-root /lustre/pipeline/slow \
  --start-timestamp 20260424_200004 \
  --end-timestamp 20260424_200254 \
  --cadence-s 10 \
  --bands 23MHz,32MHz,36MHz,41MHz,46MHz,50MHz,55MHz,59MHz,64MHz,69MHz,73MHz,78MHz,82MHz \
  --workers 5 \
  --proc-tmp /dev/shm/tmp_pipe_dir/proc_tmp \
  --proc-out /fast/rtpipe/proc_backlog/proc_out
```

Flat event-folder example:

```bash
lwasolarproc-realtime \
  --mode event \
  --data-dir /lustre/peijin/20260322weakTypeII \
  --cadence-s 10 \
  --bands 23MHz,32MHz,36MHz,41MHz,46MHz,50MHz,55MHz \
  --workers 4 \
  --proc-tmp /dev/shm/tmp_pipe_dir/proc_event \
  --proc-out /fast/rtpipe/proc_event/proc_out
```

Important queue behavior in realtime `--once` mode:

- `--once` is intended for bounded realtime smoke tests and debugging, not normal live operation.
- Without `--once`, realtime mode keeps scanning every `--scan-interval` seconds and tries to keep up with the newest ready data.
- The manager scans once, fills the queue, and keeps dispatching while the queue length is at least `--dispatch-min-queue`.
- If the remaining queue length drops below `--dispatch-min-queue`, the manager exits after current workers finish.
- Example: queue length `26` with `--dispatch-min-queue 3` typically processes `24` tasks and leaves the last `2` queued but undispatched.
- Use `--dispatch-min-queue 1` when you want a bounded `--once` run to drain the full queue.

Backlog and event modes do not need `--once`. They discover existing timestamps up front and drain them chronologically; once that list is exhausted, the dispatch threshold drops to `1` so the final tail is processed.

Important queue filters:

- `--start-timestamp YYYYMMDD_HHMMSS` ignores earlier trigger-band timestamps during a bounded replay.
- `--end-timestamp YYYYMMDD_HHMMSS` ignores later timestamps when a bounded range is needed.
- `--cadence-s` is the minimum spacing between actually queued timestamps, not a synthetic timestamp generator.
- `--el-valid` filters timestamps by local OVRO solar elevation before the band-count ready check.
- The elevation filter is vectorized in `lwasolarproc.util.get_ovro_solar_elevation_deg()` so queue scans can cheaply evaluate many timestamps at once.

Realtime worker logs:

- manager log: `proc_out/log/realtime_task_manage.log`
- per-worker logs: `proc_out/log/YYYYMMDD_HHMMSS.worker_i.log`
- worker subprocess stdout/stderr is redirected into the per-worker logs so the manager stream stays concise
- `--worker-rm-tmp` is enabled by default and removes a worker's per-job scratch directory before the worker is returned to the idle pool; use `--no-worker-rm-tmp` only when debugging a failed job directory

### Live Tests

Bounded live smoke on `2026-04-20`:

- two workers
- two completed frames
- timestamps:
  - `20260420_180003`
  - `20260420_180013`
- frame wall times:
  - `120.85 s`
  - `121.26 s`

The products were published under `proc_out_live/mfs`, `proc_out_live/fch`, `proc_out_live/fig`, and `proc_out_live/log`.

### Realtime Benchmark Results

#### 4-worker benchmark without dispatch stagger benefit

Path:

```text
/fast/rtpipe/proc_realtime
```

Configuration:

- workers: `4`
- queue length: `16`
- dispatch threshold: `3`

Observed result:

- completed: `14`
- failed: `0`
- last two queued frames were not dispatched because the queue dropped below `dispatch-min-queue`
- manager wall stats:
  - mean: `129.88 s`
  - median: `130.32 s`
  - min: `111.64 s`
  - max: `143.86 s`

Per-frame slowest instrumented steps from the summary TSVs:

| step | mean frame-max |
|---|---:|
| bright source removal | 30.50 s |
| calibration slot | 17.23 s |
| WSClean fine-channel I | 16.97 s |
| AOFlagger | 6.63 s |
| WSClean MFS I,V | 4.25 s |

#### 5-worker benchmark with 15 s dispatch stagger

Path:

```text
/fast/rtpipe/proc_realtime
```

Run window:

- start: `2026-04-20T23:31:14Z`
- stop: `2026-04-20T23:41:24Z`

Configuration:

- workers: `5`
- queue length: `26`
- dispatch threshold: `3`
- dispatch stagger: `15 s`

Observed result:

- completed: `24`
- failed: `0`
- the last `2` queued timestamps were not dispatched because the remaining queue dropped below `dispatch-min-queue`
- total wall time: `610 s`
- overall throughput: `24 / 610 = 0.0393 frames/s`
- overall cadence: about `25.4 s/frame`

Manager per-frame wall statistics:

| metric | value |
|---|---:|
| mean | 106.97 s |
| median | 115.24 s |
| min | 74.07 s |
| max | 132.89 s |

Per-frame slowest instrumented steps:

| step | mean frame-max |
|---|---:|
| bright source removal | 25.75 s |
| WSClean fine-channel I | 18.32 s |
| calibration slot | 14.08 s |
| AOFlagger | 5.20 s |
| WSClean MFS I,V | 4.19 s |
| sun centering | 1.91 s |
| applycal | 1.77 s |

The slowest-band elapsed time per frame had:

- mean: `68.29 s`
- median: `67.80 s`

That leaves an uninstrumented overhead gap of:

- mean: `38.68 s`
- median: `43.53 s`

This gap includes the realtime wrapper work outside the per-band timers, mainly:

- copying Measurement Sets from Lustre
- post-processing J2000-to-helio conversion
- combined FITS assembly
- HDF compression
- output publishing
- cleanup

### Copy-Time Estimate

There is not yet a dedicated copy timer in the realtime worker. The current estimate uses:

```text
manager dispatch timestamp -> first timestamped line in the worker log
```

This is an estimate of copy plus a small amount of worker setup, so it is slightly larger than pure copy time.

For the 24 completed staggered frames:

| metric | estimate |
|---|---:|
| mean copy+setup | 48.71 s |
| median | 53.00 s |
| min | 20.00 s |
| max | 76.00 s |

Interpretation:

- best mid-run copy+setup was about `20-27 s`
- worst cases were about `67-76 s`

If exact copy benchmarking is needed, add an explicit timer around `copy_available_ms_inputs()` in `lwasolarproc/realtime_task_manage.py`.

#### 5-worker memdisk benchmark with phase selfcal and auto model geometry

Path:

```text
/fast/rtpipe/proc_realtime_20260424_2000_18f_5w_memtmp
```

Configuration:

- data start: `2026-04-24T20:00:04Z`
- completed timestamps: `20260424_200004` through `20260424_200254`
- workers: `5`
- completed frames: `18`
- failed frames: `0`
- worker scratch: `/dev/shm/tmp_pipe_dir/proc_realtime_20260424_2000_18f_5w/proc_tmp`
- output root: `/fast/rtpipe/proc_realtime_20260424_2000_18f_5w_memtmp/proc_out`
- model auto geometry: `2500 m` telescope size, `2.2` pixel scale factor, `182 deg` field of view
- final solar imaging geometry: fixed `384 x 384`, `1.8arcmin`

Overall result:

| metric | value |
|---|---:|
| total wall time | 595.67 s |
| worker-frame mean | 126.43 s |
| worker-frame min | 112.69 s |
| worker-frame max | 147.63 s |

Published product counts:

| product dir | count |
|---|---:|
| `mfs` | 72 |
| `fch` | 36 |
| `fig` | 36 |
| all files | 199 |

Per-band means across `18 x 13 = 234` band runs:

| step | mean |
|---|---:|
| band elapsed | 60.73 s |
| applycal | 1.93 s |
| aoflagger | 5.58 s |
| avg before selfcal | 1.02 s |
| phase selfcal | 8.49 s |
| bright source removal | 12.56 s |
| sun centering | 0.80 s |
| avg after selfcal | 0.49 s |
| WSClean MFS | 5.18 s |
| WSClean FCH | 24.53 s |

The realtime worker still does not record a dedicated copy timer. A direct copy benchmark using the same Lustre source tree and `/dev/shm` target copied all `13` production bands in:

| timestamp | copy time |
|---|---:|
| `20260424_200004` | 1.52 s |
| `20260424_200014` | 1.51 s |
| `20260424_200024` | 1.49 s |

Mean direct full-band input copy time was `1.51 s`. The larger worker residual outside the slowest per-band timer includes post-processing, HDF compression, publishing, plotting, cleanup, and manager/worker overhead, not just input copy.

#### 2026-04-21 bounded 26-task live test with Stokes V quicklook publishing

Path:

```text
/fast/rtpipe/proc_realtime_vi_20260421
```

Configuration:

- workers: `5`
- queue length: `26`
- dispatch threshold: `3`
- dispatch stagger: `15 s`
- output figures: default MFS `I` and `V`

Observed result before the bounded run was stopped:

- queued: `26`
- completed and published: `10`
- each completed frame published both:
  - `...image_I.png`
  - `...image_V.png`
- observed completed-frame wall times included:
  - `894.89 s`
  - `1028.18 s`
  - `1056.32 s`
  - `1118.31 s`
  - `1147.48 s`
  - `665.06 s`
  - `484.03 s`
  - `471.94 s`
  - `400.72 s`
  - `395.20 s`

This 2026-04-21 live run was much slower than the 2026-04-20 benchmark because the first-wave workers spent most of their wall time in live copy/setup on Lustre before the pipeline stage. The run still confirmed that the realtime default now carries the new MFS `V` quicklook all the way through worker post-processing and publication.

#### 2026-04-21 bounded 26-task live test with solar-elevation gating

Path:

```text
/fast/rtpipe/proc_realtime_elvalid_1620_20260421
```

Run window:

- start: `2026-04-21T15:07:23Z`
- stop: `2026-04-21T15:19:49Z`

Configuration:

- workers: `5`
- queue length: `26`
- dispatch threshold: `3`
- dispatch stagger: `15 s`
- elevation gate: `12 deg`
- bounded replay start: `20260420_162000`
- output figures: default MFS `I` and `V`

Observed result:

- skipped before queueing: `3346` timestamps with Sun elevation below `12 deg` at OVRO
- first queued timestamp: `20260420_162004`
- completed: `24`
- failed: `0`
- the last `2` queued timestamps were not dispatched because the remaining queue dropped below `dispatch-min-queue`
- total wall time: `746 s`
- overall throughput: `24 / 746 = 0.0322 frames/s`
- overall cadence: about `31.08 s/frame`

Published products:

- `24` MFS `I` PNG quicklooks
- `24` MFS `V` PNG quicklooks
- `24` per-run summary TSV files

Manager per-frame wall statistics:

| metric | value |
|---|---:|
| mean | 131.58 s |
| median | 131.69 s |
| min | 120.83 s |
| max | 145.32 s |

This run fixed the earlier empty-image replay problem by adding the OVRO solar-elevation queue gate. The replay now starts from a scientifically valid local-Sun elevation instead of blindly queueing low-elevation timestamps from the trigger band.

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

Polarization quicklook:

```python
import matplotlib
matplotlib.use("Agg")
from lwasolarproc.visualization import slow_pipeline_default_polarization_plot

fig, axes = slow_pipeline_default_polarization_plot(
    "combined_mfs_I.fits",
    "combined_mfs_V.fits",
    add_logo=False,
)
fig.savefig("combined_mfs_V.quicklook.png", dpi=150, bbox_inches="tight")
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
