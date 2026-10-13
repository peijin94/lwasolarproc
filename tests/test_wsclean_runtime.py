"""WSClean runtime environment must follow the configured casacore context."""

from pathlib import Path

from lwasolarproc.wsclean_helper import WSCleanOptions, run_wsclean


def test_wsclean_receives_configured_environment_without_cli_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lwasolarproc.wsclean_helper.subprocess.run",
                        lambda cmd, **kwargs: calls.append((cmd, kwargs)))
    monkeypatch.setenv("CASARCFILES", "stale-default")
    run_wsclean(tmp_path / "input.ms", tmp_path / "model", WSCleanOptions(),
                extra_env={"CASARCFILES": str(tmp_path / "casarc")})
    command, kwargs = calls[0]
    assert "-extra-env" not in command
    assert kwargs["env"]["CASARCFILES"] == str(tmp_path / "casarc")
    assert "PATH" in kwargs["env"]
