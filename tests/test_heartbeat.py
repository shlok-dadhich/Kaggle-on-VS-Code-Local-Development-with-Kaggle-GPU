"""Heartbeat and one-shot preflight sync tests."""

import json
import os
import time
import pytest
from unittest.mock import patch

from kaggle_runner import runner_paths, sync, urlstore


def test_heartbeat_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))

    # Initially absent
    assert urlstore.sync_state(tmp_path) == "absent"

    hb = urlstore.Heartbeat(tmp_path, interval=0.1)
    hb.start()
    try:
        time.sleep(0.2)
        assert urlstore.sync_state(tmp_path) == "running"
    finally:
        hb.stop()

    # After stop, file removed
    assert urlstore.sync_state(tmp_path) == "absent"


def test_heartbeat_stale_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    hb_file = runner_paths.heartbeat_file(tmp_path)
    hb_file.parent.mkdir(parents=True, exist_ok=True)

    # Write heartbeat with old timestamp (>20 s ago)
    old_ts = time.time() - 25.0
    hb_file.write_text(
        json.dumps({"pid": os.getpid(), "ts": old_ts}),
        encoding="utf-8",
    )
    assert urlstore.sync_state(tmp_path) == "stale"


def test_heartbeat_stale_after_pid_dead(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    hb_file = runner_paths.heartbeat_file(tmp_path)
    hb_file.parent.mkdir(parents=True, exist_ok=True)

    # Use PID that doesn't exist
    hb_file.write_text(
        json.dumps({"pid": 999999, "ts": time.time()}),
        encoding="utf-8",
    )
    assert urlstore.sync_state(tmp_path) == "stale"


def test_kaggle_run_oneshot_sync_when_not_running(tmp_path, monkeypatch, fake_jupyter_server):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    fake_jupyter_server.clear()
    (tmp_path / ".kagglesyncignore").write_text(".kagglesyncignore\n", encoding="utf-8")

    # Save URL
    urlstore.save_url(tmp_path, fake_jupyter_server.url)

    # Create local file in project
    test_file = tmp_path / "script.py"
    test_file.write_text("print('hello world')", encoding="utf-8")

    client = sync.JupyterClient(fake_jupyter_server.url)

    # State is absent (sync not running)
    assert urlstore.sync_state(tmp_path) == "absent"

    # Preflight sync_once uploads the file
    synced = sync.sync_once(client, tmp_path)
    assert synced == 1

    # Verify file is on fake server
    remote_key = "contents/local-project/script.py"
    assert remote_key in fake_jupyter_server.handler_cls.storage

    # Edit file while sync is stopped
    test_file.write_text("print('edited content')", encoding="utf-8")

    # Second sync_once uploads the edited content
    synced2 = sync.sync_once(client, tmp_path)
    assert synced2 == 1
    assert fake_jupyter_server.handler_cls.storage[remote_key] == b"print('edited content')"


def test_kaggle_run_no_sync_flag_honored(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    fake_jupyter_server.clear()

    urlstore.save_url(tmp_path, fake_jupyter_server.url)

    script_path = tmp_path / "train.py"
    script_path.write_text("print(1)", encoding="utf-8")

    # Mock execute to avoid real websocket in run
    from kaggle_runner import run
    monkeypatch.chdir(tmp_path)

    with patch.object(run.KaggleClient, "execute", return_value={"status": "ok", "user_expressions": {"rc": {"data": {"text/plain": "0"}}}}):
        # Run with --no-sync
        with pytest.raises(SystemExit) as exc:
            run.main(["train.py", "--no-sync"])
        assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "synced" not in out.lower()
