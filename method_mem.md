# lwasolarproc Method Memory

This document is the technical memory for the current `lwasolarproc` package state. It is meant to capture how the package is structured, how the science pipeline currently runs, what assumptions the realtime system makes, and where the important implementation boundaries are.

## Project Scope

`lwasolarproc` packages the active OVRO-LWA solar processing path that had previously lived in ad hoc test scripts. The package now covers four major surfaces:

1. full-band preprocessing and imaging from per-band Measurement Sets
2. FITS post-processing, helioprojective conversion, and HDF5 compression
3. publication-quality quicklook visualization for Stokes `I` and `V`
4. a realtime orchestrator that watches the Lustre slow-data tree and dispatches bounded full-band jobs to worker processes

The package is intentionally built without CASA runtime dependencies in the main path. It uses:

- `DP3` for applycal, AOFlagger, averaging, phase selfcal, and source subtraction
- `WSClean` for imaging and source-list generation
- `astropy`, `sunpy`, `reproject`, and `scikit-image` for FITS coordinate conversion and visualization
- `h5py` for HDF5 export

The design goal is a reusable Python package whose command-line entry points reproduce the working test workflow closely enough that the package can replace those scripts.

## Repository and Package Layout

The package lives under:

```text
/fast/rtpipe/lwasolarproc
```

The import package is:

```text
/fast/rtpipe/lwasolarproc/lwasolarproc
```

Important files:

- `pyproject.toml`
  - package metadata and script entry points
- `README.MD`
  - user-facing setup, CLI usage, and benchmark notes
- `method_mem.md`
  - this document
- `flowchart/`
  - Mermaid and draw.io sources for the pipeline and realtime orchestrator diagrams

Important Python modules:

- `lwasolarproc/preprocessing_and_imaging.py`
  - main full-band workflow
- `lwasolarproc/wsclean_helper.py`
  - WSClean command construction and execution
- `lwasolarproc/coords.py`
  - J2000 to helioprojective conversion and beam/K correction
- `lwasolarproc/visualization.py`
  - default Stokes `I` and `V` quicklooks
- `lwasolarproc/util.py`
  - HDF5 compression/recovery and realtime helper utilities
- `lwasolarproc/source_list.py`
  - WSClean source-list parsing and Sun-distance masking
- `lwasolarproc/beammodel.py`
  - TTCal-style beam models including `Memo178Beam`
- `lwasolarproc/resources.py`
  - paths to packaged resources such as the AOFlagger Lua strategy
- `lwasolarproc/calibration.py`
  - equalizer and day/night gain conversion helpers
- `lwasolarproc/ndfits.py`
  - multi-frequency FITS wrapping helpers
- `lwasolarproc/realtime_task_manage.py`
  - manager/worker realtime orchestration

Bundled non-code resources:

- `lwasolarproc/LWA_sun_PZ.lua`
- `lwasolarproc/settings_mat_file/*.mat`

## Installation and Runtime Assumptions

The intended environment is:

- Python interpreter: `/fast/rtpipe/env/lwa/bin/python`
- environment activation: `source /fast/rtpipe/use_lwa.sh`
- package install mode: editable install with `uv pip install -e . --no-build-isolation`

The package assumes several external executables are available in the active environment or on `PATH`:

- `DP3`
- `wsclean`
- `chgcentre`

The current defaults in package code are conservative and tuned to the benchmarked environment rather than being generic discovery logic. For example:

- `DP3` default binary: `/opt/dp3-6.5.1/bin/DP3`

The package avoids `casatasks`, `casatools`, and `suncasa` in the normal runtime path. `python-casacore` is only optional and used for paths that inspect Measurement Set tables directly.

## Command-Line Entry Points

`pyproject.toml` exposes two main scripts:

```text
lwasolarproc-fullband
lwasolarproc-realtime
```

`lwasolarproc-fullband` maps to:

- `lwasolarproc.preprocessing_and_imaging:main`

`lwasolarproc-realtime` maps to:

- `lwasolarproc.realtime_task_manage:main`

## Full-Band Pipeline: Current Operational Sequence

The current packaged full-band path is:

1. copy or reuse per-band Measurement Sets in the working directory
2. run DP3 `applycal` from H5Parm into `CORRECTED_DATA`
3. run DP3 AOFlagger using bundled `LWA_sun_PZ.lua`
4. average by `avg_chanbin=4`
5. run phase-only selfcal from WSClean model visibilities using DP3 `gaincal`
6. perform bright-source removal outside the solar exclusion radius
7. shift phase center to the Sun
8. average by `avg_chanbin=4` again
9. image MFS Stokes `I,V`
10. optionally image fine-channel Stokes products, default `I` and optionally `I,V`
11. convert J2000 FITS products to helioprojective coordinates
12. apply primary-beam correction and Kelvin conversion in the helio conversion step
13. combine the per-band FITS into:
    - `combined_mfs_I.fits`
    - `combined_mfs_V.fits`
    - `combined_fch_I.fits`
    - optional `combined_fch_V.fits`
14. create default quicklook plots:
    - `combined_mfs_I.default_plot.png`
    - `combined_mfs_V.default_plot.png`

The pipeline is implemented in `process_fullband()` inside [preprocessing_and_imaging.py](/fast/rtpipe/lwasolarproc/lwasolarproc/preprocessing_and_imaging.py).

## Full-Band Pipeline Configuration Object

The central configuration object is `PipelineConfig`.

Important defaults:

- `threads=20`
- `avg_chanbin=4`
- `run_phase_selfcal=True`
- `selfcal_caltype="diagonalphase"`
- `selfcal_uvlambdamin=30`
- `selfcal_maxiter=500`
- `selfcal_tolerance=1e-5`
- `selfcal_image_size=4096`
- `selfcal_scale="2arcmin"`
- `selfcal_niter=800`
- `run_bright_source_removal=True`
- `bright_source_min_distance_deg=6.0`
- `mfs_pols="I,V"`
- `run_fine_channel=True`
- `fch_pols="I"`
- `fch_channels_out=12`
- `postprocess_cutout_size=256`
- `postprocess_usebeam="Memo178Beam"`
- `postprocess_beam_correction=True`
- `postprocess_to_kelvin=True`
- `plot_mfs_i=True`
- `plot_mfs_v=True`

These defaults matter because the package is not exposing every lower-level tool setting directly on the command line. The `PipelineConfig` object is the main place where benchmarked defaults are fixed.

## DP3 Stages

### Applycal

`build_dp3_applycal_command()` writes calibrated data back into the input MS:

- `verbosity=quiet`
- `showcounts=false`
- `showprogress=false`
- `showtimings=false`
- input column: `DATA`
- output column: `CORRECTED_DATA`

The H5Parm corrections used are:

- `solset=sol000`
- amplitude correction: `amplitude000`
- phase correction: `phase000`

### AOFlagger

`build_dp3_aoflagger_command()` uses:

- `verbosity=quiet`
- `aoflag.type=aoflagger`
- bundled Lua strategy
- `aoflag.keepstatistics=false`

This was changed specifically to reduce stdout volume and avoid unnecessary statistics work in the hot path.

### Averaging

`build_dp3_averager_command()` averages by frequency with:

- `avg.type=averager`
- `avg.freqstep=4`

The pipeline currently averages once before phase selfcal and again after Sun centering.

### Phase Selfcal

The phase selfcal stage follows the working quick-processing pattern:

1. run a temporary full-sky WSClean model on the first averaged MS
2. solve a DP3 H5Parm with `gaincal.caltype=diagonalphase`
3. use `gaincal.usemodelcolumn=true` and `gaincal.modelcolumn=MODEL_DATA`
4. apply only `phase000` to a new selfcal MS with DP3 `applycal`

The default selfcal WSClean pass uses frequency-dependent auto image geometry for the temporary model image. The current model auto-geometry defaults are:

- effective telescope size: `2500 m`
- pixel scale factor: `2.2`
- model field of view: `182 deg`

For the current production bands, this gives model-image sizes from about `1200 x 1200` at `23 MHz` to `4050 x 4050` at `82 MHz`. If model auto-geometry is disabled, the fallback selfcal model geometry is `4096 x 4096` with `2arcmin` pixels. The selfcal model pass also uses uniform weighting, `niter=800`, `mgain=0.9`, `horizon-mask=5deg`, `minuv-l=10`, and quiet/no-dirty/no-update-model-required flags. Temporary selfcal image products are deleted by default after the solution is applied.

### Bright-Source Subtraction

`build_dp3_subtract_sources_command()` uses:

- `predict.type=predict`
- `predict.operation=subtract`
- input column: `DATA`

This is fed from a WSClean source list that has already been masked to exclude near-Sun components.

## Bright Source Removal Design

This is a key added capability relative to the earlier simpler packaged pipeline.

The sequence is:

1. run a temporary full-sky WSClean image on the averaged post-selfcal MS
2. request `-save-source-list`
3. parse the generated `*-sources.txt`
4. compute the solar apparent RA/Dec for the observation time
5. measure angular distance from each clean component to the Sun
6. keep only components farther than `bright_source_min_distance_deg`
7. subtract those far components from the MS with DP3 predict-subtract

The source-list logic lives in [source_list.py](/fast/rtpipe/lwasolarproc/lwasolarproc/source_list.py).

The bright-source source-list WSClean pass uses the same model auto-geometry defaults as phase selfcal: `2500 m` effective telescope size, `2.2` pixel scale factor, and `182 deg` field of view. The fixed fallback geometry remains `4096 x 4096` with `2arcmin` pixels when model auto-geometry is disabled.

Important functions:

- `load_wsclean_sources()`
- `distance_to_src_list()`
- `get_time_mjd()`
- `get_sun_ra_dec()`
- `mask_far_sun_sources()`

The bright-source removal path is intentionally subtraction-only. The package does not keep or publish the temporary full-sky validation image unless explicitly asked to preserve those artifacts.

## WSClean Imaging Design

WSClean command construction is centralized in [wsclean_helper.py](/fast/rtpipe/lwasolarproc/lwasolarproc/wsclean_helper.py).

The package uses a `WSCleanOptions` dataclass rather than ad hoc shell string concatenation. This keeps command assembly predictable and makes defaults easy to inspect in Python.

Current final solar imaging defaults:

- `-quiet`
- `-j 20`
- `-mem 5`
- `-size 384 384`
- `-scale 1.8arcmin`
- `-weight briggs -0.5`
- `-minuv-l 10`
- `-auto-threshold 3`
- `-niter 10000`
- `-mgain 0.8`
- `-beam-fitting-size 2`
- `-no-reorder`
- `-no-dirty`
- `-no-update-model-required`

MFS imaging:

- Stokes: `I,V`
- one run with `-pol I,V`

Fine-channel imaging:

- Stokes: `I` by default
- optional Stokes `I,V` with `--fch-pols I,V`
- `channels_out=12`
- current package state does not use the earlier attempted deconvolution-channel split because it hurt performance

The package currently favors low-stdout operation. `quiet=True` is wired through the WSClean options so worker logs stay useful and tool overhead stays low.

Auto pixel/FOV geometry is deliberately restricted to the temporary model-imaging stages used by phase selfcal and bright-source source-list generation. The final small-FOV solar MFS/FCH products keep the fixed `384 x 384`, `1.8arcmin` geometry so the downstream helio cutout and combined FITS products remain stable.

## Coordinate Conversion and Beam Correction

The J2000-to-helioprojective conversion path is in [coords.py](/fast/rtpipe/lwasolarproc/lwasolarproc/coords.py).

Main entry point:

- `fitsj2000tohelio()`

Important behavior:

- rotates the image by the solar `P` angle
- converts `CTYPE1/2` to `HPLN-TAN` and `HPLT-TAN`
- updates `CRVAL`, `CDELT`, and `BPA`
- optionally crops a centered subregion
- can convert from Jy/beam to Kelvin
- can apply a scalar primary-beam correction

The current package defaults use:

- `usebeam="Memo178Beam"`
- `beam_correction=True`
- centered cutout size: `256 x 256`

Beam correction uses the Sun azimuth and elevation at OVRO, evaluates the selected beam model, computes a scalar Stokes-I gain, and divides the image by that gain before final output.

Beam models live in [beammodel.py](/fast/rtpipe/lwasolarproc/lwasolarproc/beammodel.py).

Implemented models:

- `ConstantBeam`
- `SineBeam`
- `Memo178Beam`
- `ZernikeBeam`

The currently used operational model is `Memo178Beam`, derived from the Memo 178 parametric dipole beam description.

## FITS Combination and HDF5 Export

The package supports:

- `compress_fits_to_h5()`
- `recover_fits_from_h5()`
- `check_h5_fits_consistency()`

These live in [util.py](/fast/rtpipe/lwasolarproc/lwasolarproc/util.py).

Compression behavior:

- one compressed dataset per polarization/channel image
- preserves image header metadata on the `ch_vals` dataset
- stores channel tables and original cube shape metadata
- supports beam-size-driven spatial downsizing before gzip compression

Realtime publication uses the FITS-to-HDF conversion automatically after copying final FITS products into the dated output tree under `proc_out`.

## Visualization Design

Visualization lives in [visualization.py](/fast/rtpipe/lwasolarproc/lwasolarproc/visualization.py).

Two default quicklooks matter operationally:

1. `slow_pipeline_default_plot()`
   - 12-panel MFS Stokes `I`
2. `slow_pipeline_default_polarization_plot()`
   - 12-panel MFS `V`

### Default Stokes I Plot

The Stokes `I` plot:

- uses `sunpy.map`
- assumes helioprojective FITS products
- uses a default field of view of `+-7200 arcsec`
- uses smoothed `imshow` interpolation instead of blocky nearest-pixel rendering
- overlays the solar limb and restoring beam
- includes NJIT and Caltech logos embedded as base64 strings in the module

### Default Stokes V Plot

The polarization quicklook uses the `image_V.png` filename and plots Stokes `V` directly in MK.

Current behavior:

- image: Stokes `V` in MK
- color scale: symmetric around zero from the `99.99` percentile of `|V|`
- per-panel annotation: `max|V|`
- text, beam ellipse, and solar-radius circle rendered in black
- the `1 R_sun` circle is black and dotted

This plot was added because the realtime system needs a fast, standardized polarization diagnostic that matches the default Stokes `I` layout closely enough to compare by eye.

## Realtime Orchestrator Design

The realtime orchestration is implemented in [realtime_task_manage.py](/fast/rtpipe/lwasolarproc/lwasolarproc/realtime_task_manage.py).

The design is manager/worker with a bounded FIFO queue.

### Data Model

Important dataclasses:

- `RealtimeTask`
  - `timestamp`
  - `discovered_at`
- `WorkerConfig`
  - immutable configuration bundle passed into worker processes
- `WorkerResult`
  - task status, elapsed wall time, copied bands, outputs, and error text

### Operating Modes

The task manager supports three modes:

- `realtime`
  - structured source layout under `/lustre/pipeline/slow/BAND/YYYY-MM-DD/HH/`
  - scans the trigger band repeatedly
  - applies the OVRO solar elevation threshold
  - chooses the newest ready timestamps when the queue has limited room, so it tries to keep up with live data rather than drain old backlog
  - applies `--cadence-s` as a minimum spacing between actually queued timestamps
- `backlog`
  - structured source layout under `/lustre/pipeline/slow/BAND/YYYY-MM-DD/HH/`
  - uses `--start-timestamp` and `--end-timestamp` to discover existing trigger-band timestamps in that range
  - does not apply the elevation gate
  - drains the discovered timestamp list, including the tail below `dispatch-min-queue`
  - applies `--cadence-s` as a minimum spacing between actually queued timestamps
- `event`
  - flat source layout under a user-provided `--data-dir`
  - discovers existing timestamps directly from the flat folder
  - `--start-timestamp` and `--end-timestamp` are optional filters; when omitted, all matching timestamps in the folder are considered
  - expects names such as `YYYYMMDD_HHMMSS_BAND.ms` or `YYYYMMDD_HHMMSS_BAND.ms.tar` directly in that folder
  - does not apply the elevation gate
  - applies `--cadence-s` as a minimum spacing between actually queued timestamps

### Discovery Model

The manager scans the trigger-band tree:

```text
/lustre/pipeline/slow/55MHz/YYYY-MM-DD/HH/*.ms
/lustre/pipeline/slow/55MHz/YYYY-MM-DD/HH/*.ms.tar
```

The trigger-band timestamps are parsed from filenames of the form:

```text
YYYYMMDD_HHMMSS_55MHz.ms
YYYYMMDD_HHMMSS_55MHz.ms.tar
```

The scan window is limited by `--scan-lookback-hours`.

For every configured production band, availability accepts either an unpacked Measurement Set directory or a tar archive:

- `YYYYMMDD_HHMMSS_BAND.ms`
- `YYYYMMDD_HHMMSS_BAND.ms.tar`

Workers always stage inputs into `proc_tmp`. Unpacked `.ms` inputs are copied with `shutil.copytree()`. Archived `.ms.tar` inputs are copied into the worker `input_ms` directory first, then safely untarred there before pipeline discovery runs. The copied archive is removed after successful extraction so memdisk scratch does not keep both archive and extracted Measurement Set.

In event mode, the same file/directory names are searched directly under `--data-dir`; no band/date/hour subdirectories are used.

### Queue Admission Rules

A realtime timestamp is queued only if all of the following pass:

1. it is within the scan lookback window
2. it is not older than `--start-timestamp`, if that filter is set
3. the Sun elevation at OVRO is at least `--el-valid`
4. at least `--ready-min-bands` of the configured production bands are visible
5. it is not already queued, running, done, or failed
6. the queue is not already at `--queue-length`

The solar-elevation filter was added after a live replay produced empty images because low-elevation timestamps were admitted purely by trigger-band presence.

In backlog and event modes, timestamps come from existing `.ms` or `.ms.tar` filenames, not from a synthetic `start/end/cadence` sequence. Those modes skip the solar-elevation filter and process the discovered timestamps in chronological order. If a discovered timestamp has fewer than `--ready-min-bands` visible inputs, it is marked failed in the manager log rather than being waited on forever.

`--cadence-s` is a minimum enqueue spacing in all modes. A timestamp is skipped if it is closer than that many seconds to the last timestamp that was actually queued. Missing or insufficient-band timestamps do not consume the cadence slot.

### OVRO Solar-Elevation Helper

The new elevation helper lives in [util.py](/fast/rtpipe/lwasolarproc/lwasolarproc/util.py).

Functions:

- `get_ovro_solar_elevation_deg()`
- `filter_ovro_timestamps_by_solar_elevation()`

The implementation uses vectorized `astropy` coordinate transforms:

- parse timestamps into `astropy.time.Time`
- compute apparent Sun coordinates
- transform to `AltAz` at the fixed OVRO EarthLocation
- return elevation in degrees

This helper is deliberately vectorized so one queue scan can evaluate thousands of candidate timestamps without a Python loop around per-time coordinate transformations.

### Dispatch Rules

Dispatch rules are intentionally conservative:

- do not dispatch until queue depth is at least `--dispatch-min-queue`
- when more than one worker is idle, dispatch at most one task every `--dispatch-stagger-s`

The stagger rule exists because concurrent worker starts were all trying to copy large MS directories from Lustre at once, which inflated copy/setup time badly.

### Worker Execution Model

Each worker:

1. creates a private temporary directory under:
   - `proc_tmp/worker_i/YYYYMMDD_HHMMSS`
2. copies all available band MS directories into `input_ms/`
3. runs `process_fullband()` with:
   - `copy_ms=False`
   - `threads=<manager threads>`
   - `jobs=min(pipeline_jobs, number of copied bands)`
4. publishes the combined products
5. compresses FITS outputs into HDF5
6. copies quicklook PNGs and summary TSVs into `proc_out`
7. removes the temporary work directory before returning the worker to the idle pool when `--worker-rm-tmp` is enabled

`--worker-rm-tmp` is enabled by default, so successful and failed per-job scratch directories are removed unless the run explicitly uses `--no-worker-rm-tmp`. `--cleanup-failed` remains available for the older behavior where failed directories are removed even if worker tmp cleanup is disabled.

Worker subprocess stdout and stderr are redirected into per-worker log files so the manager log stays readable.

### Realtime Output Naming

Published realtime output names follow:

- MFS I FITS:
  - `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.fits`
  - `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.hdf`
- MFS V FITS:
  - `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.fits`
  - `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.hdf`
- FCH I FITS:
  - `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_I.fits`
  - `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_I.hdf`
- optional FCH V FITS:
  - `proc_out/fits/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_V.fits`
  - `proc_out/hdf/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_fch_10s.YYYY-MM-DDTHHMMSSZ.image_V.hdf`
- quicklooks:
  - `proc_out/fig/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_I.png`
  - `proc_out/fig/slow/lev1/YYYY/MM/DD/ovro-lwa-352.lev1_mfs_10s.YYYY-MM-DDTHHMMSSZ.image_V.png`

Output directories:

- `proc_out/fits/slow/lev1/YYYY/MM/DD`
- `proc_out/hdf/slow/lev1/YYYY/MM/DD`
- `proc_out/fig/slow/lev1/YYYY/MM/DD`
- `proc_out/log`

### Log Files

Manager log:

- `proc_out/log/realtime_task_manage.log`

Per-worker logs:

- `proc_out/log/YYYYMMDD_HHMMSS.worker_i.log`

Per-task published summaries:

- `YYYYMMDD_HHMMSS.summary.tsv`
- `YYYYMMDD_HHMMSS.combined_products.tsv`

## Current Realtime Defaults

Current packaged defaults are:

- trigger band: `55MHz`
- production bands:
  - `13,18,23,27,32,36,41,46,50,55,59,64,69,73,78,82 MHz`
- workers: `4`
- queue length: `workers + 3` (`7` for the default `4` workers)
- dispatch minimum queue: `3`
- dispatch stagger: `15 s`
- ready minimum bands: `7`
- scan interval: `5 s`
- scan lookback: `1 hour`
- solar elevation gate: `12 deg`
- pipeline jobs: `13`
- WSClean threads per task: `18`

These defaults are a compromise between throughput and Lustre pressure, not a claim that the values are globally optimal.

## Benchmarks and Operational Lessons

### 2026-04-20 5-worker staggered benchmark

Bounded replay:

- queue length: `26`
- completed: `24`
- failed: `0`
- total wall: `610 s`
- overall cadence: `25.4 s/frame`

Slowest instrumented step at that stage:

- bright source removal

Important lesson:

- a large fraction of total frame wall time was outside the per-band timers and came from copy/setup plus publication/post-processing overhead

### 2026-04-21 16:20 UTC replay with solar-elevation gate

Bounded replay path:

- `/fast/rtpipe/proc_realtime_elvalid_1620_20260421`

Settings:

- `--start-timestamp 20260420_162000`
- `--el-valid 12`
- `--queue-length 26`
- `--workers 5`
- `--dispatch-min-queue 3`
- `--dispatch-stagger-s 15`
- `--once`

Observed result:

- skipped below elevation threshold: `3346`
- first queued timestamp: `20260420_162004`
- completed: `24`
- failed: `0`
- total wall time: `746 s`
- mean completed-frame wall: `131.58 s`
- median: `131.69 s`
- min: `120.83 s`
- max: `145.32 s`

Important lesson:

- adding the solar-elevation queue gate fixed the earlier bounded replay failure mode where low-elevation frames could image as effectively empty solar products

### 2026-04-24 20:00 UTC 18-frame memdisk replay

Bounded replay path:

- `/fast/rtpipe/proc_realtime_20260424_2000_18f_5w_memtmp`

Settings:

- `--start-timestamp 20260424_200004`
- `--max-tasks 18`
- `--workers 5`
- `--dispatch-min-queue 3`
- `--dispatch-stagger-s 15`
- worker scratch: `/dev/shm/tmp_pipe_dir/proc_realtime_20260424_2000_18f_5w/proc_tmp`
- output root: `/fast/rtpipe/proc_realtime_20260424_2000_18f_5w_memtmp/proc_out`
- model auto geometry: `2500 m`, `2.2`, `182 deg`
- final solar geometry: fixed `384 x 384`, `1.8arcmin`

Observed result:

- completed: `18`
- failed: `0`
- processed timestamps: `20260424_200004` through `20260424_200254`
- total wall time: `595.67 s`
- worker-frame mean: `126.43 s`
- worker-frame min: `112.69 s`
- worker-frame max: `147.63 s`
- product counts:
  - `mfs`: `72`
  - `fch`: `36`
  - `fig`: `36`
  - all files: `199`

Per-band mean timings over `234` band runs:

- band elapsed: `60.73 s`
- applycal: `1.93 s`
- AOFlagger: `5.58 s`
- avg before selfcal: `1.02 s`
- phase selfcal: `8.49 s`
- bright source removal: `12.56 s`
- sun centering: `0.80 s`
- avg after selfcal: `0.49 s`
- WSClean MFS: `5.18 s`
- WSClean FCH: `24.53 s`

Direct copy benchmark from the same Lustre tree to `/dev/shm`:

- `20260424_200004`: `1.52 s` for 13 bands
- `20260424_200014`: `1.51 s` for 13 bands
- `20260424_200024`: `1.49 s` for 13 bands
- mean direct full-band copy time: `1.51 s`

Important lesson:

- the large worker residual outside the slowest per-band timer is not pure input copy; direct copy was only about `1.5 s` in this benchmark. The residual includes post-processing, HDF compression, publishing, plotting, cleanup, and manager/worker overhead.

## Known Gaps and Next Instrumentation Targets

Current gaps:

- no exact dedicated copy timer around `copy_available_ms_inputs()` inside realtime worker logs
- README benchmark tables are informative but not a substitute for a formal performance report
- some module defaults are encoded in Python dataclasses rather than a runtime configuration file

If further benchmarking is needed, the next concrete instrumentation target should be:

- explicit timing around worker copy/setup
- explicit timing for post-processing and publication after `process_fullband()` returns

That would separate:

1. Lustre copy pressure
2. per-band science processing
3. combined-product publication overhead

## Practical Interpretation

The current package has reached the point where it can:

- run the full calibrated solar pipeline end to end in packaged form
- produce science-ready combined MFS/FCH products
- publish default Stokes `I` and `V` quicklooks
- drive a bounded realtime replay or service-style queue manager from the slow-data tree

The current technical risk is no longer package shape or missing features. It is operational performance: particularly input copy pressure from Lustre and the remaining uninstrumented overhead outside the band-level timers.
