"""Quality-gate tests use real HDF5; only external imaging tools are stubbed."""

from pathlib import Path

import h5py
import numpy as np
import pytest

from lwasolarproc import preprocessing_and_imaging as pipeline


AXES = ("time", "freq", "ant", "pol")


@pytest.fixture(autouse=True)
def stub_ms_support_for_unit_fixtures(monkeypatch):
    # These HDF5 unit fixtures are not Measurement Sets; real MS support is
    # exercised separately with casacore on calimsolar.
    monkeypatch.setattr(pipeline, "phase_selfcal_active_antennas", lambda path: None)


def test_pipeline_uses_input_support_when_admitting_solutions(monkeypatch, tmp_path):
    values = good_phases()
    weights = np.ones_like(values)
    weights[..., 0, :] = 0
    stub_solve(monkeypatch, values, weights)
    monkeypatch.setattr(pipeline, "phase_selfcal_active_antennas", lambda path: ["ant1", "ant2", "ant3"])
    _, products = pipeline.run_phase_selfcal(tmp_path / "input.ms", pipeline.PipelineConfig(), {})
    assert products["phase_selfcal_status"] == "accepted"
    assert products["phase_selfcal_quality"]["ignored_antennas"] == ["ant0"]


def write_phase(path, values, weights=None, axes=AXES):
    values = np.asarray(values, dtype=float)
    weights = np.ones_like(values) if weights is None else np.asarray(weights, dtype=float)
    with h5py.File(path, "w") as handle:
        group = handle.create_group("sol000/phase000")
        group.create_dataset("val", data=values).attrs["AXES"] = np.bytes_(",".join(axes))
        group.create_dataset("weight", data=weights).attrs["AXES"] = np.bytes_(",".join(axes))
        for axis, size in zip(axes, values.shape, strict=True):
            coordinates = [f"{axis}{i}".encode() for i in range(size)] if axis in {"ant", "pol", "dir"} else np.arange(size)
            group.create_dataset(axis, data=coordinates)
    return path


def good_phases():
    return np.array([[[[-0.1, 0.05], [0.0, -0.05], [0.1, 0.1], [0.2, 0.15]]]])


def stub_solve(monkeypatch, values, weights=None, error=None):
    commands = []
    monkeypatch.setattr(pipeline, "clear_model_data_column", lambda path: True)
    monkeypatch.setattr(pipeline, "run_wsclean", lambda *args, **kwargs: [])

    def run(command, **kwargs):
        commands.append(command)
        if "steps=[gaincal]" in command and not kwargs.get("dry_run"):
            if error is not None:
                raise error
            path = Path(next(part.split("=", 1)[1] for part in command if part.startswith("gaincal.parmdb=")))
            write_phase(path, values, weights)

    monkeypatch.setattr(pipeline, "run_command", run)
    return commands


def test_bad_solve_retains_transferred_calibration_without_apply(monkeypatch, tmp_path, capsys):
    input_ms = tmp_path / "20260901_200000_78MHz_avg4.ms"
    input_ms.mkdir()
    commands = stub_solve(monkeypatch, good_phases() * 10)
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    assert not any("steps=[applycal]" in command for command in commands)
    assert output == input_ms
    assert products["phase_selfcal_status"] == "fallback_transferred"
    assert "fallback" in capsys.readouterr().out


def test_good_solve_is_applied(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    commands = stub_solve(monkeypatch, good_phases())
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    assert output == pipeline.phase_selfcal_ms_path(input_ms)
    apply = [cmd for cmd in commands if "steps=[applycal]" in cmd]
    assert len(apply) == 1
    assert f"applycal.parmdb={pipeline.phase_selfcal_solution_path(input_ms)}" in apply[0]


@pytest.mark.parametrize("invalid", [np.nan, np.inf])
def test_partial_invalid_samples_above_half_are_allowed(tmp_path, invalid):
    values = np.repeat(good_phases(), 3, axis=0)
    values[0, 0, 0, 0] = invalid
    path = write_phase(tmp_path / "phase.h5", values)
    passed, report = pipeline.validate_phase_solution(path)
    assert passed, report
    assert report["finite_fraction"][0][0] == pytest.approx(2 / 3)


@pytest.mark.parametrize("weight", [0, -1, np.nan, np.inf])
def test_flagged_finite_solutions_count_against_per_antenna_coverage(tmp_path, weight):
    values = good_phases()
    weights = np.ones_like(values)
    weights[..., 0, 0] = weight
    passed, report = pipeline.validate_phase_solution(write_phase(tmp_path / "phase.h5", values, weights))
    assert not passed
    assert "coverage" in " ".join(report["reasons"])


@pytest.mark.parametrize("kind", ["missing", "corrupt", "no_phase", "no_weight", "no_axes", "wrong_axes", "wrong_shape", "empty", "coordinate_mismatch"])
def test_unreadable_or_malformed_h5_is_a_rejection(tmp_path, kind):
    path = tmp_path / "phase.h5"
    if kind == "missing":
        pass
    elif kind == "corrupt":
        path.write_text("not hdf5")
    else:
        write_phase(path, good_phases())
        with h5py.File(path, "r+") as handle:
            group = handle["sol000/phase000"]
            if kind == "no_phase":
                del handle["sol000/phase000"]
            elif kind == "no_weight":
                del group["weight"]
            elif kind == "no_axes":
                del group["val"].attrs["AXES"]
            elif kind == "wrong_axes":
                group["weight"].attrs["AXES"] = "time,ant,freq,pol"
            elif kind == "wrong_shape":
                del group["weight"]
                group.create_dataset("weight", data=np.ones((1, 1, 1, 2))).attrs["AXES"] = "time,freq,ant,pol"
            elif kind == "empty":
                del group["val"]
                group.create_dataset("val", data=np.empty((0, 1, 4, 2))).attrs["AXES"] = "time,freq,ant,pol"
            elif kind == "coordinate_mismatch":
                del group["ant"]
                group.create_dataset("ant", data=[b"only_one"])
    passed, report = pipeline.validate_phase_solution(path)
    assert passed is False
    assert report["reasons"]


def test_identity_zero_solve_is_rejected(tmp_path):
    path = write_phase(tmp_path / "phase.h5", np.zeros_like(good_phases()))
    passed, report = pipeline.validate_phase_solution(path)
    assert not passed
    assert "all-zero" in " ".join(report["reasons"])


def test_each_solution_slot_needs_two_valid_antennas(tmp_path):
    values = np.repeat(good_phases(), 3, axis=0)
    weights = np.ones_like(values)
    weights[0, 0, 1:, :] = 0
    passed, report = pipeline.validate_phase_solution(write_phase(tmp_path / "phase.h5", values, weights))
    assert not passed
    assert "two" in " ".join(report["reasons"])


def test_wrapped_phases_and_axis_permutations_are_supported(tmp_path):
    values = np.array([[[[3.1, 3.1], [-3.1, -3.1], [3.12, 3.12], [-3.12, -3.12]]]])
    path = write_phase(tmp_path / "phase.h5", values.transpose(3, 2, 0, 1), axes=("pol", "ant", "time", "freq"))
    with h5py.File(path, "r+") as handle:
        for name in ("val", "weight"):
            handle[f"sol000/phase000/{name}"].attrs["AXES"] = "pol,ant,time,freq"
    passed, report = pipeline.validate_phase_solution(path)
    assert passed, report
    assert report["max_phase_std_rad"] < 0.1


def test_scalarphase_without_pol_axis_is_supported(tmp_path):
    path = write_phase(tmp_path / "phase.h5", good_phases()[..., 0], axes=AXES[:-1])
    passed, report = pipeline.validate_phase_solution(path)
    assert passed, report
    assert report["polarizations"] == ["scalar"]


def test_coverage_boundary_is_strict(tmp_path):
    values = np.repeat(good_phases(), 2, axis=0)
    values[0, 0, 0, 0] = np.nan
    passed, report = pipeline.validate_phase_solution(write_phase(tmp_path / "phase.h5", values))
    assert not passed
    assert report["finite_fraction"][0][0] == 0.5


def test_one_bad_time_and_pol_is_not_diluted_by_good_slots(tmp_path):
    values = np.repeat(good_phases(), 100, axis=0)
    values[50, 0, :, 1] = [-1.0, -0.8, 0.8, 1.0]
    passed, report = pipeline.validate_phase_solution(write_phase(tmp_path / "phase.h5", values))
    assert not passed
    assert report["phase_std_rad_per_pol"][1] > 0.6


@pytest.mark.parametrize("kwargs", [
    {"min_finite_fraction": -0.1}, {"min_finite_fraction": 1},
    {"min_finite_fraction": float("nan")}, {"max_phase_std_rad": 0},
    {"max_phase_std_rad": float("inf")},
])
def test_invalid_gate_configuration_fails_loudly(tmp_path, kwargs):
    with pytest.raises(ValueError):
        pipeline.validate_phase_solution(tmp_path / "missing.h5", **kwargs)


def test_rejected_candidate_uses_valid_stable_residual_phase(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    stable = write_phase(tmp_path / "slow_78MHz.h5", good_phases())
    commands = stub_solve(monkeypatch, good_phases() * 10)
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {}, fallback_solution=stable)
    apply = [cmd for cmd in commands if "steps=[applycal]" in cmd]
    assert len(apply) == 1
    assert f"applycal.parmdb={stable}" in apply[0]
    assert products["phase_selfcal_solution"] == stable
    assert products["phase_selfcal_status"] == "fallback_stable"
    assert output == pipeline.phase_selfcal_ms_path(input_ms)


def test_flagged_input_antennas_do_not_reject_a_good_active_array(tmp_path):
    values = good_phases()
    weights = np.ones_like(values)
    weights[..., 0, :] = 0
    path = write_phase(tmp_path / "phase.h5", values, weights)
    passed, report = pipeline.validate_phase_solution(
        path, required_antennas=["ant1", "ant2", "ant3"])
    assert passed, report
    assert report["ignored_antennas"] == ["ant0"]
    assert report["min_valid_antennas"] == 3


@pytest.mark.parametrize("required", [["ant0", "ant1", "ant2", "ant3"], ["ant1", "not-in-table"], []])
def test_input_support_cannot_hide_newly_invalid_or_missing_solutions(tmp_path, required):
    weights = np.ones_like(good_phases())
    weights[..., 0, :] = 0
    passed, report = pipeline.validate_phase_solution(
        write_phase(tmp_path / "phase.h5", good_phases(), weights), required_antennas=required)
    assert not passed, report
    assert report["reasons"]


def test_duplicate_antenna_labels_cannot_dilute_phase_scatter(tmp_path):
    values = np.full((1, 1, 16, 2), 0.1)
    values[..., -1, :] = 2.0
    path = write_phase(tmp_path / "duplicates.h5", values)
    with h5py.File(path, "r+") as handle:
        handle["sol000/phase000/ant"][...] = [b"ant0"] * 15 + [b"ant1"]
    passed, report = pipeline.validate_phase_solution(path, required_antennas=["ant0", "ant1"])
    assert not passed, report
    assert "duplicate" in " ".join(report["reasons"])


@pytest.mark.parametrize("stage", ["clear", "clear_runtime_permission", "unlink"])
@pytest.mark.parametrize("use_stable", [False, True])
def test_preparation_permission_failure_uses_audited_fallback(monkeypatch, tmp_path, stage, use_stable):
    import json

    commands = stub_solve(monkeypatch, good_phases())
    input_ms = tmp_path / "input.ms"
    candidate = pipeline.phase_selfcal_solution_path(input_ms)
    stable = write_phase(tmp_path / "stable.h5", good_phases()) if use_stable else None
    if stage.startswith("clear"):
        def fail_clear(path):
            if stage == "clear_runtime_permission":
                raise RuntimeError("RegularFileIO: error in open or create: Permission denied")
            raise PermissionError("MODEL_DATA is not writable")
        monkeypatch.setattr(pipeline, "clear_model_data_column", fail_clear)
    else:
        unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == candidate:
                raise PermissionError("cannot remove stale candidate")
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_unlink)
    _, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {}, fallback_solution=stable)
    assert not any("steps=[gaincal]" in command for command in commands)
    assert products["phase_selfcal_status"] == ("fallback_stable" if use_stable else "fallback_transferred")
    report = json.loads(products["phase_selfcal_quality_report"].read_text())
    assert "preparation failed" in " ".join(report["candidate"]["reasons"])


def test_unrelated_preparation_runtime_errors_remain_visible(monkeypatch, tmp_path):
    stub_solve(monkeypatch, good_phases())
    def fail(path):
        raise RuntimeError("internal programming bug")
    monkeypatch.setattr(pipeline, "clear_model_data_column", fail)
    with pytest.raises(RuntimeError, match="internal programming bug"):
        pipeline.run_phase_selfcal(tmp_path / "input.ms", pipeline.PipelineConfig(), {})


def test_invalid_stable_solution_is_never_applied(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    stable = write_phase(tmp_path / "slow_78MHz.h5", good_phases() * 10)
    commands = stub_solve(monkeypatch, np.zeros_like(good_phases()))
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {}, fallback_solution=stable)
    assert output == input_ms
    assert products["phase_selfcal_status"] == "fallback_transferred"
    assert not any("steps=[applycal]" in cmd for cmd in commands)


def test_solver_failure_uses_fallback_and_persists_reason(monkeypatch, tmp_path):
    import json
    import subprocess

    input_ms = tmp_path / "input.ms"
    commands = stub_solve(monkeypatch, good_phases(), error=subprocess.CalledProcessError(1, ["DP3"]))
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    assert output == input_ms
    assert not any("steps=[applycal]" in cmd for cmd in commands)
    report = json.loads(products["phase_selfcal_quality_report"].read_text())
    assert report["status"] == "fallback_transferred"
    assert "gaincal failed" in " ".join(report["candidate"]["reasons"])
    assert products.get("phase_selfcal_solution") is None


@pytest.mark.parametrize("missing_executable", [False, True])
@pytest.mark.parametrize("use_stable", [False, True])
def test_model_generation_failure_uses_fallback_without_gaincal(monkeypatch, tmp_path,
                                                               missing_executable, use_stable):
    import json
    import subprocess

    input_ms = tmp_path / "input.ms"
    stable = write_phase(tmp_path / "stable_78MHz.h5", good_phases()) if use_stable else None
    commands = stub_solve(monkeypatch, good_phases())
    model_product = tmp_path / "input_phase_selfcal_model-model.fits"
    def model(*args, **kwargs):
        model_product.touch()
        if missing_executable:
            raise FileNotFoundError("wsclean missing")
        raise subprocess.CalledProcessError(1, ["wsclean"])
    monkeypatch.setattr(pipeline, "run_wsclean", model)
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {},
                                                fallback_solution=stable)
    assert not any("steps=[gaincal]" in cmd for cmd in commands)
    candidate = pipeline.phase_selfcal_solution_path(input_ms)
    assert not any(f"applycal.parmdb={candidate}" in cmd for cmd in commands)
    apply = [cmd for cmd in commands if "steps=[applycal]" in cmd]
    assert len(apply) == int(use_stable)
    if use_stable:
        assert f"applycal.parmdb={stable}" in apply[0]
    assert output == (pipeline.phase_selfcal_ms_path(input_ms) if use_stable else input_ms)
    report = json.loads(products["phase_selfcal_quality_report"].read_text())
    assert report["status"] == ("fallback_stable" if use_stable else "fallback_transferred")
    assert report["candidate"]["passed"] is False
    assert "model generation failed" in " ".join(report["candidate"]["reasons"])
    assert report["selected_solution"] == (str(stable) if use_stable else None)
    assert report["apply_succeeded"] is use_stable
    assert not model_product.exists()


@pytest.mark.parametrize("use_stable", [False, True])
def test_nonexecutable_wsclean_uses_fallback_and_writes_report(monkeypatch, tmp_path, use_stable):
    """Exercise a real OS launch failure rather than a mocked PermissionError."""
    import json
    import subprocess

    input_ms = tmp_path / "input.ms"
    input_ms.mkdir()
    stable = write_phase(tmp_path / "stable_78MHz.h5", good_phases()) if use_stable else None
    executable = tmp_path / "wsclean-not-executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o644)
    with pytest.raises(PermissionError):
        subprocess.run([str(executable)], check=True)
    monkeypatch.setattr(pipeline, "clear_model_data_column", lambda path: True)
    commands = []
    monkeypatch.setattr(pipeline, "run_command", lambda cmd, **kwargs: commands.append(cmd))
    config = pipeline.PipelineConfig(model_auto_pix_fov=False)
    config.wsclean.wsclean_bin = str(executable)
    output, products = pipeline.run_phase_selfcal(input_ms, config, {}, fallback_solution=stable)
    assert not any("steps=[gaincal]" in cmd for cmd in commands)
    assert output == (pipeline.phase_selfcal_ms_path(input_ms) if use_stable else input_ms)
    assert len(commands) == int(use_stable)
    report = json.loads(products["phase_selfcal_quality_report"].read_text())
    assert report["status"] == ("fallback_stable" if use_stable else "fallback_transferred")
    assert "Permission denied" in " ".join(report["candidate"]["reasons"])
    assert report["apply_succeeded"] is use_stable


def test_selfcal_wsclean_uses_same_casarc_as_dp3(monkeypatch, tmp_path):
    stub_solve(monkeypatch, good_phases())
    calls = []
    monkeypatch.setattr(pipeline, "run_wsclean", lambda *args, **kwargs: calls.append(kwargs))
    casarc = tmp_path / "casarc"
    pipeline.run_phase_selfcal(tmp_path / "input.ms", pipeline.PipelineConfig(casarc=casarc), {})
    assert calls[0]["extra_env"] == {"CASARCFILES": str(casarc)}


def test_selfcal_requires_fresh_model_visibilities_without_slowing_final_imaging():
    from lwasolarproc.wsclean_helper import build_wsclean_command

    config = pipeline.PipelineConfig(model_auto_pix_fov=False)
    selfcal_options = pipeline.phase_selfcal_wsclean_options(config)
    command = build_wsclean_command(Path("input.ms"), Path("model"), selfcal_options)
    assert "-no-update-model-required" not in command
    assert config.wsclean.no_update_model_required is True
    assert pipeline.bright_source_wsclean_options(config).no_update_model_required is True


def test_solve_only_command_never_applies_in_place():
    command = pipeline.build_dp3_gaincal_command(pipeline.PipelineConfig(), Path("input.ms"), Path("phase.h5"))
    assert "gaincal.applysolution=false" in command
    assert "applycal.solset=sol000" in pipeline.build_dp3_phase_applycal_command(
        pipeline.PipelineConfig(), Path("input.ms"), Path("out.ms"), Path("phase.h5"))


def test_reused_legacy_output_without_admission_proof_is_refused(tmp_path):
    input_ms = tmp_path / "input.ms"
    pipeline.phase_selfcal_ms_path(input_ms).mkdir()
    with pytest.raises(ValueError, match="quality"):
        pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(reuse_workdir=True), {})


def test_stale_candidate_cannot_survive_a_solve_that_writes_nothing(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    candidate = write_phase(pipeline.phase_selfcal_solution_path(input_ms), good_phases())
    stub_solve(monkeypatch, good_phases())
    commands = []
    monkeypatch.setattr(pipeline, "run_command", lambda cmd, **kwargs: commands.append(cmd))
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    assert not candidate.exists()
    assert output == input_ms
    assert products["phase_selfcal_status"] == "fallback_transferred"
    assert not any("steps=[applycal]" in cmd for cmd in commands)


def test_dry_run_does_not_validate_or_write_reports(monkeypatch, tmp_path, capsys):
    input_ms = tmp_path / "input.ms"
    stub_solve(monkeypatch, good_phases())
    def unexpected(*args, **kwargs):
        pytest.fail("dry run must not read H5Parm files")
    monkeypatch.setattr(pipeline, "validate_phase_solution", unexpected)
    _, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(dry_run=True), {})
    assert products["phase_selfcal_status"] == "dry_run"
    assert "conditional" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_transfer_only_policy_applies_stable_phases_without_solving(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    stable = write_phase(tmp_path / "slow_78MHz.h5", good_phases())
    commands = stub_solve(monkeypatch, good_phases())
    output, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(run_phase_selfcal=False), {},
                                                fallback_solution=stable)
    assert not any("steps=[gaincal]" in cmd for cmd in commands)
    assert any(f"applycal.parmdb={stable}" in cmd for cmd in commands)
    assert products["phase_selfcal_status"] == "fallback_stable"
    assert output == pipeline.phase_selfcal_ms_path(input_ms)


def test_admitted_output_can_be_reused_but_changed_solution_cannot(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    stub_solve(monkeypatch, good_phases())
    output, _ = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    output.mkdir()
    reused, products = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(reuse_workdir=True), {})
    assert reused == output
    assert products["phase_selfcal_status"] == "accepted"
    write_phase(pipeline.phase_selfcal_solution_path(input_ms), good_phases() * 1.1)
    with pytest.raises(ValueError, match="quality"):
        pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(reuse_workdir=True), {})


@pytest.mark.parametrize("changed", [
    {"selfcal_solint": 10}, {"selfcal_caltype": "scalarphase"},
    {"selfcal_uvlambdamin": 50.0}, {"selfcal_maxiter": 100},
    {"selfcal_tolerance": 1e-4}, {"selfcal_use_model_column": False},
    {"selfcal_model_column": "OTHER_MODEL"}, {"selfcal_niter": 10},
    {"selfcal_mgain": 0.5}, {"selfcal_horizon_mask": "10deg"},
    {"model_auto_pix_fov": False}, {"selfcal_image_size": 512},
    {"selfcal_scale": "4arcmin"}, {"model_auto_telescope_size_m": 2000.0},
    {"model_auto_im_fov_arcsec": 180.0 * 3600.0},
    {"model_auto_pix_scale_factor": 3.0}, {"model_auto_min_size": 512},
    {"selfcal_min_finite_fraction": 0.4}, {"selfcal_max_phase_std_rad": 0.8},
    {"run_phase_selfcal": False},
])
def test_reuse_refuses_changed_solving_policy(monkeypatch, tmp_path, changed):
    input_ms = tmp_path / "input.ms"
    stub_solve(monkeypatch, good_phases())
    output, _ = pipeline.run_phase_selfcal(input_ms, pipeline.PipelineConfig(), {})
    output.mkdir()
    with pytest.raises(ValueError, match="quality"):
        pipeline.run_phase_selfcal(
            input_ms, pipeline.PipelineConfig(reuse_workdir=True, **changed), {})


def test_reuse_refuses_replaced_fallback_selection(monkeypatch, tmp_path):
    input_ms = tmp_path / "input.ms"
    first = write_phase(tmp_path / "stable_a.h5", good_phases())
    replacement = write_phase(tmp_path / "stable_b.h5", good_phases() * 1.1)
    stub_solve(monkeypatch, good_phases())
    config = pipeline.PipelineConfig(run_phase_selfcal=False)
    output, _ = pipeline.run_phase_selfcal(input_ms, config, {}, fallback_solution=first)
    output.mkdir()
    config.reuse_workdir = True
    with pytest.raises(ValueError, match="quality"):
        pipeline.run_phase_selfcal(input_ms, config, {}, fallback_solution=replacement)


def test_scatter_boundary_is_strict(tmp_path):
    path = write_phase(tmp_path / "phase.h5", good_phases())
    passed, report = pipeline.validate_phase_solution(path)
    assert passed
    passed, rejected = pipeline.validate_phase_solution(path, max_phase_std_rad=report["max_phase_std_rad"])
    assert not passed
    assert "scatter" in " ".join(rejected["reasons"])


@pytest.mark.parametrize("axis", ["ant", "pol"])
def test_malformed_coordinate_encoding_is_rejected(tmp_path, axis):
    path = write_phase(tmp_path / "phase.h5", good_phases())
    with h5py.File(path, "r+") as handle:
        handle[f"sol000/phase000/{axis}"][0] = b"\xff"
    passed, report = pipeline.validate_phase_solution(path)
    assert passed is False
    assert "malformed H5Parm" in report["reasons"][0]


def test_transfer_only_dry_run_shows_supplied_stable_path(monkeypatch, tmp_path):
    stable = tmp_path / "stable_78MHz.h5"
    commands = stub_solve(monkeypatch, good_phases())
    pipeline.run_phase_selfcal(tmp_path / "input.ms",
                              pipeline.PipelineConfig(dry_run=True, run_phase_selfcal=False), {},
                              fallback_solution=stable)
    assert len(commands) == 1
    assert f"applycal.parmdb={stable}" in commands[0]


def test_phase_apply_failure_keeps_report_in_failed_band(monkeypatch, tmp_path):
    import json
    input_ms = tmp_path / "input_78MHz.ms"
    target = pipeline.BandTarget(78, input_ms, tmp_path / "bandpass_78MHz.h5", tmp_path / "run")
    stub_solve(monkeypatch, good_phases())
    solve = pipeline.run_command
    def run(cmd, **kwargs):
        if "applycal.phase.correction=phase000" in cmd:
            raise RuntimeError("phase apply failed")
        solve(cmd, **kwargs)
    monkeypatch.setattr(pipeline, "run_command", run)
    monkeypatch.setattr(pipeline, "prepare_work_ms", lambda *args: input_ms)
    monkeypatch.setattr(pipeline, "run_average_channels", lambda path, *args: path)
    result = pipeline.process_band(target, pipeline.PipelineConfig())
    assert result.status == "failed"
    assert result.error == "phase apply failed"
    report = result.products["phase_selfcal_quality_report"]
    audit = json.loads(report.read_text())
    assert audit["candidate"]["passed"]
    assert audit["apply_succeeded"] is False
