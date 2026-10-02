"""URL storage, permissions, and TTL tests."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from kaggle_runner import runner_paths, urlstore


def test_urlstore_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess/token123/proxy"

    saved_path = urlstore.save_url(tmp_path, test_url)
    assert saved_path.exists()

    content = json.loads(saved_path.read_text(encoding="utf-8"))
    assert content["url"] == test_url
    assert "saved_at" in content

    loaded = urlstore.load_url(tmp_path)
    assert loaded == test_url


def test_urlstore_ttl_expiry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess/expired-token/proxy"

    urlstore.save_url(tmp_path, test_url)
    u_file = runner_paths.url_file(tmp_path)

    # Artificially age the URL to 14 hours ago
    fourteen_hours_ago = time.time() - (14 * 3600)
    u_file.write_text(
        json.dumps({"url": test_url, "saved_at": fourteen_hours_ago}),
        encoding="utf-8",
    )

    loaded = urlstore.load_url(tmp_path)
    assert loaded is None

    # Verify notice printed and file was deleted
    out = capsys.readouterr().out
    assert "expired" in out.lower()
    assert not u_file.exists()


def test_urlstore_legacy_plain_text_read(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess/legacy-tok/proxy"

    u_file = runner_paths.url_file(tmp_path)
    u_file.parent.mkdir(parents=True, exist_ok=True)
    u_file.write_text(test_url, encoding="utf-8")

    loaded = urlstore.load_url(tmp_path)
    assert loaded == test_url


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL test")
def test_windows_acl_grants_current_user(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess/tok/proxy"

    saved_path = urlstore.save_url(tmp_path, test_url)
    res = urlstore.restrict_permissions(saved_path)
    assert res is True


def test_forget_removes_everything(tmp_path, monkeypatch):
    runner_home = tmp_path / ".kaggle-runner"
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(runner_home))

    # Create url file
    test_url = "https://kkb-production.jupyter-proxy.kaggle.net/k/sess/tok/proxy"
    url_file = urlstore.save_url(tmp_path, test_url)

    # Create heartbeat file
    hb_file = runner_paths.heartbeat_file(tmp_path)
    hb_file.parent.mkdir(parents=True, exist_ok=True)
    hb_file.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")

    # Create fake legacy url file in home
    legacy_file = tmp_path / ".kaggle-runner-url"
    legacy_file.write_text(test_url, encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    removed = urlstore.forget(tmp_path)
    assert str(url_file) in removed
    assert str(hb_file) in removed
    assert str(legacy_file) in removed

    assert not url_file.exists()
    assert not hb_file.exists()
    assert not legacy_file.exists()


def test_legacy_file_auto_deleted(tmp_path, monkeypatch, capsys):
    legacy_file = tmp_path / ".kaggle-runner-url"
    legacy_file.write_text("https://kkb-production.jupyter-proxy.kaggle.net/k/sess/tok/proxy", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cleaned = urlstore.cleanup_legacy_url_file()
    assert cleaned == legacy_file
    assert not legacy_file.exists()
    captured = capsys.readouterr().out
    assert "legacy" in captured.lower()
