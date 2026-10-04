"""Packaging and installation tests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_windows_launchers_are_available():
    root = Path(__file__).resolve().parent.parent
    assert (root / "install.cmd").is_file()
    for name in ("kaggle-sync", "kaggle-run", "kaggle-pull"):
        launcher = root / "cmd" / f"{name}.cmd"
        assert launcher.is_file()
        assert "KAGGLE_LOCAL_RUNNER_LAUNCHER" in launcher.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows command launchers require cmd.exe")
@pytest.mark.parametrize(
    "name",
    ("kaggle-sync", "kaggle-run", "kaggle-pull"),
)
def test_windows_launchers_work_outside_repository(tmp_path, name):
    root = Path(__file__).resolve().parent.parent
    launcher = root / "cmd" / f"{name}.cmd"
    workdir = tmp_path / "external project"
    workdir.mkdir()
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "--help"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"usage: {name}" in result.stdout.lower()


def test_entrypoints_version_and_help():
    for cmd in ("kaggle-sync", "kaggle-run", "kaggle-pull"):
        res_v = subprocess.run([cmd, "--version"], capture_output=True, text=True)
        assert res_v.returncode == 0, f"{cmd} --version failed: {res_v.stderr}"
        assert "1.0.0" in res_v.stdout

        res_h = subprocess.run([cmd, "--help"], capture_output=True, text=True)
        assert res_h.returncode == 0, f"{cmd} --help failed: {res_h.stderr}"
        assert "usage:" in res_h.stdout.lower()


def test_module_invocation_works():
    res = subprocess.run(
        [sys.executable, "-m", "kaggle_runner", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"python -m kaggle_runner --help failed: {res.stderr}"
    assert "kaggle-sync" in res.stdout


def test_ruff_clean():
    res = subprocess.run(
        ["ruff", "check", "--select", "F,E9,B,W6", "kaggle_runner"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Ruff reported issues:\n{res.stdout}\n{res.stderr}"
