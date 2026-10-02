"""Packaging and installation tests."""

import subprocess
import sys
from pathlib import Path


def test_no_cmd_files_remain():
    root = Path(__file__).resolve().parent.parent
    cmd_files = list(root.glob("*.cmd"))
    assert cmd_files == [], f"Found leftover .cmd files: {cmd_files}"


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
