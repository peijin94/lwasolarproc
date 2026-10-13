"""End-to-end configuration propagation and published admission evidence."""

import csv
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from lwasolarproc import preprocessing_and_imaging as pipeline
from lwasolarproc import realtime_task_manage as manager


@pytest.mark.parametrize("module", [pipeline, manager])
def test_quality_options_are_exposed_on_both_entrypoints(module, tmp_path):
    flags = ["--selfcal-min-finite-fraction", "0.7", "--selfcal-max-phase-std-rad", "0.4",
             "--selfcal-fallback-dir", str(tmp_path)]
    if module is pipeline:
        args = module.parse_args(["--ms-dir", str(tmp_path), *flags])
        config = module.config_from_args(args)
        assert config.selfcal_min_finite_fraction == 0.7
        assert config.selfcal_max_phase_std_rad == 0.4
        assert config.selfcal_fallback_dir == tmp_path
    else:
        args = module.parse_args(flags)
        assert args.selfcal_min_finite_fraction == 0.7
        assert args.selfcal_max_phase_std_rad == 0.4
        assert args.selfcal_fallback_dir == tmp_path


@pytest.mark.parametrize("field,value", [("selfcal_min_finite_fraction", 1), ("selfcal_max_phase_std_rad", float("nan"))])
def test_bad_pipeline_thresholds_rejected_before_launch(field, value):
    with pytest.raises(ValueError):
        pipeline.PipelineConfig(**{field: value})


def test_fullband_matches_fallbacks_by_frequency_without_double_apply(tmp_path, monkeypatch):
    ms = tmp_path / "20260901_200000_78MHz.ms"
    ms.mkdir()
    cal = tmp_path / "bandpass_78MHz.h5"
    cal.touch()
    stable_dir = tmp_path / "stable"
    stable_dir.mkdir()
    stable = stable_dir / "slow_78MHz.h5"
    stable.touch()
    seen = []
    def band(target, config):
        seen.append(target)
        return pipeline.BandResult(target.freq_mhz, "ok", target.work_dir, 0)
    monkeypatch.setattr(pipeline, "process_band", band)
    cfg = pipeline.PipelineConfig(work_dir=tmp_path / "run", selfcal_fallback_dir=stable_dir,
                                  postprocess_products=False)
    pipeline.process_fullband(tmp_path, [cal], cfg)
    assert seen[0].fallback_solution == stable
    assert seen[0].caltable == cal


def test_band_passes_selected_stable_solution_to_selfcal(tmp_path, monkeypatch):
    stable = tmp_path / "slow_78MHz.h5"
    input_ms = tmp_path / "78MHz.ms"
    target = pipeline.BandTarget(78, input_ms, tmp_path / "bandpass_78MHz.h5", tmp_path / "run",
                                 fallback_solution=stable)
    monkeypatch.setattr(pipeline, "prepare_work_ms", lambda *args: input_ms)
    monkeypatch.setattr(pipeline, "run_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "run_average_channels", lambda path, *args: path)
    seen = []
    def selfcal(path, config, timings, **kwargs):
        seen.append(kwargs["fallback_solution"])
        return path, {"phase_selfcal_status": "fallback_transferred"}
    monkeypatch.setattr(pipeline, "run_phase_selfcal", selfcal)
    monkeypatch.setattr(pipeline, "sun_center_ms", lambda path, *args: path)
    monkeypatch.setattr(pipeline, "run_mfs_wsclean", lambda *args: {})
    result = pipeline.process_band(target, pipeline.PipelineConfig(run_bright_source_removal=False, run_fine_channel=False))
    assert result.status == "ok", result.error
    assert seen == [stable]
    assert result.products["phase_selfcal_status"] == "fallback_transferred"


def test_summary_distinguishes_candidate_from_actually_applied_solution(tmp_path):
    result = pipeline.BandResult(78, "ok", tmp_path, 1, products={
        "phase_selfcal_status": "fallback_transferred", "phase_selfcal_candidate": tmp_path / "bad.h5",
        "phase_selfcal_quality_report": tmp_path / "quality.json", "phase_selfcal_solution": None})
    with pipeline.write_summary(tmp_path, [result]).open() as handle:
        row = next(csv.DictReader(handle, delimiter="\t"))
    assert row["phase_selfcal_status"] == "fallback_transferred"
    assert row["phase_selfcal_solution"] == ""
    assert row["phase_selfcal_candidate"].endswith("bad.h5")


@pytest.mark.parametrize("mode", ["event", "backlog", "realtime"])
def test_manager_dispatch_propagates_quality_policy_in_all_modes(tmp_path, mode):
    flags = ["--mode", mode, "--slow-root", str(tmp_path), "--caltable-dir", str(tmp_path),
             "--proc-tmp", str(tmp_path / "proc"), "--proc-out", str(tmp_path / "out"),
             "--dispatch-min-queue", "1", "--selfcal-fallback-dir", str(tmp_path),
             "--selfcal-min-finite-fraction", "0.7", "--selfcal-max-phase-std-rad", "0.4",
             "--no-phase-selfcal"]
    if mode == "backlog":
        flags += ["--start-timestamp", "20260901_200000", "--end-timestamp", "20260901_200000"]
    args = manager.parse_args(flags)
    manager.validate_args(args)
    task_manager = manager.RealtimeManager(args)
    stamp = "20260901_200000"
    task_manager.queue.append(manager.RealtimeTask(stamp, ""))
    task_manager.queued.add(stamp)
    seen = []
    def submit(fn, timestamp, config):
        seen.append(config)
        return Future()
    task_manager.dispatch_ready(SimpleNamespace(submit=submit), [0], {})
    assert len(seen) == 1
    assert seen[0].selfcal_fallback_dir == tmp_path
    assert seen[0].selfcal_min_finite_fraction == 0.7
    assert seen[0].selfcal_max_phase_std_rad == 0.4
    assert seen[0].run_phase_selfcal is False


def test_worker_retains_quality_report_after_scratch_cleanup(tmp_path, monkeypatch):
    stamp = "20260901_200000"
    seen = []
    monkeypatch.setattr(manager, "copy_available_ms_inputs", lambda **kwargs: ("78MHz",))
    monkeypatch.setattr(manager, "collect_caltables", lambda **kwargs: [])
    def process(input_dir, caltables, config, **kwargs):
        seen.append(config)
        report = config.work_dir / "quality.json"
        config.work_dir.mkdir(parents=True)
        report.write_text(json.dumps({"status": "fallback_transferred"}))
        return [SimpleNamespace(status="ok", freq_mhz=78, products={"phase_selfcal_quality_report": report})]
    monkeypatch.setattr(manager, "process_fullband", process)
    monkeypatch.setattr(manager, "publish_outputs", lambda *args, **kwargs: ())
    config = manager.WorkerConfig(
        slow_root=tmp_path, source_layout="flat", proc_tmp=tmp_path / "proc", proc_out=tmp_path / "out",
        log_dir=tmp_path / "log", ingest_lustre=False, caltable_dir=tmp_path, bands=("78MHz",), worker_id=0,
        pipeline_jobs=1, threads=1, fch_pols="I", do_refraction=False, logging=False,
        cleanup_failed=False, worker_rm_tmp=True, selfcal_fallback_dir=tmp_path,
        selfcal_min_finite_fraction=0.7, selfcal_max_phase_std_rad=0.4, run_phase_selfcal=False)
    result = manager.run_worker_task(stamp, config)
    assert result.status == "ok", result.error
    assert not (tmp_path / "proc/worker_0" / stamp).exists()
    assert seen[0].selfcal_max_phase_std_rad == 0.4
    assert seen[0].selfcal_fallback_dir == tmp_path
    assert not seen[0].run_phase_selfcal
    assert len(result.output_paths) == 1
    assert json.loads(Path(result.output_paths[0]).read_text())["status"] == "fallback_transferred"


def test_empty_optional_fallback_dir_transfer_only_fullband(tmp_path, monkeypatch):
    stable = tmp_path / "stable"
    stable.mkdir()
    for freq in (78, 82):
        (tmp_path / f"input_{freq}MHz.ms").mkdir()
        (tmp_path / f"bandpass_{freq}MHz.h5").touch()
    args = pipeline.parse_args(["--ms-dir", str(tmp_path), "--work-dir", str(tmp_path / "run"),
                               "--selfcal-fallback-dir", str(stable), "--no-phase-selfcal"])
    cfg = pipeline.config_from_args(args)
    cfg.postprocess_products = False
    monkeypatch.setattr(pipeline, "prepare_work_ms", lambda target, config: target.src_ms)
    commands = []
    monkeypatch.setattr(pipeline, "run_command", lambda cmd, **kw: commands.append(cmd))
    monkeypatch.setattr(pipeline, "run_average_channels", lambda path, *args: path)
    monkeypatch.setattr(pipeline, "sun_center_ms", lambda path, *args: path)
    monkeypatch.setattr(pipeline, "run_bright_source_removal", lambda path, *args: (path, {}))
    monkeypatch.setattr(pipeline, "run_mfs_wsclean", lambda *args: {})
    monkeypatch.setattr(pipeline, "run_fine_channel_wsclean", lambda *args: {})
    monkeypatch.setattr(pipeline, "run_phase_selfcal", lambda *a, **kw: pytest.fail("must retain upstream calibration"))
    cals = list(tmp_path.glob("bandpass*.h5"))
    results = pipeline.process_fullband(tmp_path, cals, cfg)
    assert [r.status for r in results] == ["ok", "ok"]
    assert sum(any(part.startswith("ac.parmdb=") for part in cmd) for cmd in commands) == 2
    for invalid in (tmp_path / "missing", cals[0]):
        cfg.selfcal_fallback_dir = invalid
        with pytest.raises((FileNotFoundError, NotADirectoryError)):
            pipeline.process_fullband(tmp_path, cals, cfg)
    cfg.selfcal_fallback_dir = stable
    (stable / "a_78MHz.h5").touch()
    (stable / "b_78MHz.h5").touch()
    with pytest.raises(ValueError, match="Expected one caltable"):
        pipeline.process_fullband(tmp_path, cals, cfg)
    with pytest.raises(ValueError, match="No caltables"):
        pipeline.collect_caltables(caltable_dir=tmp_path / "run")


@pytest.mark.parametrize("failure", ["band", "postprocess", "publish"])
def test_worker_failure_preserves_report_before_cleanup(tmp_path, monkeypatch, failure):
    stamp = "20260901_200000"
    monkeypatch.setattr(manager, "copy_available_ms_inputs", lambda **kw: ("78MHz",))
    monkeypatch.setattr(manager, "collect_caltables", lambda **kw: [])
    def process(input_dir, caltables, config, **kwargs):
        report = config.work_dir / "78MHz" / "input_phase_selfcal.quality.json"
        report.parent.mkdir(parents=True)
        report.write_text('{"apply_succeeded": false}')
        if failure == "postprocess":
            raise RuntimeError("postprocess failed")
        return [SimpleNamespace(status="failed" if failure == "band" else "ok", error="downstream failed",
                                freq_mhz=78, products={"phase_selfcal_quality_report": report})]
    monkeypatch.setattr(manager, "process_fullband", process)
    def publish(*args, **kwargs):
        raise RuntimeError("publish failed")
    monkeypatch.setattr(manager, "publish_outputs", publish)
    config = manager.WorkerConfig(
        slow_root=tmp_path, source_layout="flat", proc_tmp=tmp_path / "proc", proc_out=tmp_path / "out",
        log_dir=tmp_path / "log", ingest_lustre=False, caltable_dir=tmp_path, bands=("78MHz",), worker_id=0,
        pipeline_jobs=1, threads=1, fch_pols="I", do_refraction=False, logging=False,
        cleanup_failed=False, worker_rm_tmp=True)
    result = manager.run_worker_task(stamp, config)
    assert result.status == "failed"
    assert {"band": "downstream failed", "postprocess": "postprocess failed", "publish": "publish failed"}[failure] in result.error
    saved = config.log_dir / f"{stamp}.78MHz.selfcal-quality.json"
    assert saved.exists()
    assert json.loads(saved.read_text())["apply_succeeded"] is False
    assert str(saved) in result.output_paths
    assert not Path(result.work_dir).exists()


def test_worker_real_layout_postprocess_failure_preserves_root_report(tmp_path, monkeypatch):
    stamp = "20260901_200000"
    cal = tmp_path / "bandpass_78MHz.h5"
    cal.touch()
    def copy_inputs(**kwargs):
        kwargs["input_dir"].mkdir(parents=True)
        (kwargs["input_dir"] / f"{stamp}_78MHz.ms").mkdir()
        return ("78MHz",)
    def band(target, config):
        assert config.copy_ms is False
        report = pipeline.phase_selfcal_solution_path(
            pipeline.averaged_ms_path(target.src_ms, config.avg_chanbin)).with_suffix(".quality.json")
        assert report.parent == target.src_ms.parent
        report.write_text('{"status":"fallback_transferred"}')
        # A substring match must not mistake 178MHz for 78MHz.
        report.with_name(report.name.replace("78MHz", "178MHz")).write_text('{}')
        return pipeline.BandResult(78, "ok", target.work_dir, 0,
                                   products={"phase_selfcal_quality_report": report})
    def postprocess(*args):
        raise RuntimeError("postprocess failed")
    monkeypatch.setattr(manager, "copy_available_ms_inputs", copy_inputs)
    monkeypatch.setattr(manager, "collect_caltables", lambda **kwargs: [cal])
    monkeypatch.setattr(pipeline, "process_band", band)
    monkeypatch.setattr(pipeline, "postprocess_fullband_products", postprocess)
    config = manager.WorkerConfig(
        slow_root=tmp_path, source_layout="flat", proc_tmp=tmp_path / "proc", proc_out=tmp_path / "out",
        log_dir=tmp_path / "log", ingest_lustre=False, caltable_dir=tmp_path, bands=("78MHz",), worker_id=0,
        pipeline_jobs=1, threads=1, fch_pols="I", do_refraction=False, logging=False,
        cleanup_failed=False, worker_rm_tmp=True)
    result = manager.run_worker_task(stamp, config)
    assert result.status == "failed"
    assert result.error == "postprocess failed"
    saved = config.log_dir / f"{stamp}.78MHz.selfcal-quality.json"
    assert saved.exists()
    assert json.loads(saved.read_text())["status"] == "fallback_transferred"
    assert str(saved) in result.output_paths
    assert not Path(result.work_dir).exists()
