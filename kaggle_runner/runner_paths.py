"""Per-project paths under ~/.kaggle-runner.

Sync state, URL storage, locks, and heartbeats are kept under RUNNER_HOME
(~/.kaggle-runner), keyed by a stable hash of the project path.
"""

import contextlib
import hashlib
import os
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
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(text)
    chmod_private(temporary)
    os.replace(temporary, path)
    chmod_private(path)


def read_text(path) -> str:
    """Read utf-8 text; return empty string when missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


STATE_LOCK_RETRY_SECONDS = 0.05
STATE_LOCK_TIMEOUT_SECONDS = 10.0
STATE_LOCK_STALE_SECONDS = 30.0


def _lock_age_seconds(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            stamp = float(f.read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            return None
    try:
        return time.time() - stamp
    except Exception:
        return None


@contextlib.contextmanager
def state_lock(project_root, timeout=STATE_LOCK_TIMEOUT_SECONDS):
    """Cross-process mutex for a project's state file."""
    path = lock_file(project_root)
    deadline = time.time() + timeout
    acquired = False

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
                os.write(
                    fd,
                    f"{time.time()} {os.getpid()}".encode("ascii"),
                )
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            acquired = True
            break

        age = _lock_age_seconds(path)
        if age is not None and age > STATE_LOCK_STALE_SECONDS:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue

        if time.time() >= deadline:
            raise TimeoutError(f"Timed out waiting for state lock: {path}")

        time.sleep(STATE_LOCK_RETRY_SECONDS)

    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
