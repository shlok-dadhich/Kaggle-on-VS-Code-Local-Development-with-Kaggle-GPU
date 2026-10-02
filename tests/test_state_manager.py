import json
import os
import socket
import threading
import time

import pytest
import requests

from kaggle_runner import runner_paths, session_guard, sync


def _use_temp_runner_home(monkeypatch, tmp_path):
    home = tmp_path.parent / f"{tmp_path.name}-runner-home"
    monkeypatch.setattr(runner_paths, "runner_home", lambda: home)
    sync.bind_state_lock(tmp_path)


def test_state_lock_records_owner_lease(monkeypatch, tmp_path):
    _use_temp_runner_home(monkeypatch, tmp_path)

    with runner_paths.state_lock(tmp_path):
        metadata = json.loads(
            runner_paths.lock_file(tmp_path).read_text(encoding="utf-8")
        )

    assert metadata["pid"] == os.getpid()
    assert metadata["host"] == socket.gethostname()
    assert metadata["created"] == metadata["refreshed"]


def test_state_lock_refreshes_lease_while_held(monkeypatch, tmp_path):
    _use_temp_runner_home(monkeypatch, tmp_path)
    monkeypatch.setattr(runner_paths, "STATE_LOCK_REFRESH_SECONDS", 0.01)

    with runner_paths.state_lock(tmp_path):
        lock_path = runner_paths.lock_file(tmp_path)
        before = json.loads(lock_path.read_text(encoding="utf-8"))
        time.sleep(0.04)
        after = json.loads(lock_path.read_text(encoding="utf-8"))

    assert after["refreshed"] > before["refreshed"]


def test_state_lock_stale_when_local_owner_is_dead(monkeypatch, tmp_path):
    _use_temp_runner_home(monkeypatch, tmp_path)
    lock_path = runner_paths.lock_file(tmp_path)
    lock_path.write_text(
        json.dumps({
            "pid": -1,
            "host": socket.gethostname(),
            "created": time.time(),
            "refreshed": time.time(),
        }),
        encoding="utf-8",
    )

    with runner_paths.state_lock(tmp_path):
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()


def test_state_lock_live_remote_owner_expires_after_lease(monkeypatch, tmp_path):
    _use_temp_runner_home(monkeypatch, tmp_path)
    lock_path = runner_paths.lock_file(tmp_path)
    now = time.time()
    metadata = {
        "pid": os.getpid(),
        "host": "another-host",
        "created": now - 61,
        "refreshed": now - 61,
    }
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert runner_paths._lock_is_stale(lock_path, now=now)
    metadata["refreshed"] = now
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert not runner_paths._lock_is_stale(lock_path, now=now)


def test_state_manager_updates_are_atomic_and_preserve_other_keys(
    monkeypatch,
    tmp_path,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    manager = sync.StateManager(tmp_path)
    manager.save_files({"one.py": {"size": 1}})
    manager.set_value("requirements", "requirements.txt", "digest")
    manager.save_files({"two.py": {"size": 2}})

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state["files"] == {
        "one.py": {"size": 1},
        "two.py": {"size": 2},
    }
    assert state["requirements"]["requirements.txt"] == "digest"

    manager.delete_file("one.py")
    assert manager.load_files() == {"two.py": {"size": 2}}


def test_legacy_state_is_deleted_only_after_verified_write(
    monkeypatch,
    tmp_path,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    legacy = tmp_path / ".kaggle-sync-state.json"
    legacy.write_text(
        json.dumps({"files": {"script.py": {"size": 7}}}),
        encoding="utf-8",
    )

    sync.migrate_legacy_state(tmp_path)

    assert not legacy.exists()
    assert sync.StateManager(tmp_path).load_files() == {
        "script.py": {"size": 7},
    }


def test_legacy_state_is_kept_when_atomic_write_fails(
    monkeypatch,
    tmp_path,
    capsys,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    legacy = tmp_path / ".kaggle-sync-state.json"
    legacy.write_text("{}", encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner_paths, "write_text_atomic", fail_write)
    sync.migrate_legacy_state(tmp_path)

    assert legacy.exists()
    assert "kept" in capsys.readouterr().out


def test_offline_state_reports_only_transitions():
    state = session_guard.OFFLINE_STATE
    with state._lock:
        state._offline = False
        state._backoff = 2.0
        state.recovered.clear()

    assert state.mark_offline()
    assert not state.mark_offline()
    assert state.mark_online()
    assert not state.mark_online()
    assert state.recovered.is_set()


def test_one_shot_upload_does_not_hold_state_lock(
    monkeypatch,
    tmp_path,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    (tmp_path / "script.py").write_text("print('ok')", encoding="utf-8")
    acquired_during_upload = []

    class Client:
        token = "test-token"

        def ensure_directory(self, _path):
            pass

        def upload_file(self, _path, _remote):
            def acquire_state_lock():
                acquired = sync.STATE_LOCK.acquire(timeout=0.5)
                acquired_during_upload.append(acquired)
                if acquired:
                    sync.STATE_LOCK.release()

            worker = threading.Thread(target=acquire_state_lock)
            worker.start()
            worker.join(timeout=1)
            return True

    assert sync.sync_once(Client(), tmp_path) == 2
    assert acquired_during_upload == [True, True]


def test_same_path_watcher_uploads_are_serialized(
    monkeypatch,
    tmp_path,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    path = tmp_path / "script.py"
    path.write_text("print('ok')", encoding="utf-8")
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    class Client:
        token = "test-token"

        def upload_file(self, _path, _remote):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with active_lock:
                active -= 1
            return True

    handler = sync.SyncHandler(Client(), tmp_path, None)
    threads = [
        threading.Thread(
            target=handler._do_sync_file,
            args=(str(path),),
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    handler.stop()

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1


def test_remote_loop_logs_one_offline_online_pair_and_rescans(
    monkeypatch,
    tmp_path,
    capsys,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    state = session_guard.OFFLINE_STATE
    with state._lock:
        state._offline = False
        state._backoff = 2.0
        state.recovered.clear()

    error = requests.HTTPError("gateway error")
    error.response = type("Response", (), {"status_code": 502})()
    attempts = iter((error, []))

    def list_remote_tree(*_args):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        sync,
        "list_remote_tree_bfs",
        list_remote_tree,
    )
    monkeypatch.setattr(state, "get_backoff", lambda: 0)
    rescans = []
    monkeypatch.setattr(
        sync,
        "sync_once",
        lambda *_args, **_kwargs: rescans.append(True),
    )

    class Client:
        token = "test-token"

    class StopAfterRecovery:
        calls = 0

        def wait(self, _timeout):
            self.calls += 1
            return self.calls >= 2

        def set(self):
            pass

        def is_set(self):
            return False

    stop_event = StopAfterRecovery()
    monkeypatch.setattr(session_guard, "SESSION_DEAD", threading.Event())
    sync.remote_sync_loop(Client(), tmp_path, stop_event)
    output = capsys.readouterr().out

    assert output.count("[OFFLINE]") == 1
    assert output.count("[ONLINE]") == 1
    assert rescans == [True]


@pytest.mark.parametrize("refresh_age,expected", [(59, False), (61, True)])
def test_state_lock_lease_uses_sixty_second_refresh(
    monkeypatch,
    tmp_path,
    refresh_age,
    expected,
):
    _use_temp_runner_home(monkeypatch, tmp_path)
    lock_path = runner_paths.lock_file(tmp_path)
    now = time.time()
    lock_path.write_text(
        json.dumps({
            "pid": os.getpid(),
            "host": "another-host",
            "created": now,
            "refreshed": now - refresh_age,
        }),
        encoding="utf-8",
    )

    assert runner_paths._lock_is_stale(lock_path, now=now) is expected
