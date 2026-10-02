"""Per-project paths under ~/.kaggle-runner.

Sync state, URL storage, locks, and heartbeats are kept under RUNNER_HOME
(~/.kaggle-runner), keyed by a stable hash of the project path.
"""

import contextlib
import hashlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path


RUNNER_HOME = Path.home() / ".kaggle-runner"
STATE_DIR_NAME = "state"
URL_DIR_NAME = "urls"


def runner_home() -> Path:
    override = os.getenv("KAGGLE_RUNNER_HOME")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path.home() / ".kaggle-runner"


def is_within(path, root) -> bool:
    """Return whether path resolves inside root, including equality."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def is_runner_home_path(project_root, path) -> bool:
    """Return whether path is the configured runner home or one of its children."""
    return is_within(path, runner_home()) and is_within(
        runner_home(), project_root
    )


def project_key(root) -> str:
    """Stable id for a project folder.

    sha1 of the normalized lowercase absolute path, first 12 hex
    chars. Lowercased so the same folder maps to the same key
    regardless of Windows path casing.
    """
    normalized = os.path.normpath(
        os.path.abspath(os.fspath(root))
    ).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def state_file(root) -> Path:
    """Path of this project's sync-state JSON (dirs created lazily)."""
    directory = runner_home() / STATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (project_key(root) + ".json")


def url_file(root) -> Path:
    """Path of this project's saved server-URL file (dirs lazy)."""
    directory = runner_home() / URL_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (project_key(root) + ".url")


def lock_file(root) -> Path:
    """Path of the cross-process lock file for a project."""
    directory = runner_home() / STATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (project_key(root) + ".lock")


def heartbeat_file(root) -> Path:
    """Path of the heartbeat file for a project."""
    directory = runner_home() / STATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (project_key(root) + ".heartbeat")


def chmod_private(path):
    """Best-effort 0o600 on files that may hold sensitive state."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError, AttributeError):
        pass


def write_text_atomic(path, text: str):
    """Write utf-8 (no BOM) atomically via tmp + replace; chmod 600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    chmod_private(temporary)
    os.replace(temporary, path)
    chmod_private(path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def read_text(path) -> str:
    """Read utf-8 text; return empty string when missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


STATE_LOCK_RETRY_SECONDS = 0.05
STATE_LOCK_TIMEOUT_SECONDS = 10.0
STATE_LOCK_STALE_SECONDS = 60.0
STATE_LOCK_REFRESH_SECONDS = 15.0


def _read_lock_metadata(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        if isinstance(metadata, dict):
            return metadata
    except (OSError, ValueError):
        pass
    return None


def _pid_is_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.GetLastError() == 5
            try:
                exit_code = ctypes.c_ulong()
                return bool(
                    kernel32.GetExitCodeProcess(
                        handle,
                        ctypes.byref(exit_code),
                    )
                    and exit_code.value == 259
                )
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _lock_is_stale(path, now=None):
    now = time.time() if now is None else now
    metadata = _read_lock_metadata(path)
    if metadata is not None:
        try:
            refreshed = float(metadata["refreshed"])
        except (KeyError, TypeError, ValueError):
            refreshed = None
        if refreshed is not None and now - refreshed > STATE_LOCK_STALE_SECONDS:
            return True
        if (
            metadata.get("host") == socket.gethostname()
            and not _pid_is_alive(metadata.get("pid"))
        ):
            return True
        return False

    try:
        return now - os.stat(path).st_mtime > STATE_LOCK_STALE_SECONDS
    except OSError:
        return False


def _same_lock_owner(metadata, owner):
    return (
        metadata is not None
        and metadata.get("pid") == owner["pid"]
        and metadata.get("host") == owner["host"]
        and metadata.get("created") == owner["created"]
    )


def _refresh_lock(path, owner, stop_event):
    while not stop_event.wait(STATE_LOCK_REFRESH_SECONDS):
        metadata = _read_lock_metadata(path)
        if not _same_lock_owner(metadata, owner):
            return
        metadata["refreshed"] = time.time()
        try:
            write_text_atomic(path, json.dumps(metadata))
        except OSError:
            continue


@contextlib.contextmanager
def state_lock(project_root, timeout=STATE_LOCK_TIMEOUT_SECONDS):
    """Cross-process mutex for a project's state file."""
    path = lock_file(project_root)
    deadline = time.time() + timeout
    acquired = False
    owner = None
    refresh_stop = None
    refresh_thread = None

    while not acquired:
        fd = None
        try:
            fd = os.open(
                os.fspath(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except (FileExistsError, OSError):
            fd = None

        if fd is not None:
            try:
                now = time.time()
                owner = {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "created": now,
                    "refreshed": now,
                }
                payload = json.dumps(owner).encode("utf-8")
                if os.write(fd, payload) != len(payload):
                    raise OSError("incomplete state-lock metadata write")
                os.fsync(fd)
            except OSError:
                os.close(fd)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            os.close(fd)
            acquired = True
            break

        if _lock_is_stale(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue

        if time.time() >= deadline:
            raise TimeoutError(f"Timed out waiting for state lock: {path}")

        time.sleep(STATE_LOCK_RETRY_SECONDS)

    try:
        refresh_stop = threading.Event()
        refresh_thread = threading.Thread(
            target=_refresh_lock,
            args=(path, owner, refresh_stop),
            name="kaggle-state-lock-refresh",
            daemon=True,
        )
        refresh_thread.start()
        yield path
    finally:
        if refresh_stop is not None:
            refresh_stop.set()
        if refresh_thread is not None:
            refresh_thread.join(timeout=1)
        try:
            metadata = _read_lock_metadata(path)
            if _same_lock_owner(metadata, owner):
                os.unlink(path)
        except OSError:
            pass
