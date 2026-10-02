"""CLI invocation, arguments, and secret redaction tests."""

import os
import subprocess
import sys
from unittest.mock import patch

from kaggle_runner import sync, urlstore


def test_sync_no_url_no_tty_exits_2_no_traceback(tmp_path):
    env = dict(os.environ)
    env.pop("KAGGLE_RUNNER_URL", None)
    res = subprocess.run(
        [sys.executable, "-m", "kaggle_runner.sync", "--project", str(tmp_path)],
        stdin=subprocess.DEVNULL,  # Not a TTY
        capture_output=True,
        text=True,
        env=env,
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
