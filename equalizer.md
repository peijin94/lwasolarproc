# Equalizer Notes

## Settings Files

The equalizer settings `.mat` files are stored in:

`/fast/pipe2026solar/settings_mat_file`

Current default files used by `lwasunproc`:

- Day: `settings_mat_file/20251120a-settingsAll-day-FW7p5.mat`
- Night: `settings_mat_file/20251120f-settingsAll-night-FW7p5.mat`

These defaults are defined in [calibration.py](/fast/pipe2026solar/lwasunproc/calibration.py:1).

## Equalizer Model

The settings file contains:

- `coef` with shape `(7, 4096)`
- `eq0` ... `eq6`, each listing digital signal numbers assigned to one equalizer curve

There are:

- `352` antennas
- dual polarization `X/Y`
- `704` digital signals total
- `3072` transmitted channels used from the original `4096` F-engine channels

The transmitted channel window follows the MATLAB code:

- `istart = 561`
- `iend = 3632`
- Python slice: `560:3632`

So `coef[k, 560:3632]` is the 3072-channel equalizer curve for group `k`.

## What `coef[0]` to `coef[6]` Mean

`coef[0]` through `coef[6]` are the seven equalizer functions. They are not seven antennas or seven polarizations.

Instead:

- digital signals listed in `eq0` use `coef[0]`
- digital signals listed in `eq1` use `coef[1]`
- ...
- digital signals listed in `eq6` use `coef[6]`

For a digital signal number `dsig`:

- `X` polarization corresponds to even `dsig = 2 * ant`
- `Y` polarization corresponds to odd `dsig = 2 * ant + 1`

## Implemented Functions

In [calibration.py](/fast/pipe2026solar/lwasunproc/calibration.py:1):

- `load_equalizer_settings(...)`
  Loads `coef` and `eq0...eq6` from a settings file.
- `equalizer_func(ch, settings_path=...)`
  Returns the 3072-channel equalizer curve for digital signal `ch`.
- `remove_equalization(spec, settings_path=...)`
  Applies the inverse equalization to a `(704, 3072)` spectrum array.
- `build_gain_table(settings_path=...)`
  Expands the seven equalizer curves into a full `(704, 3072)` gain table.
- `build_day_night_conversion_table(...)`
  Builds day gain, night gain, and `night/day` ratio tables, each with shape `(704, 3072)`.

## Generated Products

Saved conversion table:

- [day_night_gain_conversion.npz](/fast/pipe2026solar/day_night_gain_conversion.npz)

Arrays inside:

- `day_gain`
- `night_gain`
- `night_over_day_ratio`

All have shape `(704, 3072)`.

## Scripts

Under [lwasunproc/script](/fast/pipe2026solar/lwasunproc/script):

- `test_eq.py`
  Plots day/night equalizer coefficients and comparison plots.
- `report_eq_groups.py`
  Prints which digital signals and antenna/pol pairs belong to `eq0...eq6`.
- `save_day_night_conversion.py`
  Saves the day/night gain conversion arrays to `.npz`.
- `plot_conversion_traces.py`
  Plots selected conversion-factor traces from the saved `.npz`.
