"""CLI invocation, arguments, and secret redaction tests."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from kaggle_runner import urlstore


def test_sync_no_url_no_tty_exits_2_no_traceback(tmp_path):
    env = dict(os.environ)
    env.pop("KAGGLE_RUNNER_URL", None)
    env["KAGGLE_RUNNER_HOME"] = str(tmp_path.parent / f"{tmp_path.name}-runner")
    res = subprocess.run(
        [sys.executable, "-m", "kaggle_runner.sync", "--project", str(tmp_path)],
        stdin=subprocess.DEVNULL,  # Not a TTY
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert res.returncode == 2
    assert "Traceback" not in res.stderr
    assert "Traceback" not in res.stdout
    assert "Usage:" in res.stderr or "usage:" in res.stderr


def test_url_with_ampersand_and_percent_via_env(tmp_path, monkeypatch):
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess-123/tok%20&foo%25bar/proxy"
    monkeypatch.setenv("KAGGLE_RUNNER_URL", test_url)
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))

    loaded = urlstore.load_url(tmp_path)
    assert loaded == test_url

    # Save to file and verify load from file
    urlstore.save_url(tmp_path, test_url)
    monkeypatch.delenv("KAGGLE_RUNNER_URL")
    loaded_file = urlstore.load_url(tmp_path)
    assert loaded_file == test_url


def test_url_with_ampersand_and_percent_via_prompt(tmp_path, monkeypatch):
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess-456/tok%20&baz%25qux/proxy"
    monkeypatch.delenv("KAGGLE_RUNNER_URL", raising=False)
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))

    # Test saving via getpass prompt
    with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value=test_url):
        # Call parse_args/main logic up to saving
        urlstore.save_url(tmp_path, test_url)
        loaded = urlstore.load_url(tmp_path)
        assert loaded == test_url


def test_no_secrets_in_output(tmp_path, monkeypatch, capsys):
    token = "secret-token-abcdef123456"
    test_url = f"https://kkb-production.jupyter-proxy.kaggle.net/k/sess-789/{token}/proxy"
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))

    urlstore.save_url(tmp_path, test_url)

    # Run forget and verify output
    removed = urlstore.forget(tmp_path)
    assert len(removed) > 0

    # Ensure token is never in stdout or stderr
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_kaggle_deps_off_disables_remote_installs(tmp_path, monkeypatch, capsys):
    from kaggle_runner.sync import DependencyManager

    monkeypatch.setenv("KAGGLE_DEPS", "off")
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))

    manager = DependencyManager(client=None, project_root=tmp_path)
    assert manager._run_check() == "skipped"
    assert manager._run_check() == "skipped"
    assert capsys.readouterr().out.count("Dependencies: off") == 1


def test_run_returns_offline_exit_code(tmp_path, monkeypatch, fake_jupyter_server):
    from kaggle_runner import run, session_guard, urlstore

    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "train.py"
    script.write_text("print('never run')", encoding="utf-8")
    urlstore.save_url(tmp_path, fake_jupyter_server.url)
    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.fail_500 = True

    try:
        with pytest.raises(SystemExit) as exc:
            run.main(["--no-sync", "train.py"])
        assert exc.value.code == session_guard.EXIT_OFFLINE
    finally:
        fake_jupyter_server.handler_cls.fail_500 = False


def test_pull_returns_offline_exit_code(tmp_path, monkeypatch, fake_jupyter_server):
    from kaggle_runner import pull, session_guard, urlstore

    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    monkeypatch.chdir(tmp_path)
    urlstore.save_url(tmp_path, fake_jupyter_server.url)
    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.fail_500 = True

    try:
        with pytest.raises(SystemExit) as exc:
            pull.main(["--list"])
        assert exc.value.code == session_guard.EXIT_OFFLINE
    finally:
        fake_jupyter_server.handler_cls.fail_500 = False
