"""Protocol integration tests against a real local Jupyter Server."""

import hashlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import requests
import pytest

from kaggle_runner import kernel_exec, pull, runner_paths, sync


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _RealJupyter:
    def __init__(self, **values):
        self.__dict__.update(values)

    def __repr__(self):
        return "RealJupyterServer(<credentials and URL redacted>)"


@pytest.fixture(scope="module")
def real_jupyter(tmp_path_factory):
    root = tmp_path_factory.mktemp("real-jupyter-root")
    work_root = root / "local-project"
    work_root.mkdir()
    runtime = root / "runtime"
    runtime.mkdir()
    token = secrets.token_urlsafe(24)
    base_path = f"/k/test/{token}/proxy/"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}{base_path.rstrip('/')}"
    env = dict(os.environ)
    env.update({
        "JUPYTER_CONFIG_DIR": str(runtime / "config"),
        "JUPYTER_DATA_DIR": str(runtime / "data"),
        "JUPYTER_RUNTIME_DIR": str(runtime / "run"),
        "PYTHONUNBUFFERED": "1",
    })
    command = [
        sys.executable,
        "-m",
        "jupyter_server",
        "--no-browser",
        "--ServerApp.ip=127.0.0.1",
        f"--ServerApp.port={port}",
        "--ServerApp.port_retries=0",
        "--ServerApp.open_browser=False",
        f"--ServerApp.root_dir={root}",
        f"--ServerApp.base_url={base_path}",
        "--ServerApp.allow_remote_access=True",
        "--ServerApp.disable_check_xsrf=True",
        f"--IdentityProvider.token={token}",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pytest.fail("Could not start local Jupyter Server.")

    headers = {"Authorization": f"token {token}"}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("Local Jupyter Server exited during startup.")
        try:
            response = requests.get(
                f"{base_url}/api",
                headers=headers,
                timeout=1,
            )
            if response.status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(0.25)
    else:
        process.terminate()
        process.wait(timeout=10)
        pytest.fail("Local Jupyter Server did not become ready.")

    server = _RealJupyter(
        root=root,
        work_root=work_root,
        token=token,
        url=base_url,
        process=process,
        headers=headers,
    )
    yield server

    try:
        response = requests.get(
            f"{base_url}/api/kernels",
            headers=headers,
            timeout=5,
        )
        if response.ok:
            for kernel in response.json():
                requests.delete(
                    f"{base_url}/api/kernels/{kernel['id']}",
                    headers=headers,
                    timeout=5,
                )
    except requests.RequestException:
        pass
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@pytest.fixture
def real_project(monkeypatch, tmp_path, real_jupyter):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path.parent / f"{tmp_path.name}-runner-home"
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(home))
    monkeypatch.setattr(runner_paths, "runner_home", lambda: home)
    sync.bind_state_lock(project)
    yield project
    try:
        sync.JupyterClient(real_jupyter.url).delete_tree("local-project")
    except requests.RequestException:
        pass
    return project


def _create_kernel(client, project):
    from kaggle_runner.kernel_manager import KernelManager

    manager = KernelManager(client, project)
    kernel_id = manager.create_ephemeral_kernel()
    return manager, kernel_id


def _kernel_exists(server, kernel_id):
    response = requests.get(
        f"{server.url}/api/kernels/{kernel_id}",
        headers=server.headers,
        timeout=5,
    )
    return response.status_code == 200


def test_real_server_contents_range_and_websocket(real_jupyter, real_project):
    client = sync.JupyterClient(real_jupyter.url)
    remote = "local-project/protocol.txt"
    payload = b"real jupyter contents and range"
    client.ensure_directory("local-project")
    client.upload_bytes(payload, remote)

    response = client.request_session().get(
        f"{client.base_url}/files/{remote}",
        headers={"Range": "bytes=0-3"},
        timeout=10,
    )
    assert response.status_code == 206
    assert response.content == payload[:4]

    manager, kernel_id = _create_kernel(client, real_project)
    output = []
    try:
        result = kernel_exec.execute_in_kernel(
            client.websocket_base,
            client.base_url,
            client.token,
            kernel_id,
            "print('real-websocket-ok')",
            on_text=output.append,
            user_expressions={"answer": "40 + 2"},
            timeout=30,
        )
        assert result["status"] == "ok"
        assert "real-websocket-ok" in "".join(output)
        assert result["user_expressions"]["answer"]["data"]["text/plain"] == "42"
    finally:
        manager.shutdown_ephemeral_kernel()


@pytest.mark.slow
@pytest.mark.timeout(150)
def test_real_server_silent_execution_pings_for_ninety_seconds(
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    manager, kernel_id = _create_kernel(client, real_project)
    started = time.monotonic()
    try:
        result = kernel_exec.execute_in_kernel(
            client.websocket_base,
            client.base_url,
            client.token,
            kernel_id,
            "import time; time.sleep(90)",
            on_text=lambda _text: None,
            timeout=120,
            poll_interval=1,
            ping_interval=20,
        )
        assert result["status"] == "ok"
        assert time.monotonic() - started >= 90
    finally:
        manager.shutdown_ephemeral_kernel()


@pytest.mark.slow
def test_real_server_deleted_kernel_is_reported_lost(
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    manager, kernel_id = _create_kernel(client, real_project)
    deleted_status = []

    def delete_kernel():
        response = requests.delete(
            f"{client.base_url}/api/kernels/{kernel_id}",
            headers={"Authorization": f"token {client.token}"},
            timeout=5,
        )
        deleted_status.append(response.status_code)

    timer = threading.Timer(
        3,
        delete_kernel,
    )
    timer.start()
    try:
        result = kernel_exec.execute_in_kernel(
            client.websocket_base,
            client.base_url,
            client.token,
            kernel_id,
            "import time; time.sleep(30)",
            on_text=lambda _text: None,
            timeout=20,
            poll_interval=1,
            ping_interval=1,
        )
        assert result["status"] == "lost"
    finally:
        timer.join(timeout=6)
        manager.shutdown_ephemeral_kernel()
    assert deleted_status == [204]


@pytest.mark.slow
def test_real_server_websocket_reconnect_does_not_resend(
    monkeypatch,
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    manager, kernel_id = _create_kernel(client, real_project)
    real_jupyter.work_root.mkdir(parents=True, exist_ok=True)
    marker = real_jupyter.work_root / "execution-count.txt"
    code = (
        f"from pathlib import Path; p=Path({str(marker)!r}); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
        "_KAGGLE_RUN_RC=0; import time; time.sleep(5); print('finished-once')"
    )
    create_connection = kernel_exec.websocket.create_connection
    websocket_urls = []

    class DropOnce:
        def __init__(self, socket):
            self.socket = socket
            self.dropped = False

        def send(self, data):
            return self.socket.send(data)

        def recv(self):
            if not self.dropped:
                self.dropped = True
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.05)
                if not marker.exists():
                    raise RuntimeError("execution did not reach the server")
                time.sleep(0.2)
                self.socket.close()
                raise kernel_exec.websocket.WebSocketConnectionClosedException(
                    "forced test reconnect"
                )
            return self.socket.recv()

        def __getattr__(self, name):
            return getattr(self.socket, name)

    def connect(url, **kwargs):
        websocket_urls.append(url)
        socket = create_connection(url, **kwargs)
        if len(websocket_urls) == 1:
            return DropOnce(socket)
        return socket

    monkeypatch.setattr(
        kernel_exec.websocket,
        "create_connection",
        connect,
    )
    output = []
    try:
        result = kernel_exec.execute_in_kernel(
            client.websocket_base,
            client.base_url,
            client.token,
            kernel_id,
            code,
            on_text=output.append,
            timeout=30,
            poll_interval=1,
        )
        assert result["status"] == "ok", (
            result["error_text"],
            result["user_expressions"],
        )
        assert marker.read_text(encoding="utf-8") == "x"
        assert isinstance(output, list)
        assert len(websocket_urls) >= 2
        session_ids = [
            url.split("session_id=", 1)[1]
            for url in websocket_urls
        ]
        assert session_ids[0] == session_ids[1]
    finally:
        manager.shutdown_ephemeral_kernel()


def test_real_jupyter_stream_download_matches_hash(real_jupyter, real_project):
    client = sync.JupyterClient(real_jupyter.url)
    local_file = real_project / "binary.bin"
    payload = (bytes(range(256)) * 4096)
    local_file.write_bytes(payload)

    assert client.upload_file(local_file, "local-project/binary.bin")
    downloaded = real_project / "downloaded.bin"
    client.download_file(
        "local-project/binary.bin",
        downloaded,
        len(payload),
    )
    assert hashlib.sha256(downloaded.read_bytes()).digest() == (
        hashlib.sha256(payload).digest()
    )


def test_real_jupyter_large_chunked_upload_has_no_remote_part(
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    local_file = real_project / "sixty-meg.bin"
    digest = hashlib.sha256()
    block = bytes(range(256)) * 4096
    with open(local_file, "wb") as handle:
        for _ in range(60):
            handle.write(block)
            digest.update(block)

    assert local_file.stat().st_size == 60 * 1024 * 1024
    assert client.upload_file(local_file, "local-project/sixty-meg.bin")
    response = client.request_session().get(
        client.api_url(
            "local-project/sixty-meg.bin.kaggle-sync-part"
        ),
        timeout=10,
    )
    assert response.status_code == 404
    response = client.request_session().get(
        f"{client.base_url}/files/local-project/sixty-meg.bin",
        stream=True,
        timeout=20,
    )
    response.raise_for_status()
    remote_digest = hashlib.sha256()
    for chunk in response.iter_content(1024 * 1024):
        remote_digest.update(chunk)
    response.close()
    assert remote_digest.digest() == digest.digest()


@pytest.mark.slow
def test_real_initial_sync_skips_150_mb_and_secret_even_when_negated(
    real_jupyter,
    real_project,
    capsys,
):
    client = sync.JupyterClient(real_jupyter.url)
    (real_project / ".kagglesyncignore").write_text(
        "!.env\n",
        encoding="utf-8",
    )
    (real_project / ".env").write_text("NOT_A_SECRET_VALUE\n", encoding="utf-8")
    oversized = real_project / "oversized.bin"
    with open(oversized, "wb") as handle:
        handle.truncate(150 * 1024 * 1024)

    sync.initial_sync(client, real_project)
    output = capsys.readouterr().out

    assert "150.0 MB" in output
    for relative in (".env", "oversized.bin"):
        response = client.request_session().get(
            client.api_url(f"local-project/{relative}"),
            timeout=10,
        )
        assert response.status_code == 404


@pytest.mark.slow
def test_real_watcher_debounces_edits_then_handles_rename_and_delete(
    real_jupyter,
    real_project,
):
    from types import SimpleNamespace

    client = sync.JupyterClient(real_jupyter.url)
    uploads = []
    original_upload = client.upload_file

    def counted_upload(path, remote):
        uploads.append(remote)
        return original_upload(path, remote)

    client.upload_file = counted_upload
    handler = sync.SyncHandler(client, real_project, None)
    path = real_project / "debounced.txt"
    path.write_text("first", encoding="utf-8")
    handler.sync_file(path)
    time.sleep(0.2)
    path.write_text("last-write-wins", encoding="utf-8")
    handler.sync_file(path)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not uploads:
        time.sleep(0.1)
    assert uploads == ["local-project/debounced.txt"]
    assert client.download_bytes("local-project/debounced.txt") == (
        b"last-write-wins"
    )

    renamed = real_project / "renamed.txt"
    path.rename(renamed)
    handler.on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(path),
            dest_path=str(renamed),
        )
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(uploads) < 2:
        time.sleep(0.1)
    assert uploads[-1] == "local-project/renamed.txt"
    assert client.download_bytes("local-project/renamed.txt") == (
        b"last-write-wins"
    )

    renamed.unlink()
    handler.on_deleted(
        SimpleNamespace(is_directory=False, src_path=str(renamed))
    )
    response = client.request_session().get(
        client.api_url("local-project/renamed.txt"),
        timeout=10,
    )
    assert response.status_code == 404
    assert handler.stop() == []


@pytest.mark.slow
def test_real_remote_change_downloads_once_without_ping_pong(
    monkeypatch,
    real_jupyter,
    real_project,
):
    from watchdog.observers import Observer

    client = sync.JupyterClient(real_jupyter.url)
    local = real_project / "remote.txt"
    local.write_text("initial", encoding="utf-8")
    sync.initial_sync(client, real_project)
    client.upload_bytes(b"remote-version", "local-project/remote.txt")

    uploads = []
    downloads = []
    cycles = []
    original_upload = client.upload_file
    original_download = client.download_file
    original_list = sync.list_remote_tree_bfs

    def counted_upload(path, remote):
        uploads.append(remote)
        return original_upload(path, remote)

    def counted_download(remote, path, size=None):
        downloads.append(remote)
        return original_download(remote, path, size=size)

    client.upload_file = counted_upload
    client.download_file = counted_download

    def counted_list(remote_client, project_root):
        cycles.append(True)
        return original_list(remote_client, project_root)

    monkeypatch.setattr(sync, "list_remote_tree_bfs", counted_list)
    handler = sync.SyncHandler(client, real_project, None)
    observer = Observer()
    observer.schedule(handler, str(real_project), recursive=True)
    observer.start()
    monkeypatch.setattr(sync, "REMOTE_SYNC_INTERVAL", 0.1)
    monkeypatch.setattr(sync, "REMOTE_POLL_IDLE_CYCLES", 2)
    sync._POLL_RESET.clear()
    with sync.session_guard.OFFLINE_STATE._lock:
        sync.session_guard.OFFLINE_STATE._offline = False
        sync.session_guard.OFFLINE_STATE._backoff = 2.0
        sync.session_guard.OFFLINE_STATE.recovered.clear()
    stop_event = threading.Event()
    stopper = threading.Timer(0.8, stop_event.set)
    stopper.start()
    try:
        sync.remote_sync_loop(client, real_project, stop_event)
    finally:
        stopper.join(timeout=5)
        observer.stop()
        observer.join(timeout=5)
        handler.stop()

    assert local.read_bytes() == b"remote-version"
    assert len(cycles) >= 3
    assert downloads == ["local-project/remote.txt"]
    assert uploads == []


def test_real_pull_resumes_range_and_directory_fetch_clears_pending(
    monkeypatch,
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    relative = "restored/checkpoint.bin"
    payload = bytes(range(256)) * 8192
    client.upload_bytes(payload, f"local-project/{relative}")

    part_path = real_project / "resumed.bin.kaggle-pull.part"
    resume_at = 100_000
    part_path.write_bytes(payload[:resume_at])
    result = pull.download_raw(
        client,
        pull.remote_url_for_file(client, f"local-project/{relative}"),
        part_path,
        len(payload),
        relative,
    )
    assert result == len(payload)
    assert part_path.read_bytes() == payload

    pending = {relative: {"size": len(payload)}}
    manager = sync.StateManager(real_project)
    manager.save_pending_remote(pending)
    targets, problems = pull.expand_specs(
        client,
        real_project,
        ["restored"],
    )
    assert targets == [relative]
    assert problems == []

    local_destination = real_project / relative
    args = SimpleNamespace(dest=None, force=True, include_ignored=False)
    assert pull.pull_one(
        client,
        real_project,
        relative,
        len(payload),
        args,
        None,
    )
    assert local_destination.read_bytes() == payload
    assert manager.load_pending_remote() == {}

    uploads = []
    original_upload = client.upload_file
    monkeypatch.setattr(
        client,
        "upload_file",
        lambda path, remote: (
            uploads.append(remote) or original_upload(path, remote)
        ),
    )
    handler = sync.SyncHandler(client, real_project, None)
    handler._do_sync_file(str(local_destination))
    handler.stop()
    assert uploads == []


def test_real_pull_restarts_when_server_ignores_range(
    real_jupyter,
    real_project,
    capsys,
):
    client = sync.JupyterClient(real_jupyter.url)
    relative = "outputs/no-range.bin"
    payload = b"range-ignored" * 100_000
    client.upload_bytes(payload, f"local-project/{relative}")
    original_session = client.request_session()

    class RangeIgnoringSession:
        def get(self, url, headers=None, **kwargs):
            return original_session.get(
                url,
                headers={
                    key: value
                    for key, value in (headers or {}).items()
                    if key.lower() != "range"
                },
                **kwargs,
            )

    client.request_session = lambda: RangeIgnoringSession()
    part_path = real_project / "ignore-range.bin.kaggle-pull.part"
    part_path.write_bytes(payload[:500])

    downloaded = pull.download_raw(
        client,
        pull.remote_url_for_file(client, f"local-project/{relative}"),
        part_path,
        len(payload),
        relative,
    )

    assert downloaded == len(payload)
    assert part_path.read_bytes() == payload
    assert "Server ignored resume" in capsys.readouterr().out


def test_real_missing_file_404s_do_not_expire_sync_session(
    monkeypatch,
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    stop_event = threading.Event()
    calls = []

    def list_with_missing_file(_client, _root):
        response = client.request_session().get(
            client.api_url("local-project/not-found.bin"),
            timeout=10,
        )
        response.raise_for_status()

    original_list = sync.list_remote_tree_bfs

    def missing_file_storm(_client, root):
        calls.append(True)
        list_with_missing_file(_client, root)
        return original_list(client, root)

    monkeypatch.setattr(sync, "list_remote_tree_bfs", missing_file_storm)
    monkeypatch.setattr(sync, "REMOTE_SYNC_INTERVAL", 0.05)

    class StopAfterThree:
        def __init__(self):
            self.waits = 0

        def wait(self, _timeout):
            self.waits += 1
            return self.waits >= 3

        def set(self):
            stop_event.set()

        def is_set(self):
            return False

    previous_dead = sync.session_guard.SESSION_DEAD
    monkeypatch.setattr(
        sync.session_guard,
        "SESSION_DEAD",
        threading.Event(),
    )
    sync.remote_sync_loop(client, real_project, StopAfterThree())

    assert len(calls) == 3
    assert not sync.session_guard.SESSION_DEAD.is_set()
    monkeypatch.setattr(sync.session_guard, "SESSION_DEAD", previous_dead)


@pytest.mark.slow
def test_real_timed_outage_emits_one_pair_and_rescans_after_recovery(
    monkeypatch,
    real_jupyter,
    real_project,
    capsys,
):
    client = sync.JupyterClient(real_jupyter.url)
    (real_project / "after-outage.txt").write_text(
        "kept-local",
        encoding="utf-8",
    )
    state = sync.session_guard.OFFLINE_STATE
    with state._lock:
        state._offline = False
        state._backoff = 2.0
        state.recovered.clear()
    monkeypatch.setattr(state, "get_backoff", lambda: 0.05)
    monkeypatch.setattr(sync, "REMOTE_SYNC_INTERVAL", 0.05)

    started = time.monotonic()
    stop_event = threading.Event()
    uploaded = []
    original_upload = client.upload_file
    client.upload_file = lambda path, remote: (
        uploaded.append(remote) or original_upload(path, remote)
    )

    def outage_then_recovery(_client, _root):
        if time.monotonic() - started < 20:
            error = requests.HTTPError("simulated gateway outage")
            error.response = SimpleNamespace(status_code=502)
            raise error
        stop_event.set()
        return []

    monkeypatch.setattr(sync, "list_remote_tree_bfs", outage_then_recovery)
    sync.remote_sync_loop(client, real_project, stop_event)
    output = capsys.readouterr().out

    assert time.monotonic() - started >= 20
    assert output.count("[OFFLINE]") == 1
    assert output.count("[ONLINE]") == 1
    assert uploaded == ["local-project/after-outage.txt"]


def test_real_doctor_cleans_remote_tree_and_kernel_on_interrupt(
    monkeypatch,
    real_jupyter,
    real_project,
):
    from kaggle_runner import doctor

    client = sync.JupyterClient(real_jupyter.url)
    before = requests.get(
        f"{client.base_url}/api/kernels",
        headers=real_jupyter.headers,
        timeout=10,
    )
    before.raise_for_status()
    before_ids = {item["id"] for item in before.json()}

    def interrupt_execute(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(doctor, "execute_in_kernel", interrupt_execute)
    results = doctor.run_remote_checks(client, real_project)
    assert any(
        "Interrupted" in result.message for result in results
    ), [(result.name, result.status, result.message) for result in results]
    assert any(
        result.name == "Cleanup" and result.status == "PASS"
        for result in results
    ), [(result.name, result.status, result.message) for result in results]

    after = requests.get(
        f"{client.base_url}/api/kernels",
        headers=real_jupyter.headers,
        timeout=10,
    )
    after.raise_for_status()
    assert {item["id"] for item in after.json()} == before_ids
    root_listing = client.request_session().get(
        client.api_url("local-project"),
        params={"content": 1},
        timeout=10,
    )
    if root_listing.status_code == 200:
        assert not any(
            "kaggle-doctor-" in entry.get("path", "")
            for entry in root_listing.json().get("content", [])
        )


def test_real_healthy_doctor_checks_protocol_and_leaves_empty_tree(
    monkeypatch,
    real_jupyter,
    real_project,
    capsys,
):
    from kaggle_runner import doctor, kernel_exec, urlstore

    client = sync.JupyterClient(real_jupyter.url)
    (real_project / ".kagglesyncignore").write_text(
        "data/\n",
        encoding="utf-8",
    )
    heartbeat = urlstore.Heartbeat(real_project, interval=0.1)
    heartbeat.start()
    urlstore.save_url(real_project, real_jupyter.url)
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline
        and urlstore.sync_state(real_project) != "running"
    ):
        time.sleep(0.05)

    def execute_with_local_environment(*args, **kwargs):
        call_args = list(args)
        code = call_args[4]
        expressions = kwargs.get("user_expressions") or {}
        if "root_probe" in expressions:
            prefix = "os.path.exists('/kaggle/working/"
            start = code.index(prefix) + len(prefix)
            end = code.index("')", start)
            remote_relative = code[start:end]
            local_probe = real_jupyter.root / remote_relative
            code = code.replace(
                f"os.path.exists('/kaggle/working/{remote_relative}')",
                f"os.path.exists({str(local_probe)!r})",
            )
            call_args[4] = code
        elif "_INSPECT_RESULT = json.dumps(info)" in code:
            call_args[4] = code.replace(
                "_INSPECT_RESULT = json.dumps(info)",
                'info["gpu"] = "Local test GPU"\n'
                'info["internet"] = True\n'
                "_INSPECT_RESULT = json.dumps(info)",
            )
        return kernel_exec.execute_in_kernel(*call_args, **kwargs)

    monkeypatch.setattr(
        doctor,
        "execute_in_kernel",
        execute_with_local_environment,
    )
    try:
        result = doctor.run_doctor(
            project_root=real_project,
            url=real_jupyter.url,
            deep=False,
            as_json=False,
            fix=False,
        )
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=5)

    output = capsys.readouterr().out
    assert result == 0
    assert "[FAIL]" not in output
    root_listing = client.request_session().get(
        client.api_url("local-project"),
        params={"content": 1},
        timeout=10,
    )
    if root_listing.status_code == 200:
        assert root_listing.json().get("content", []) == []


def test_real_onedrive_like_project_prints_warning(tmp_path, capsys, monkeypatch):
    project = tmp_path / "OneDrive" / "project"
    monkeypatch.delenv("KAGGLE_SYNC_ALLOW_ONEDRIVE", raising=False)

    sync.warn_if_onedrive(project)

    assert "WARNING: this project appears to be inside OneDrive" in (
        capsys.readouterr().out
    )


def _run_cli(monkeypatch, real_jupyter, project, argv):
    from kaggle_runner import run

    from kaggle_runner.urlstore import save_url

    monkeypatch.chdir(project)
    monkeypatch.setattr(run, "REMOTE_ROOT", str(real_jupyter.work_root))
    save_url(project, real_jupyter.url)
    try:
        run.main(argv)
    except SystemExit as error:
        return error.code
    raise AssertionError("kaggle-run did not exit")


def test_real_runner_reloads_helpers_passes_args_and_returns_exit_code(
    monkeypatch,
    real_jupyter,
    real_project,
    capsys,
):
    from kaggle_runner import sync

    helper = real_project / "helper.py"
    script = real_project / "train.py"
    helper.write_text("VALUE = 'old-helper'\n", encoding="utf-8")
    script.write_text(
        "import helper, sys\n"
        "print(helper.VALUE)\n"
        "print(repr(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    result = _run_cli(
        monkeypatch,
        real_jupyter,
        real_project,
        ["train.py", "one arg", "ü"],
    )
    assert result == 0
    assert "old-helper" in capsys.readouterr().out

    helper.write_text("VALUE = 'new-helper'\n", encoding="utf-8")
    result = _run_cli(
        monkeypatch,
        real_jupyter,
        real_project,
        ["train.py", "one arg", "ü"],
    )
    output = capsys.readouterr().out
    assert result == 0
    from kaggle_runner.sync import JupyterClient

    remote_helper = JupyterClient(real_jupyter.url).download_bytes(
        "local-project/helper.py"
    )
    assert remote_helper.replace(b"\r\n", b"\n") == b"VALUE = 'new-helper'\n"
    assert "new-helper" in output
    assert "one arg" in output
    assert "ü" in output

    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    result = _run_cli(
        monkeypatch,
        real_jupyter,
        real_project,
        ["train.py"],
    )
    assert result == 3

    state_manager = sync.StateManager(real_project)
    persistent_kernel = state_manager.load_runner_kernel()
    assert persistent_kernel

    kernel_list = requests.get(
        f"{real_jupyter.url}/api/kernels",
        headers=real_jupyter.headers,
        timeout=5,
    )
    kernel_list.raise_for_status()
    kernels_before_ephemeral_run = {
        item["id"] for item in kernel_list.json()
    }
    result = _run_cli(
        monkeypatch,
        real_jupyter,
        real_project,
        ["--no-sync", "--new-kernel", "train.py"],
    )
    assert result == 3
    assert _kernel_exists(real_jupyter, persistent_kernel)
    kernel_list = requests.get(
        f"{real_jupyter.url}/api/kernels",
        headers=real_jupyter.headers,
        timeout=5,
    )
    assert {
        item["id"] for item in kernel_list.json()
    } == kernels_before_ephemeral_run

    result = _run_cli(
        monkeypatch,
        real_jupyter,
        real_project,
        ["--stop"],
    )
    assert result == 0
    assert not _kernel_exists(real_jupyter, persistent_kernel)


def test_real_runner_uses_owned_kernel_not_notebook_kernel(
    real_jupyter,
    real_project,
):
    client = sync.JupyterClient(real_jupyter.url)
    client.ensure_directory("local-project")
    notebook = (
        '{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}'
    ).encode("utf-8")
    client.upload_bytes(notebook, "local-project/notebook.ipynb")
    response = client.request_session().post(
        f"{client.base_url}/api/sessions",
        json={
            "path": "local-project/notebook.ipynb",
            "name": "python3",
            "type": "notebook",
        },
        timeout=20,
    )
    response.raise_for_status()
    notebook_kernel = response.json()["kernel"]["id"]

    from kaggle_runner.kernel_manager import KernelManager

    manager = KernelManager(client, real_project)
    runner_kernel = manager.ensure_runner_kernel()
    try:
        assert runner_kernel != notebook_kernel
    finally:
        manager.stop_runner_kernel()
        requests.delete(
            f"{client.base_url}/api/sessions/"
            f"{response.json()['id']}",
            headers={"Authorization": f"token {client.token}"},
            timeout=10,
        )


def test_real_runner_handles_project_paths_with_spaces_and_unicode(
    monkeypatch,
    real_jupyter,
    real_project,
    capsys,
):
    project = real_project / "project with spaces - café"
    project.mkdir()
    (project / "train.py").write_text(
        "print('unicode-project-ok')\n",
        encoding="utf-8",
    )

    result = _run_cli(
        monkeypatch,
        real_jupyter,
        project,
        ["--no-sync", "train.py"],
    )

    assert result == 0
    assert "unicode-project-ok" in capsys.readouterr().out


def test_two_real_runner_processes_refuse_busy_kernel(
    monkeypatch,
    real_jupyter,
    real_project,
    capsys,
):
    from kaggle_runner import run

    script = real_project / "slow.py"
    script.write_text(
        "import time; print('started'); time.sleep(5)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(real_project)
    monkeypatch.setattr(run, "REMOTE_ROOT", str(real_jupyter.work_root))
    from kaggle_runner.urlstore import save_url

    save_url(real_project, real_jupyter.url)
    result = []

    def first_runner():
        try:
            run.main(["--no-sync", "slow.py"])
        except SystemExit as error:
            result.append(error.code)

    thread = threading.Thread(target=first_runner)
    thread.start()
    lock_path = runner_paths.lock_file(real_project).with_suffix(
        ".runner.lock"
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not lock_path.exists():
        time.sleep(0.1)

    assert lock_path.exists()
    with pytest.raises(SystemExit) as busy:
        run.main(["--no-sync", "slow.py"])
    assert busy.value.code == 1
    assert "Another kaggle-run is still active" in capsys.readouterr().out

    thread.join(timeout=30)
    assert not thread.is_alive()
    assert result == [0]


def _process_is_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(
            ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            )
            and exit_code.value == 259
        )
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.slow
def test_real_runner_interrupt_stops_remote_child(
    monkeypatch,
    real_jupyter,
    real_project,
):
    from kaggle_runner import run

    marker = real_jupyter.work_root / "child-pid.txt"
    script = real_project / "interruptible.py"
    script.write_text(
        "import os, time\n"
        "open('child-pid.txt', 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    print('running', flush=True)\n"
        "    time.sleep(0.25)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(real_project)
    monkeypatch.setattr(run, "REMOTE_ROOT", str(real_jupyter.work_root))
    from kaggle_runner.urlstore import save_url

    save_url(real_project, real_jupyter.url)
    interrupted = []

    def send_ctrl_c():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        if not marker.exists():
            return
        interrupted.append(True)
        signal.raise_signal(signal.SIGINT)

    thread = threading.Thread(target=send_ctrl_c)
    thread.start()
    try:
        run.main(["--no-sync", "interruptible.py"])
    except SystemExit as error:
        assert error.code == 130
    else:
        raise AssertionError("kaggle-run did not report Ctrl+C exit code 130")
    thread.join(timeout=15)

    assert interrupted and interrupted[0] < 300
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _process_is_alive(child_pid):
        time.sleep(0.1)
    assert not _process_is_alive(child_pid)
