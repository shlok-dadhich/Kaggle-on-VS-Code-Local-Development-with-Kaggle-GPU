import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests

import argparse
from . import ignore_rules
from . import runner_paths
from . import session_guard
from . import urlstore
from . import __version__
from .kernel_exec import execute_in_kernel

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ============================================================
# Configuration
# ============================================================

REMOTE_ROOT = "local-project"

MAX_FILE_SIZE_MB = int(
    os.getenv("KAGGLE_SYNC_MAX_FILE_MB", "100")
)
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# Files larger than this are uploaded with the chunked Contents API
# protocol instead of one in-memory PUT.
CHUNK_THRESHOLD_BYTES = 8 * 1024 * 1024

# Raw read size per chunk (a multiple of 3, so base64 never needs
# mid-stream padding).
CHUNK_SIZE_BYTES = 3 * 1024 * 1024

# Backoff between retries of a single chunk/rename request.
CHUNK_RETRY_BACKOFFS = (1, 2, 4)

# Warn when the remote project grows past this (working disk ~20 GB).
REMOTE_SIZE_WARN_BYTES = 10 * 1024 * 1024 * 1024

# Single-PUT fallback ceiling when a server rejects chunked upload.
SINGLE_PUT_FALLBACK_LIMIT_BYTES = 100 * 1024 * 1024

_OVERSIZE_WATCHER_LOGGED = set()
_OVERSIZE_WATCHER_LOCK = threading.Lock()

# Increase this for slow connections or large repositories. Override with
# KAGGLE_SYNC_REQUEST_TIMEOUT without modifying this file.
REQUEST_TIMEOUT = int(
    os.getenv("KAGGLE_SYNC_REQUEST_TIMEOUT", "300")
)

SYNC_WORKERS = int(
    os.getenv("KAGGLE_SYNC_WORKERS", "4")
)

REMOTE_SYNC_INTERVAL = float(
    os.getenv("KAGGLE_REMOTE_SYNC_INTERVAL", "5")
)

REMOTE_POLL_MAX_INTERVAL = 30.0

REMOTE_POLL_IDLE_CYCLES = 5

# Download policy for files created/changed on Kaggle:
# "small" (default) auto-downloads only small text/image files,
# "off" downloads nothing, "all" downloads everything new.
DOWNLOAD_POLICY = os.getenv(
    "KAGGLE_SYNC_DOWNLOAD",
    "small",
).strip().lower() or "small"

if DOWNLOAD_POLICY not in ("small", "off", "all"):
    DOWNLOAD_POLICY = "small"

DOWNLOAD_MAX_MB = float(
    os.getenv("KAGGLE_SYNC_DOWNLOAD_MAX_MB", "5")
)
DOWNLOAD_MAX_BYTES = int(DOWNLOAD_MAX_MB * 1024 * 1024)

# Extensions eligible for auto-download under the "small" policy.
DOWNLOAD_EXTENSIONS = frozenset({
    ".py", ".txt", ".md", ".json", ".jsonl",
    ".yaml", ".yml", ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".svg", ".html",
})

# Extra directories never descended into during remote polling
# (in addition to ignore rules, EXCLUDED_DIRS and
# REMOTE_EXCLUDED_DIRS).
REMOTE_POLL_SKIP_DIRS = frozenset({
    ".ipynb_checkpoints",
    "__pycache__",
})

# Directories fetched concurrently per remote polling cycle.
REMOTE_POLL_CONCURRENCY = 4

# Set whenever a local upload succeeds so the polling loop resets
# its adaptive interval back to the base value.
_POLL_RESET = threading.Event()

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
    ".idea",
    ".vscode",
    "node_modules",
    "dataset_extracted",
}

EXCLUDED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".kaggle-sync-state.json",
}

REMOTE_EXCLUDED_DIRS = {
    "data",
    "dataset",
    "datasets",
    "kaggle_datasets",
    "dataset_extracted",
}

REMOTE_EXCLUDED_DIRS.update(
    name.strip()
    for name in os.getenv(
        "KAGGLE_REMOTE_EXCLUDE_DIRS",
        "",
    ).split(",")
    if name.strip()
)


# ============================================================
# Shared sync state (thread safety / echo suppression)
# ============================================================

class _CombinedStateLock:
    """In-process RLock + cross-process file lock combined.

    Every `with STATE_LOCK:` block first takes the in-process RLock
    and then the per-project file lock from runner_paths, so the
    sync watcher, the remote polling thread and an external
    kaggle-pull process can never corrupt the state file. The lock
    is re-entrant within one thread (the file lock is only taken at
    the outermost level). Until bind_state_lock() runs it behaves
    exactly like a plain RLock.
    """

    def __init__(self):
        self._rlock = threading.RLock()
        self._project_root = None
        self._local = threading.local()

    def bind(self, project_root):

        with self._rlock:
            self._project_root = Path(project_root).resolve()

    def acquire(self, blocking=True, timeout=-1):

        acquired = self._rlock.acquire(blocking, timeout)

        if not acquired:
            return False

        try:
            self._enter_file_lock()
        except Exception:
            self._rlock.release()
            raise

        return True

    def release(self):

        self._exit_file_lock()
        self._rlock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def _enter_file_lock(self):

        depth = getattr(self._local, "depth", 0)

        if depth == 0 and self._project_root is not None:
            context = runner_paths.state_lock(self._project_root)
            context.__enter__()
            self._local.file_context = context

        self._local.depth = depth + 1

    def _exit_file_lock(self):

        depth = getattr(self._local, "depth", 0)

        if depth <= 1:
            context = getattr(self._local, "file_context", None)
            self._local.file_context = None
            self._local.depth = 0

            if context is not None:
                context.__exit__(None, None, None)
        else:
            self._local.depth = depth - 1


STATE_LOCK = _CombinedStateLock()


def bind_state_lock(project_root):
    """Bind STATE_LOCK's file lock to a project (call once at startup)."""

    STATE_LOCK.bind(project_root)

DEBOUNCE_DELAY = 0.4

DOWNLOAD_ECHO_TTL = 5.0

_RECENTLY_DOWNLOADED = {}
_RECENTLY_DOWNLOADED_LOCK = threading.Lock()



def _registry_key(path):

    try:
        return str(Path(path).resolve())
    except Exception:
        try:
            return str(Path(path).absolute())
        except Exception:
            return str(path)


def mark_recently_downloaded(local_path):

    now = time.time()

    local_path = Path(local_path)

    try:
        temporary_path = local_path.with_name(
            f".{local_path.name}.kaggle-sync.tmp"
        )
    except Exception:
        temporary_path = None

    keys = {_registry_key(local_path)}

    try:
        keys.add(str(local_path.absolute()))
    except Exception:
        keys.add(str(local_path))

    if temporary_path is not None:
        keys.add(_registry_key(temporary_path))

        try:
            keys.add(str(temporary_path.absolute()))
        except Exception:
            keys.add(str(temporary_path))

    with _RECENTLY_DOWNLOADED_LOCK:
        for key in keys:
            _RECENTLY_DOWNLOADED[key] = now


def is_recently_downloaded(path):

    now = time.time()

    candidates = set()

    try:
        candidates.add(_registry_key(path))
    except Exception:
        pass

    try:
        candidates.add(str(Path(path).absolute()))
    except Exception:
        pass

    candidates.add(str(path))

    with _RECENTLY_DOWNLOADED_LOCK:

        expired = [
            key
            for key, timestamp in _RECENTLY_DOWNLOADED.items()
            if now - timestamp > DOWNLOAD_ECHO_TTL
        ]

        for key in expired:
            _RECENTLY_DOWNLOADED.pop(key, None)

        return any(
            key in _RECENTLY_DOWNLOADED
            for key in candidates
        )


def _status_from_exception(error):

    response = getattr(error, "response", None)

    if response is not None:

        status = getattr(response, "status_code", None)

        if isinstance(status, int):
            return status

    status = getattr(error, "status_code", None)

    if isinstance(status, int):
        return status

    return None



def note_auth_error(error):

    if not session_guard.is_auth_error(error):
        return False

    if not session_guard.SESSION_DEAD.is_set():
        session_guard.SESSION_DEAD.set()
        session_guard.log_session_expired()

    return True


# ============================================================
# Jupyter / Kaggle client
# ============================================================

class JupyterClient:

    def __init__(self, server_url):

        parsed = urlsplit(server_url)

        parts = [
            p for p in parsed.path.split("/")
            if p
        ]

        # Kaggle:
        #
        # /k/<session>/<token>/proxy

        if (
            len(parts) != 4
            or parts[0] != "k"
            or parts[3] != "proxy"
        ):
            raise RuntimeError(
                "\nInvalid Kaggle Jupyter Server URL.\n\n"
                "Expected:\n"
                "https://<host>/k/<session>/<token>/proxy\n"
            )

        self.token = parts[2]

        self.base_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

        self.websocket_base = (
            f"{'wss' if parsed.scheme == 'https' else 'ws'}"
            f"://{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

        self.session = requests.Session()

        self.session.headers.update({
            "Authorization": f"token {self.token}"
        })

        self._thread_local = threading.local()
        self._directory_cache = set()
        self._directory_cache_lock = threading.Lock()


    def request_session(self):

        session = getattr(
            self._thread_local,
            "session",
            None,
        )

        if session is None:

            session = requests.Session()

            session.headers.update({
                "Authorization": f"token {self.token}"
            })

            self._thread_local.session = session

        return session


    # --------------------------------------------------------
    # Contents API
    # --------------------------------------------------------

    def api(self, path=""):
        path = path.strip("/")
        if path:
            encoded = "/".join(
                quote(part, safe="")
                for part in path.split("/")
            )
            return f"{self.base_url}/api/{encoded}"
        return f"{self.base_url}/api"

    def upload_bytes(self, data: bytes, remote_path: str):
        dir_parts = remote_path.strip("/").split("/")[:-1]
        if dir_parts:
            self.ensure_directory("/".join(dir_parts))
        b64 = base64.b64encode(data).decode("ascii")
        session = self.request_session()
        resp = session.put(
            self.api_url(remote_path),
            json={"type": "file", "format": "base64", "content": b64},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

    def download_bytes(self, remote_path: str) -> bytes:
        session = self.request_session()
        resp = session.get(self.api_url(remote_path), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", "")
        if data.get("format") == "base64":
            return base64.b64decode(content)
        elif isinstance(content, str):
            return content.encode("utf-8")
        return bytes(content)

    def api_url(self, remote_path=""):

        remote_path = remote_path.strip("/")

        if remote_path:

            encoded = "/".join(
                quote(part, safe="")
                for part in remote_path.split("/")
            )

            return (
                f"{self.base_url}/api/contents/"
                f"{encoded}"
            )

        return (
            f"{self.base_url}/api/contents"
        )


    def ensure_directory(self, remote_path):

        parts = remote_path.strip("/").split("/")

        current = ""

        for part in parts:

            if not part:
                continue

            current = (
                f"{current}/{part}"
                if current
                else part
            )

            with self._directory_cache_lock:

                if current in self._directory_cache:
                    continue

            session = self.request_session()

            response = session.get(
                self.api_url(current),
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:

                with self._directory_cache_lock:
                    self._directory_cache.add(current)

                continue

            if response.status_code != 404:
                response.raise_for_status()

            response = session.put(
                self.api_url(current),
                json={
                    "type": "directory"
                },
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            with self._directory_cache_lock:
                self._directory_cache.add(current)


    def _send_with_retry(self, method, url, payload):
        """PUT/PATCH/DELETE with retries on timeout or 5xx.

        Up to 3 retries with backoff 1/2/4 s. Returns the last
        response (including 4xx) so callers can branch on it.
        """

        session = self.request_session()

        sender = getattr(session, method)

        last_error = None

        for attempt in range(1 + len(CHUNK_RETRY_BACKOFFS)):

            try:

                response = sender(
                    url,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )

            except requests.Timeout as e:

                last_error = e

                if attempt < len(CHUNK_RETRY_BACKOFFS):
                    time.sleep(CHUNK_RETRY_BACKOFFS[attempt])
                    continue

                raise

            status = getattr(response, "status_code", 0) or 0

            if status >= 500 and attempt < len(CHUNK_RETRY_BACKOFFS):
                time.sleep(CHUNK_RETRY_BACKOFFS[attempt])
                continue

            return response

        if last_error is not None:
            raise last_error

        raise RuntimeError(
            f"Request failed: {method.upper()} {url}"
        )


    def _upload_single_bytes(self, raw, remote_path):
        """Single-request PUT of already-read bytes."""

        payload = {
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        }

        response = self._send_with_retry(
            "put",
            self.api_url(remote_path),
            payload,
        )

        response.raise_for_status()


    def _upload_chunked(self, local_path, remote_path, size):
        """Chunked Contents API upload without loading the file.

        Streams 3 MiB raw chunks (a multiple of 3, so base64 needs
        no mid-stream padding) to "<remote>.kaggle-sync-part", then
        DELETEs any existing destination and PATCH-renames the part
        file, so the kernel never sees a half-written file.
        Returns True on success, False when skipped.
        """

        display = remote_path

        try:
            display = str(
                Path(local_path).relative_to(Path.cwd())
            )
        except Exception:
            display = str(local_path)

        part_remote = remote_path + ".kaggle-sync-part"

        # Start from a clean part file: a previous interrupted
        # upload must not be appended to.
        try:
            self.delete(part_remote)
        except Exception:
            pass

        def fallback_to_single(http_status):
            """One single-request PUT when the server rejects chunks."""

            if size > SINGLE_PUT_FALLBACK_LIMIT_BYTES:

                print(
                    f"[SKIP] {display} "
                    f"({size / 1024 / 1024:.1f} MB): "
                    "server does not support chunked upload "
                    f"(HTTP {http_status})"
                )

                return False

            print(
                f"[FALLBACK single PUT] {display} "
                "(server rejected chunked upload, "
                f"HTTP {http_status})"
            )

            try:
                raw = read_bytes_retry(local_path)
            except (PermissionError, OSError):
                skip_locked_message(local_path)
                return False

            self._upload_single_bytes(raw, remote_path)

            return True

        try:
            handle = open(local_path, "rb")
        except (PermissionError, OSError):
            skip_locked_message(local_path)
            return False

        sent = 0
        chunk_number = 0
        next_milestone = 25
        finished = False

        try:

            while True:

                try:
                    raw_chunk = handle.read(CHUNK_SIZE_BYTES)
                except (PermissionError, OSError):
                    skip_locked_message(local_path)
                    return False

                if not raw_chunk:
                    break

                chunk_number += 1
                sent += len(raw_chunk)

                is_last = sent >= size

                payload = {
                    "type": "file",
                    "format": "base64",
                    "content": base64.b64encode(
                        raw_chunk
                    ).decode("ascii"),
                    "chunk": -1 if is_last else chunk_number,
                }

                response = self._send_with_retry(
                    "put",
                    self.api_url(part_remote),
                    payload,
                )

                if response.status_code in (400, 405, 501):

                    try:
                        handle.close()
                    except Exception:
                        pass

                    return fallback_to_single(
                        response.status_code
                    )

                response.raise_for_status()

                percent = (sent * 100) // max(size, 1)

                while percent >= next_milestone and next_milestone < 100:

                    print(
                        f"[SYNC {next_milestone}%]",
                        display,
                    )

                    next_milestone += 25

                if is_last:
                    finished = True
                    break

            if not finished:

                # Empty tail (e.g. exact multiple): close the stream.
                response = self._send_with_retry(
                    "put",
                    self.api_url(part_remote),
                    {
                        "type": "file",
                        "format": "base64",
                        "content": "",
                        "chunk": -1,
                    },
                )

                if response.status_code in (400, 405, 501):
                    return fallback_to_single(
                        response.status_code
                    )

                response.raise_for_status()

        finally:

            try:
                handle.close()
            except Exception:
                pass

        # Swap the complete part file into place.
        self.delete(remote_path)

        response = self._send_with_retry(
            "patch",
            self.api_url(part_remote),
            {"path": remote_path},
        )

        response.raise_for_status()

        return True


    def upload_file(self, local_path, remote_path):
        """Upload a file. Returns True when uploaded, False when the
        file was missing, too large, or still locked after retries
        (a single [SKIP locked] line is printed in the locked case).

        Files over CHUNK_THRESHOLD_BYTES stream through the chunked
        Contents API protocol without loading the file into RAM.
        """

        local_path = Path(local_path)

        if not local_path.exists():
            return False

        try:
            size = local_path.stat().st_size
        except (PermissionError, OSError):
            skip_locked_message(local_path)
            return False

        if size > MAX_FILE_SIZE:

            print(
                f"[SKIP] {local_path} "
                f"({size / 1024 / 1024:.1f} MB > "
                f"{MAX_FILE_SIZE / 1024 / 1024:.0f} MB)"
            )

            return False

        parent = os.path.dirname(remote_path)

        if parent:
            self.ensure_directory(parent)

        if size > CHUNK_THRESHOLD_BYTES:
            return self._upload_chunked(local_path, remote_path, size)

        try:
            raw = read_bytes_retry(local_path)
        except (PermissionError, OSError):
            skip_locked_message(local_path)
            return False

        self._upload_single_bytes(raw, remote_path)

        return True


    def list_files(self, remote_path=""):

        response = self.request_session().get(
            self.api_url(remote_path),
            params={"content": 1},
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            return []

        if data.get("type") == "file":
            return [data]

        files = []

        for entry in data.get("content") or []:

            if not isinstance(entry, dict):
                continue

            entry_path = entry.get("path")

            if not entry_path:
                continue

            if entry.get("type") == "directory":
                files.extend(self.list_files(entry_path))
            elif entry.get("type") == "file":
                files.append(entry)

        return files


    def download_file(self, remote_path, local_path):

        response = self.request_session().get(
            self.api_url(remote_path),
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        data = response.json()
        content = data.get("content", "")

        if data.get("format") == "base64":
            raw_content = base64.b64decode(content)
        else:
            raw_content = content.encode("utf-8")

        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = local_path.with_name(
            f".{local_path.name}.kaggle-sync.tmp"
        )

        try:
            temporary_path.write_bytes(raw_content)
            os.replace(temporary_path, local_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


    def delete(self, remote_path):

        response = self.request_session().delete(
            self.api_url(remote_path),
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code not in (
            200,
            204,
            404,
        ):
            response.raise_for_status()


    # --------------------------------------------------------
    # Sessions
    # --------------------------------------------------------

    def get_sessions(self):

        response = self.session.get(
            f"{self.base_url}/api/sessions",
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        return response.json()


# ============================================================
# Helpers
# ============================================================

def should_skip(path):

    path = Path(path)

    name = path.name

    if name in EXCLUDED_FILES:
        return True

    if name.endswith(".kaggle-sync.tmp"):
        return True

    if name.endswith(".tmp"):
        return True

    for part in path.parts:

        if part in EXCLUDED_DIRS:
            return True

    return False


def should_skip_remote(path):

    path = Path(path)

    if any(
        part.lower() in REMOTE_EXCLUDED_DIRS
        for part in path.parts
    ):
        return True

    return False


# ============================================================
# OneDrive / cloud placeholders / locked-file reads
# ============================================================

# Files at or below this size also get a content hash in the
# manifest, so attribute-only touches do not cause re-uploads.
HASH_LIMIT_BYTES = 20 * 1024 * 1024

# OneDrive Files-On-Demand placeholder bits (Windows
# st_file_attributes; getattr default 0 -> no-op elsewhere).
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

# Read-retry backoff for locked files (OneDrive may hold locks).
READ_RETRY_BACKOFFS = (0.2, 0.4, 0.8, 1.6, 3.2)


def onedrive_root_for(project_root):

    try:
        resolved = str(Path(project_root).resolve())
    except Exception:
        resolved = str(project_root)

    lowered = resolved.lower()

    for variable in (
        "OneDrive",
        "OneDriveConsumer",
        "OneDriveCommercial",
    ):

        candidate = os.getenv(variable)

        if not candidate:
            continue

        base = candidate.lower().rstrip("\\/")

        if lowered == base or lowered.startswith(base + "\\"):
            return candidate

    try:
        parts = [
            part.lower()
            for part in Path(resolved).parts
        ]
    except Exception:
        parts = []

    if "onedrive" in parts:
        return "OneDrive"

    return None


def warn_if_onedrive(project_root):

    if os.getenv("KAGGLE_SYNC_ALLOW_ONEDRIVE", "") == "1":
        return

    root = onedrive_root_for(project_root)

    if not root:
        return

    print()
    print(
        "WARNING: this project appears to be inside OneDrive "
        f"({root})."
    )
    print(
        "OneDrive placeholders, locks and attribute-only touches "
        "can slow down or confuse synchronization."
    )
    print(
        "Recommended: keep the project in a non-synced folder, "
        "e.g. C:\\dev\\<project>."
    )
    print(
        "Set KAGGLE_SYNC_ALLOW_ONEDRIVE=1 to silence this warning."
    )
    print()


def is_cloud_placeholder(path):

    try:
        attributes = getattr(
            os.stat(path),
            "st_file_attributes",
            0,
        ) or 0
    except OSError:
        return False
    except Exception:
        return False

    return bool(
        attributes
        & (
            FILE_ATTRIBUTE_OFFLINE
            | FILE_ATTRIBUTE_RECALL_ON_OPEN
            | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        )
    )


def read_bytes_retry(path, attempts=6):
    """Read a file, retrying OneDrive-style transient locks.

    One initial try plus up to 5 retries with backoff
    0.2, 0.4, 0.8, 1.6, 3.2 s. Raises the last error.
    """

    last_error = None

    for attempt in range(max(1, attempts)):

        try:

            with open(path, "rb") as f:
                return f.read()

        except (PermissionError, OSError) as e:

            last_error = e

            if attempt < len(READ_RETRY_BACKOFFS):
                time.sleep(READ_RETRY_BACKOFFS[attempt])

    if last_error is not None:
        raise last_error

    raise OSError(f"Could not read file: {path}")


def skip_locked_message(path):

    try:
        display = str(
            Path(path).relative_to(Path.cwd())
        )
    except Exception:
        display = str(path)

    print(
        "[SKIP locked]",
        display,
    )


def remote_path_for(
    local_path,
    project_root,
):

    relative = Path(local_path).resolve().relative_to(
        Path(project_root).resolve()
    )

    return "/".join(
        [REMOTE_ROOT] + list(relative.parts)
    )


def sha256_file(path):

    h = hashlib.sha256()

    last_error = None

    for attempt in range(max(1, 6)):

        try:

            h = hashlib.sha256()

            with open(path, "rb") as f:

                while True:

                    chunk = f.read(1024 * 1024)

                    if not chunk:
                        break

                    h.update(chunk)

            return h.hexdigest()

        except (PermissionError, OSError) as e:

            last_error = e

            if attempt < len(READ_RETRY_BACKOFFS):
                time.sleep(READ_RETRY_BACKOFFS[attempt])

    if last_error is not None:
        raise last_error

    raise OSError(f"Could not read file: {path}")


def file_signature(path):

    stat = Path(path).stat()

    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def file_signature_full(path):
    """Manifest entry: size + mtime_ns, plus sha256 for small files.

    The hash lets the watcher tell attribute-only touches apart
    from real content changes without re-uploading.
    """

    signature = file_signature(path)

    entry = {}
    entry.update(signature)

    try:
        size = entry.get("size", 0)
    except Exception:
        size = 0

    if isinstance(size, int) and size <= HASH_LIMIT_BYTES:

        try:
            entry["sha256"] = sha256_file(path)
        except (PermissionError, OSError):
            pass
        except Exception:
            pass

    return entry


def signatures_match(stored, current):
    """True when size + mtime_ns agree (ignores sha256 key)."""

    try:

        if not isinstance(stored, dict):
            return False

        if not isinstance(current, dict):
            return False

        return (
            stored.get("size") == current.get("size")
            and stored.get("mtime_ns") == current.get("mtime_ns")
        )

    except Exception:
        return False


def state_file_for_project(project_root):
    """Sync-state JSON for this project (under RUNNER_HOME)."""

    return runner_paths.state_file(project_root)


def migrate_legacy_state(project_root):
    """One-time move of the old in-project state file.

    Merges into the new RUNNER_HOME file without overwriting keys
    that already exist there and removes the legacy file only after
    the replacement has been written and verified.
    """

    with STATE_LOCK:
        legacy = (
            Path(project_root)
            / ".kaggle-sync-state.json"
        )

        if not legacy.exists():
            return

        try:
            with open(legacy, "r", encoding="utf-8") as f:
                legacy_state = json.load(f)
            if not isinstance(legacy_state, dict):
                raise ValueError("legacy state is not a JSON object")
        except (OSError, ValueError) as exc:
            print(
                "WARNING: Could not read legacy sync state; kept "
                f"{legacy}: {exc}"
            )
            return
        new_path = state_file_for_project(project_root)
        manager = StateManager(project_root)
        current = manager._load_all()

        for key, value in legacy_state.items():
            if key not in current:
                current[key] = value
            elif (
                isinstance(current[key], dict)
                and isinstance(value, dict)
            ):
                merged = dict(value)
                merged.update(current[key])
                current[key] = merged

        try:
            runner_paths.write_text_atomic(
                new_path,
                json.dumps(current, indent=2),
            )
            with open(new_path, "r", encoding="utf-8") as f:
                verified = json.load(f)
            if verified != current:
                raise ValueError("written state did not match the migration")
        except (OSError, ValueError) as exc:
            print(
                "WARNING: Could not verify legacy sync-state migration; "
                f"kept {legacy}: {exc}"
            )
            return
        try:
            legacy.unlink()
        except OSError as exc:
            print(
                "WARNING: Migrated state is verified, but could not remove "
                f"legacy file {legacy}: {exc}"
            )


class StateManager:
    """Unified state manager for project sync and remote tracking."""

    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()
        self.state_file = state_file_for_project(self.project_root)

    def _load_all(self) -> dict:
        with STATE_LOCK:
            if not self.state_file.exists():
                return {}
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def _write_all(self, state: dict):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        runner_paths.write_text_atomic(
            self.state_file,
            json.dumps(state, indent=2),
        )

    def _save_section(self, section: str, data: dict):
        with STATE_LOCK:
            state = self._load_all()
            state[section] = dict(data)
            self._write_all(state)

    def load_section(self, section: str) -> dict:
        value = self._load_all().get(section, {})
        return dict(value) if isinstance(value, dict) else {}

    def get_value(self, section: str, key: str, default=None):
        return self.load_section(section).get(key, default)

    def set_value(self, section: str, key: str, value):
        with STATE_LOCK:
            state = self._load_all()
            values = state.get(section, {})
            if not isinstance(values, dict):
                values = {}
            values = dict(values)
            values[key] = value
            state[section] = values
            self._write_all(state)

    def delete_value(self, section: str, key: str):
        with STATE_LOCK:
            state = self._load_all()
            values = state.get(section, {})
            if not isinstance(values, dict) or key not in values:
                return
            values = dict(values)
            del values[key]
            state[section] = values
            self._write_all(state)

    def load_files(self) -> dict:
        return self.load_section("files")

    def save_files(self, manifest: dict):
        with STATE_LOCK:
            current = self.load_section("files")
            current.update(manifest)
            self._save_section("files", current)

    def delete_file(self, relative_path: str):
        self.delete_value("files", relative_path)

    def load_remote(self) -> dict:
        return dict(self._load_all().get("remote", {}) or {})

    def save_remote(self, manifest: dict):
        self._save_section("remote", manifest)

    def load_pending_remote(self) -> dict:
        pending = self._load_all().get("pending_remote", {}) or {}
        return {
            key: value
            for key, value in dict(pending).items()
            if isinstance(value, dict)
        }

    def save_pending_remote(self, manifest: dict):
        self._save_section("pending_remote", manifest)

    def load_runner_kernel(self):
        value = self._load_all().get("runner_kernel")
        if isinstance(value, dict):
            return value.get("id")
        return None

    def save_runner_kernel(self, kernel_id):
        self._save_section(
            "runner_kernel",
            {"id": kernel_id} if kernel_id else {},
        )


def load_sync_manifest(project_root):
    return StateManager(project_root).load_files()


def save_sync_manifest(project_root, manifest):
    StateManager(project_root).save_files(manifest)


def load_remote_manifest(project_root):
    return StateManager(project_root).load_remote()


def save_remote_manifest(project_root, manifest):
    StateManager(project_root).save_remote(manifest)


def load_pending_remote(project_root):
    return StateManager(project_root).load_pending_remote()


def save_pending_remote(project_root, manifest):
    StateManager(project_root).save_pending_remote(manifest)


def is_ignored_path(project_root, path):
    """True when a local path is excluded by .kagglesyncignore."""

    try:
        if runner_paths.is_runner_home_path(project_root, path):
            return True
        relative = Path(path).resolve().relative_to(
            Path(project_root).resolve()
        ).as_posix()
    except Exception:
        return False

    return ignore_rules.is_ignored(
        project_root,
        relative,
        is_dir=False,
    )


def iter_local_files(project_root):
    """Yield file Paths under the project without slow descents.

    Uses os.walk(topdown=True) and prunes EXCLUDED_DIRS plus ignored
    directories in place, so huge trees (.venv, node_modules, .git,
    data/) are never even listed. When the ignore file uses "!"
    re-includes, ignored directories are NOT pruned (only files are
    skipped) so re-included paths are still found.
    """

    project_root = Path(project_root).resolve()

    try:
        prune_ignored_dirs = not ignore_rules.has_negation(
            project_root
        )
    except Exception:
        prune_ignored_dirs = True

    for dirpath, dirnames, filenames in os.walk(
        project_root,
        topdown=True,
    ):

        dirnames[:] = [
            name
            for name in dirnames
            if name not in EXCLUDED_DIRS
            and not runner_paths.is_runner_home_path(
                project_root,
                Path(dirpath) / name,
            )
        ]

        if prune_ignored_dirs:

            kept = []

            for name in dirnames:

                try:
                    relative = Path(
                        dirpath,
                        name,
                    ).relative_to(
                        project_root
                    ).as_posix()
                except Exception:
                    kept.append(name)
                    continue

                try:
                    ignored = ignore_rules.is_ignored(
                        project_root,
                        relative,
                        is_dir=True,
                    )
                except Exception:
                    ignored = False

                if not ignored:
                    kept.append(name)

            dirnames[:] = kept

        for name in filenames:
            yield Path(dirpath) / name


def find_requirements(project_root):

    project_root = Path(project_root).resolve()

    result = []

    seen = set()

    for path in iter_local_files(project_root):

        if not path.name.startswith("requirements"):
            continue

        if path.suffix != ".txt":
            continue

        try:
            resolved = path.resolve()
        except Exception:
            continue

        if resolved in seen:
            continue

        if should_skip(resolved):
            continue

        seen.add(resolved)

        result.append(resolved)

    return result


# ============================================================
# Requirements parsing / selection (local, no new dependencies)
# ============================================================

# Packages that Kaggle preinstalls with CUDA builds. Installing them
# from a plain requirements.txt can replace those builds and break
# the GPU environment, so they are filtered out of the install list.
PROTECTED_PACKAGES_DEFAULT = (
    "torch",
    "torchvision",
    "torchaudio",
    "torchtext",
    "numpy",
    "pandas",
    "scipy",
    "scikit-learn",
    "tensorflow",
    "keras",
    "jax",
    "jaxlib",
    "triton",
    "pillow",
)

# Debounce for coalescing repeated dependency-check requests.
DEPENDENCY_DEBOUNCE_SECONDS = 2.0

# How long a remote pip install may take.
DEPENDENCY_INSTALL_TIMEOUT = 1800


def normalize_package_name(name):
    """PEP 503 normalization for comparing package names."""

    return re.sub(r"[-_.]+", "-", name).lower()


def protected_package_names():
    """Protected set + KAGGLE_SYNC_PROTECTED - KAGGLE_SYNC_UNPROTECT."""

    protected = {
        normalize_package_name(name)
        for name in PROTECTED_PACKAGES_DEFAULT
    }

    extra = os.getenv("KAGGLE_SYNC_PROTECTED", "")

    for name in extra.split(","):

        name = name.strip()

        if name:
            protected.add(normalize_package_name(name))

    remove = os.getenv("KAGGLE_SYNC_UNPROTECT", "")

    for name in remove.split(","):

        name = name.strip()

        if name:
            protected.discard(normalize_package_name(name))

    return protected


def is_protected_package(normalized_name, protected=None):
    """True for protected names and any nvidia-* build."""

    if protected is None:
        protected = protected_package_names()

    if normalized_name in protected:
        return True

    return normalized_name.startswith("nvidia-")


_REQUIREMENT_NAME_RE = re.compile(
    r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[^\]]*\])?\s*(.*)$"
)


def parse_requirements_text(text, protected=None):
    """Split a requirements file into installable lines and notices.

    Returns (kept, dropped_protected, dropped_unsupported, options)
    where kept holds the requirement lines pip should install (with
    extras/markers intact), options holds pip option lines such as
    --extra-index-url, and the dropped lists hold display names for
    the printed notices.
    """

    if protected is None:
        protected = protected_package_names()

    kept = []
    dropped_protected = []
    dropped_unsupported = []
    options = []

    for raw_line in text.splitlines():

        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "#" in line:
            line = line.split("#", 1)[0].strip()

        if not line:
            continue

        if line.startswith("--"):
            options.append(line)
            continue

        lowered = line.lower()

        if (
            lowered.startswith("-r")
            or lowered.startswith("-e")
            or lowered.startswith("--requirement")
            or lowered.startswith("--editable")
        ):
            dropped_unsupported.append(line)
            continue

        if line.startswith("-"):
            dropped_unsupported.append(line)
            continue

        if "://" in line:
            dropped_unsupported.append(line)
            continue

        if (
            line.startswith(".")
            or line.startswith("/")
            or re.match(r"^[A-Za-z]:[\\/]", line)
        ):
            dropped_unsupported.append(line)
            continue

        if re.search(r"\s@\s", line):
            # PEP 508 direct reference (name @ url).
            dropped_unsupported.append(line)
            continue

        match = _REQUIREMENT_NAME_RE.match(line)

        if not match:
            dropped_unsupported.append(line)
            continue

        normalized = normalize_package_name(match.group(1))

        if not normalized:
            dropped_unsupported.append(line)
            continue

        if is_protected_package(normalized, protected):
            dropped_protected.append(match.group(1))
            continue

        kept.append(line)

    return kept, dropped_protected, dropped_unsupported, options


def select_requirements_file(project_root):
    """Pick the single requirements file to install from.

    Returns (path_or_None, notice_lines). Honors KAGGLE_SYNC_REQUIREMENTS,
    prefers <root>/requirements.txt, falls back to a lone
    requirements*.txt elsewhere, and installs nothing when several
    exist without a root file.
    """

    root = Path(project_root).resolve()

    forced = os.getenv("KAGGLE_SYNC_REQUIREMENTS", "").strip()

    if forced:

        candidate = (
            root / forced.replace("/", os.sep)
        ).resolve()

        if candidate.is_file():
            return candidate, []

        return None, [
            "KAGGLE_SYNC_REQUIREMENTS points to "
            f"{forced}, which was not found.",
        ]

    root_file = root / "requirements.txt"

    if root_file.is_file():
        return root_file, []

    candidates = []

    for path in find_requirements(project_root):

        try:
            relative = path.relative_to(root)
        except Exception:
            continue

        if is_ignored_path(root, path):
            continue

        candidates.append((str(relative), path))

    candidates.sort(key=lambda item: item[0])

    if len(candidates) == 1:

        return candidates[0][1], [
            f"Using {candidates[0][0]} "
            "(no requirements.txt at the project root).",
        ]

    if not candidates:
        return None, []

    names = ", ".join(name for name, _ in candidates)

    return None, [
        f"Found several requirements files: {names}.",
        "Install none of them; set KAGGLE_SYNC_REQUIREMENTS "
        "to a relative path to pick one.",
    ]


def find_requirement_candidates(project_root):
    """All requirements*.txt files (used by the selection notice)."""

    return [
        str(path.relative_to(Path(project_root).resolve()))
        for path in find_requirements(project_root)
    ]


def build_install_code(requirements, options=None):
    """Remote installer cell sent through execute_in_kernel.

    Probes internet access first (NO_INTERNET when the Kaggle
    Internet toggle is off), skips already-satisfied requirements,
    installs the rest with pip constrained to Kaggle's preinstalled
    protected versions, and reports restart-affected modules.
    """

    template = """
import json as _json
import os as _os
import subprocess as _sp
import sys as _sys
import tempfile as _tf

_REQUIREMENTS = __REQUIREMENTS_JSON__
_OPTIONS = __OPTIONS_JSON__
_PROTECTED = __PROTECTED_JSON__


def _emit(kind, payload):
    print(
        "[KAGGLE_DEPS:" + kind + "] " + _json.dumps(payload),
        flush=True,
    )


_internet = False

try:
    import urllib.request as _url

    try:
        _probe = _url.Request(
            "https://pypi.org/simple/pip/",
            headers={"User-Agent": "kaggle-sync"},
        )
        _url.urlopen(_probe, timeout=8).close()
        _internet = True
    except Exception:
        _internet = False
except Exception:
    _internet = False

if not _internet:
    print(
        "Kaggle Internet is OFF (or blocked). Enable it in the "
        "notebook Settings -> Internet (account needs phone "
        "verification); this may restart the session, so paste "
        "the new URL into kaggle-sync again.",
        flush=True,
    )
    _emit("RESULT", {"status": "NO_INTERNET"})
else:
    try:
        from importlib import metadata as _md
    except ImportError:
        _md = None

    try:
        from packaging.requirements import Requirement as _Req
    except ImportError:
        _Req = None

    _satisfied = []
    _missing = []

    for _rs in _REQUIREMENTS:
        if _Req is None or _md is None:
            _missing.append(_rs)
            continue
        try:
            _rq = _Req(_rs)
        except Exception:
            _missing.append(_rs)
            continue
        try:
            _ver = _md.version(_rq.name)
        except Exception:
            _missing.append(_rs)
            continue
        try:
            _ok = (not _rq.specifier) or (_ver in _rq.specifier)
        except Exception:
            _ok = False
        if _ok:
            _satisfied.append("%s==%s" % (_rq.name, _ver))
        else:
            _missing.append(_rs)

    _emit("SATISFIED", _satisfied)

    if not _missing:
        _emit("RESULT", {"status": "OK", "installed": []})
    else:
        _constraints = []

        for _name in _PROTECTED:
            try:
                _v = _md.version(_name) if _md else None
            except Exception:
                _v = None
            if _v:
                _constraints.append("%s==%s" % (_name, _v))

        _cfd, _cfp = _tf.mkstemp(
            prefix="kaggle-sync-constraints-",
            suffix=".txt",
        )
        _os.write(
            _cfd,
            ("\\n".join(_constraints) + "\\n").encode("utf-8"),
        )
        _os.close(_cfd)

        _cmd = [
            _sys.executable, "-m", "pip", "install",
            "--no-input", "--disable-pip-version-check",
            "--default-timeout", "60", "--retries", "3",
            "-c", _cfp,
        ] + list(_OPTIONS) + list(_missing)

        print("Running: " + " ".join(_cmd), flush=True)

        _proc = _sp.Popen(
            _cmd,
            stdout=_sp.PIPE,
            stderr=_sp.STDOUT,
            text=True,
        )
        _out_lines = []

        for _line in _proc.stdout:
            _out_lines.append(_line)
            print(_line, end="", flush=True)

        _rc = _proc.wait()

        try:
            _os.unlink(_cfp)
        except Exception:
            pass

        if _rc != 0:
            _tail = "".join(_out_lines[-30:])
            _flat = _tail.lower().replace("_", "-")
            _hit = []

            for _pn in _PROTECTED:
                if _pn.lower().replace("_", "-") in _flat:
                    try:
                        _pv = _md.version(_pn) if _md else "?"
                    except Exception:
                        _pv = "?"
                    _hit.append("%s==%s" % (_pn, _pv))

            for _h in _hit:
                print(
                    "Requirement conflicts with Kaggle's "
                    "preinstalled %s; relax the pin in "
                    "requirements.txt." % _h,
                    flush=True,
                )

            _emit("RESULT", {
                "status": "PIP_FAILED",
                "conflicts": _hit,
                "tail": _tail[-2000:],
            })
        else:
            try:
                _mapping = (
                    _md.packages_distributions()
                    if _md and hasattr(_md, "packages_distributions")
                    else {}
                )
            except Exception:
                _mapping = {}

            _wanted = set()

            for _rs in _missing:
                try:
                    _n = _Req(_rs).name if _Req else _rs
                except Exception:
                    _n = _rs
                _wanted.add(str(_n).lower().replace("_", "-"))

            _restart = sorted({
                _mod
                for _mod, _dists in _mapping.items()
                if _mod in _sys.modules and any(
                    str(_d).lower().replace("_", "-") in _wanted
                    for _d in _dists
                )
            })

            if _restart:
                print(
                    "Restart the Kaggle kernel for these to take "
                    "effect: " + ", ".join(_restart),
                    flush=True,
                )

            _emit("RESULT", {
                "status": "OK",
                "installed": list(_missing),
                "restart": _restart,
            })
"""

    code = template.replace(
        "__REQUIREMENTS_JSON__",
        json.dumps(list(requirements)),
    )

    code = code.replace(
        "__OPTIONS_JSON__",
        json.dumps(list(options or [])),
    )

    code = code.replace(
        "__PROTECTED_JSON__",
        json.dumps(sorted(protected_package_names())),
    )

    return code


def parse_remote_result(outputs):
    """Parse installer markers from streamed remote output."""

    satisfied = []
    result = {}

    for line in str(outputs).splitlines():

        line = line.strip()

        if line.startswith("[KAGGLE_DEPS:SATISFIED]"):

            try:
                parsed = json.loads(
                    line[len("[KAGGLE_DEPS:SATISFIED]"):]
                )

                if isinstance(parsed, list):
                    satisfied = [
                        str(item) for item in parsed
                    ]

            except Exception:
                pass

        elif line.startswith("[KAGGLE_DEPS:RESULT]"):

            try:
                parsed = json.loads(
                    line[len("[KAGGLE_DEPS:RESULT]"):]
                )

                if isinstance(parsed, dict):
                    result = dict(parsed)

            except Exception:
                pass

    result.setdefault("status", "failed")
    result["satisfied"] = satisfied

    if not isinstance(result.get("installed"), list):
        result["installed"] = []

    return result


# ============================================================
# Dependency manager
# ============================================================

class DependencyManager:

    def __init__(
        self,
        client,
        project_root,
    ):

        self.client = client

        self.project_root = Path(
            project_root
        ).resolve()

        self.state_manager = StateManager(self.project_root)

        self._dep_queue = queue.Queue()
        self._dep_thread = None
        self._dep_stop = threading.Event()
        self._dep_lock = threading.Lock()
        self._last_selection_notice = None
        self._deps_off_reported = False

    def _record_digest(self, key, digest):
        self.state_manager.set_value("requirements", key, digest)


    def _record_failed_digest(self, key, digest):
        self.state_manager.set_value("failed_digest", key, digest)


    def _clear_failed_digest(self, key):
        self.state_manager.delete_value("failed_digest", key)


    def install_requirements(
        self,
        requirements_file,
    ):

        self.request_check()


    def check(self):

        self.start()
        self.request_check(quiet=False)


    def start(self):

        with self._dep_lock:

            if (
                self._dep_thread is not None
                and self._dep_thread.is_alive()
            ):
                return

            self._dep_stop.clear()

            self._dep_thread = threading.Thread(
                target=self._dep_worker,
                name="kaggle-dependency-installer",
                daemon=True,
            )
            self._dep_thread.start()


    def stop(self):

        self._dep_stop.set()

        thread = None

        with self._dep_lock:
            thread = self._dep_thread

        if thread is not None and thread.is_alive():
            thread.join(timeout=5)


    def request_check(self, quiet=True):

        try:
            self._dep_queue.put_nowait(bool(quiet))
        except Exception:
            pass


    def flush(self, timeout=120):
        """Block until the queue is empty and no install is running.

        Test helper; not used by the sync loops.
        """

        deadline = time.time() + timeout

        while time.time() < deadline:

            if self._dep_queue.empty():
                return True

            time.sleep(0.05)

        return self._dep_queue.empty()


    def _dep_worker(self):

        while (
            not self._dep_stop.is_set()
            and not session_guard.SESSION_DEAD.is_set()
        ):

            try:
                quiet = bool(self._dep_queue.get(timeout=0.5))
            except queue.Empty:
                continue
            except Exception:
                continue

            try:

                deadline = (
                    time.time()
                    + DEPENDENCY_DEBOUNCE_SECONDS
                )

                while True:

                    remaining = deadline - time.time()

                    if remaining <= 0:
                        break

                    try:
                        extra = self._dep_queue.get(
                            timeout=remaining
                        )
                        quiet = quiet and bool(extra)
                    except queue.Empty:
                        break
                    except Exception:
                        break

                if (
                    self._dep_stop.is_set()
                    or session_guard.SESSION_DEAD.is_set()
                ):
                    break

                self._run_check(quiet=quiet)

            except Exception as e:

                print(
                    "[DEPENDENCIES ERROR]",
                    e,
                )

            finally:

                try:
                    self._dep_queue.task_done()
                except Exception:
                    pass


    def _run_check(self, quiet=False):
        policy = os.getenv("KAGGLE_DEPS", "auto").strip().lower() or "auto"
        if policy == "off":
            if not self._deps_off_reported:
                self._deps_off_reported = True
                print("Dependencies: off (remote installs disabled)")
            return "skipped"
        if policy != "auto":
            print(f"Unsupported KAGGLE_DEPS={policy!r}; using auto")

        selected, notices = select_requirements_file(
            self.project_root
        )

        notice_key = (
            str(selected) if selected else None,
            tuple(notices),
        )

        if notices and notice_key != self._last_selection_notice:

            self._last_selection_notice = notice_key

            print()

            for line in notices:
                print(line)

            print()

        if selected is None:
            return "skipped"

        try:
            relative = selected.relative_to(self.project_root)
        except Exception:
            relative = Path(selected.name)

        key = str(relative).replace("\\", "/")

        try:

            with open(selected, "r", encoding="utf-8") as f:
                text = f.read()

        except (PermissionError, OSError) as e:

            if not quiet:
                print()
                print(
                    "Dependencies: FAILED "
                    f"(cannot read {key}: "
                    f"{session_guard.format_exception(e, self.client.token)})"
                )
                print()

            return "failed"

        kept, dropped_protected, dropped_unsupported, options = (
            parse_requirements_text(text)
        )

        filtered_lines = list(options) + list(kept)

        digest = hashlib.sha256(
            "\n".join(filtered_lines).encode("utf-8")
        ).hexdigest()

        old_digest = self.state_manager.get_value("requirements", key)

        if old_digest == digest:

            if not quiet:
                print(
                    "Dependencies: OK "
                    f"({key} up to date)"
                )

            return "up-to-date"

        if self.state_manager.get_value("failed_digest", key) == digest:

            if not quiet:
                print(
                    "Dependencies: skipped "
                    f"({key} unchanged since the last failure)"
                )

            return "failed-known"

        if dropped_protected:

            print(
                "Using Kaggle's preinstalled: "
                + ", ".join(sorted(set(dropped_protected)))
            )

        for line in dropped_unsupported:

            print(
                f"Ignoring unsupported requirements line: {line}"
            )

        if not kept:

            self._record_digest(key, digest)
            self._clear_failed_digest(key)

            if not quiet:
                print(
                    "Dependencies: OK "
                    "(nothing to install)"
                )

            return "ok"

        print()
        print("=" * 60)
        print("Requirements changed")
        print("=" * 60)
        print()
        print("File:", key)
        print("Installing dependencies on Kaggle...")
        print()

        code = build_install_code(kept, options)

        kernel_id = None
        temp_kernel = False

        try:

            kernel_id, temp_kernel = self._ensure_kernel()

            outputs = self._run_remote(code, kernel_id)

            result = parse_remote_result(outputs)

        except Exception as e:

            if note_auth_error(e):
                self._record_failed_digest(key, digest)
                return "failed"

            print()
            print("Dependencies: FAILED")
            print(session_guard.format_exception(e, self.client.token))
            print()

            self._record_failed_digest(key, digest)

            return "failed"

        finally:

            if temp_kernel and kernel_id:
                self._release_kernel(kernel_id)

        status = result.get("status", "failed")

        if status == "NO_INTERNET":

            self._record_failed_digest(key, digest)

            print()
            print(
                "Dependencies: FAILED "
                "(no internet on Kaggle)"
            )
            print()

            return "failed"

        if status != "OK":

            self._record_failed_digest(key, digest)

            detail = result.get("detail", "")

            print()
            print("Dependencies: FAILED")

            if detail:
                print(detail)

            print()

            return "failed"

        installed = result.get("installed", [])
        satisfied = result.get("satisfied", [])

        self._record_digest(key, digest)
        self._clear_failed_digest(key)

        print()
        print(
            f"Dependencies: OK "
            f"({len(installed)} installed, "
            f"{len(satisfied)} already satisfied)"
        )
        print()

        return "ok"


    def _ensure_kernel(self):
        """Create a private temporary kernel for dependency installation."""

        def _create():
            session = self.client.request_session()

            response = session.get(
                f"{self.client.base_url}/api/kernelspecs",
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            specs = response.json() or {}

            name = specs.get("default") or "python3"

            response = session.post(
                f"{self.client.base_url}/api/kernels",
                json={"name": name},
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            created = response.json() or {}

            kernel_id = created.get("id")

            if not kernel_id:
                raise RuntimeError(
                    "Could not create a temporary Kaggle kernel."
                )

            print(
                "Created an ephemeral kernel for dependency installation."
            )

            return kernel_id, True

        kernel_id, temp_kernel = session_guard.guarded_call(self.client, _create)
        return kernel_id, temp_kernel


    def _release_kernel(self, kernel_id):

        try:

            self.client.request_session().delete(
                f"{self.client.base_url}/api/kernels/{kernel_id}",
                timeout=REQUEST_TIMEOUT,
            )

        except Exception:
            pass


    def _run_remote(self, code, kernel_id):
        """Run code in a kernel, streaming output; return full text."""

        # execute_in_kernel imported at module level

        outputs = []

        def on_text(text):

            outputs.append(text)

            print(
                "[KAGGLE]",
                text,
                end="",
            )

        result = execute_in_kernel(
            self.client.websocket_base,
            self.client.base_url,
            self.client.token,
            kernel_id,
            code,
            on_text=on_text,
            timeout=DEPENDENCY_INSTALL_TIMEOUT,
        )

        if result["status"] != "ok":

            raise RuntimeError(
                result["error_text"]
                or f"Remote execution failed: {result['status']}"
            )

        return "".join(outputs)


# ============================================================
# Project synchronization
# ============================================================


def sync_once(client, project_root, verbose=False) -> int:
    """One-shot project synchronization.

    Reuses initial_sync rules: manifest skip, ignore rules, size caps,
    placeholder skipping, and cross-process state lock.
    Returns the number of changed files uploaded.
    """
    project_root = Path(project_root).resolve()
    client.ensure_directory(REMOTE_ROOT)

    state_mgr = StateManager(project_root)
    manifest = state_mgr.load_files()
    files = []

    for path in iter_local_files(project_root):
        if not path.is_file():
            continue
        if should_skip(path):
            continue
        if is_ignored_path(project_root, path):
            continue
        if is_cloud_placeholder(path):
            continue

        relative = str(path.relative_to(project_root)).replace("\\", "/")
        try:
            quick = file_signature(path)
        except (PermissionError, OSError):
            continue

        if quick.get("size", 0) > MAX_FILE_SIZE:
            continue

        try:
            signature = file_signature_full(path)
        except (PermissionError, OSError):
            continue

        if signatures_match(manifest.get(relative), signature):
            continue

        files.append((path, relative, signature))

    synced = []
    for path, relative, signature in files:
        remote = remote_path_for(path, project_root)
        if verbose:
            print(f"[SYNC] {relative}")
        try:
            uploaded = client.upload_file(path, remote)
            if uploaded:
                synced.append((relative, signature))
                if session_guard.OFFLINE_STATE.mark_online():
                    session_guard.log("[ONLINE] Kaggle connection restored")
        except Exception as e:
            if session_guard.is_offline_error(e):
                if session_guard.OFFLINE_STATE.mark_offline():
                    session_guard.log(
                        "[OFFLINE] Kaggle server is unreachable; retrying"
                    )
            if verbose:
                print(
                    f"[ERROR] {relative}: "
                    f"{session_guard.format_exception(e, client.token)}"
                )

    if synced:
        with STATE_LOCK:
            manifest = state_mgr.load_files()
            for relative, signature in synced:
                manifest[relative] = signature
            state_mgr.save_files(manifest)

    return len(synced)

def initial_sync(
    client,
    project_root,
):

    project_root = Path(
        project_root
    ).resolve()

    print()
    print("=" * 60)
    print("Initial project synchronization")
    print("=" * 60)
    print()
    print(
        "Local :",
        project_root
    )
    print(
        "Remote:",
        f"/kaggle/working/{REMOTE_ROOT}"
    )
    print()

    client.ensure_directory(
        REMOTE_ROOT
    )

    manifest = load_sync_manifest(project_root)
    files = []
    cloud_skipped = []
    oversized = []

    for path in iter_local_files(project_root):

        if not path.is_file():
            continue

        if should_skip(path):
            continue

        if is_ignored_path(project_root, path):
            continue

        # Cloud-only OneDrive placeholders: listing them is fine,
        # but opening/reading would force a full download.
        if is_cloud_placeholder(path):
            try:
                cloud_skipped.append(
                    str(path.relative_to(project_root))
                )
            except Exception:
                cloud_skipped.append(str(path))
            continue

        relative = str(path.relative_to(project_root))

        try:
            quick = file_signature(path)
        except (PermissionError, OSError):
            skip_locked_message(path)
            continue

        if quick.get("size", 0) > MAX_FILE_SIZE:
            oversized.append((relative, quick.get("size", 0)))
            continue

        try:
            signature = file_signature_full(path)
        except (PermissionError, OSError):
            skip_locked_message(path)
            continue

        if signatures_match(manifest.get(relative), signature):
            print(
                "[SKIP unchanged]",
                relative,
            )
            continue

        files.append((path, relative, signature))

    def sync_one(item):

        path, relative, signature = item

        remote = remote_path_for(
            path,
            project_root,
        )

        print(
            "[SYNC]",
            relative
        )

        uploaded = client.upload_file(
            path,
            remote,
        )

        if not uploaded:
            return None

        return relative, signature

    executor = ThreadPoolExecutor(max_workers=SYNC_WORKERS)
    try:
        futures = [
            executor.submit(sync_one, item)
            for item in files
        ]

        synced = []

        for future in as_completed(futures):
            result = future.result()

            if result is not None:
                synced.append(result)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for relative, signature in synced:
        manifest[relative] = signature

    save_sync_manifest(project_root, manifest)

    # Ensure the remote-manifest key exists from the first run.
    save_remote_manifest(
        project_root,
        load_remote_manifest(project_root),
    )

    count = len(synced)

    if oversized:
        oversized_sorted = sorted(
            oversized,
            key=lambda item: item[1],
            reverse=True,
        )

        print()
        print(
            f"{len(oversized_sorted)} files skipped: larger than "
            f"{MAX_FILE_SIZE_MB} MB"
        )

        for name, size in oversized_sorted[:5]:
            print(
                f"  [LARGE] {name} "
                f"({size / 1024 / 1024:.1f} MB)"
            )

        print(
            "Hint: add to .kagglesyncignore or attach as a "
            "Kaggle Dataset (/kaggle/input/<name>)"
        )

    if cloud_skipped:
        print()
        print(
            f"{len(cloud_skipped)} cloud-only OneDrive "
            "files skipped "
            "(right-click -> Always keep on this device)"
        )

        for name in sorted(cloud_skipped)[:5]:
            print("  [CLOUD]", name)

    print()
    print(
        f"Initial synchronization complete: "
        f"{count} files"
    )
    print()

    warn_remote_size(client)


def print_dataset_tip(project_root):
    """One-time pointer for projects with local data folders."""

    for name in (
        "data",
        "dataset",
        "datasets",
        "kaggle_datasets",
    ):

        try:
            if (Path(project_root) / name).is_dir():
                break
        except Exception:
            continue

    else:
        return

    print()
    print(
        "Tip: local data folders are ignored by default "
        "(.kagglesyncignore)."
    )
    print(
        "Attach the dataset in the Kaggle notebook sidebar "
        "(Add data), then read it from "
        "/kaggle/input/<dataset-slug>/."
    )
    print()


def warn_remote_size(client):
    """Warn when the remote project nears the working-disk limit."""

    try:
        entries = client.list_files(REMOTE_ROOT)
    except Exception:
        return

    total = 0

    for entry in entries or []:

        if not isinstance(entry, dict):
            continue

        size = entry.get("size")

        if isinstance(size, (int, float)):
            total += size

    if total <= REMOTE_SIZE_WARN_BYTES:
        return

    print()
    print(
        f"WARNING: remote project uses "
        f"{total / 1024 / 1024 / 1024:.1f} GB "
        f"of ~20 GB on /kaggle/working."
    )
    print(
        "Add large files/folders to .kagglesyncignore or attach "
        "them as a Kaggle Dataset (/kaggle/input/<name>)."
    )
    print()


# ============================================================
# Watcher
# ============================================================

class SyncHandler(
    FileSystemEventHandler
):

    def __init__(
        self,
        client,
        project_root,
        dependency_manager,
    ):

        self.client = client

        self.project_root = Path(
            project_root
        ).resolve()

        self.dependencies = (
            dependency_manager
        )

        self._debounce_timers = {}
        self._debounce_lock = threading.Lock()
        self._file_upload_locks = {}
        self._active_timers = set()
        self._stopped = threading.Event()


    def stop(self):
        self._stopped.set()
        with self._debounce_lock:
            pending = list(self._debounce_timers.values())
            timers = set(pending)
            timers.update(self._active_timers)
            self._debounce_timers.clear()
        for timer in pending:
            timer.cancel()
        deadline = time.monotonic() + 5
        for timer in timers:
            if timer is not threading.current_thread():
                timer.join(timeout=max(0, deadline - time.monotonic()))
        return [timer for timer in timers if timer.is_alive()]


    def sync_file(self, filename):

        if self._stopped.is_set() or session_guard.SESSION_DEAD.is_set():
            return

        try:
            path = Path(filename).resolve()
        except Exception:
            return

        if should_skip(path):
            return

        if is_ignored_path(self.project_root, path):
            return

        if is_cloud_placeholder(path):
            return

        if is_recently_downloaded(path):
            return

        key = str(path)

        with self._debounce_lock:

            old_timer = self._debounce_timers.pop(key, None)

            if old_timer is not None:
                try:
                    old_timer.cancel()
                except Exception:
                    pass

            timer = threading.Timer(
                DEBOUNCE_DELAY,
                self._do_sync_file,
                args=(key,),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()


    def _do_sync_file(self, filename_str):
        current_thread = threading.current_thread()
        with self._debounce_lock:
            self._active_timers.add(current_thread)
            lock = self._file_upload_locks.setdefault(
                filename_str,
                threading.Lock(),
            )
        try:
            with lock:
                self._do_sync_file_locked(filename_str)
        finally:
            with self._debounce_lock:
                self._active_timers.discard(current_thread)


    def _do_sync_file_locked(self, filename_str):

        with self._debounce_lock:
            self._debounce_timers.pop(filename_str, None)

        if self._stopped.is_set() or session_guard.SESSION_DEAD.is_set():
            return

        path = Path(filename_str)

        if should_skip(path):
            return

        if is_ignored_path(self.project_root, path):
            return

        if is_cloud_placeholder(path):
            return

        if is_recently_downloaded(path):
            return

        if not path.exists():
            return

        if not path.is_file():
            return

        try:
            relative = path.relative_to(self.project_root)
        except Exception:
            return

        relative_key = str(relative)

        try:
            current_stat = file_signature(path)
        except (PermissionError, OSError):
            current_stat = None
        except Exception:
            current_stat = None

        if (
            current_stat is not None
            and isinstance(current_stat.get("size"), int)
            and current_stat["size"] > MAX_FILE_SIZE
        ):

            with _OVERSIZE_WATCHER_LOCK:

                if relative_key not in _OVERSIZE_WATCHER_LOGGED:

                    _OVERSIZE_WATCHER_LOGGED.add(relative_key)

                    print(
                        f"[SKIP] {relative} "
                        f"({current_stat['size'] / 1024 / 1024:.1f} MB > "
                        f"{MAX_FILE_SIZE / 1024 / 1024:.0f} MB)"
                    )

            return

        if current_stat is not None:

            with STATE_LOCK:
                stored = load_sync_manifest(
                    self.project_root
                ).get(relative_key)

            # Attribute-only touch (OneDrive loves these): the
            # size + mtime already recorded -> nothing to do.
            if signatures_match(stored, current_stat):
                return

            # Same size, new mtime, small file with a known hash:
            # compare content before deciding to upload.
            if (
                isinstance(stored, dict)
                and stored.get("sha256")
                and isinstance(current_stat.get("size"), int)
                and current_stat["size"] <= HASH_LIMIT_BYTES
            ):

                try:
                    current_hash = sha256_file(path)
                except (PermissionError, OSError):
                    current_hash = None
                except Exception:
                    current_hash = None

                if (
                    current_hash is not None
                    and current_hash == stored.get("sha256")
                ):

                    refreshed = {}
                    refreshed.update(current_stat)
                    refreshed["sha256"] = current_hash

                    with STATE_LOCK:
                        manifest = load_sync_manifest(
                            self.project_root
                        )
                        manifest[relative_key] = refreshed
                        save_sync_manifest(
                            self.project_root,
                            manifest,
                        )

                    return

        for attempt in range(3):

            try:

                remote = remote_path_for(
                    path,
                    self.project_root,
                )

                print(
                    "[SYNC]",
                    relative,
                )

                uploaded = self.client.upload_file(
                    path,
                    remote,
                )

                if not uploaded:
                    return

                with STATE_LOCK:
                    manifest = load_sync_manifest(
                        self.project_root
                    )
                    manifest[relative_key] = file_signature_full(
                        path
                    )
                    save_sync_manifest(
                        self.project_root,
                        manifest,
                    )

                if session_guard.OFFLINE_STATE.mark_online():
                    session_guard.log("[ONLINE] Kaggle connection restored")
                _POLL_RESET.set()

                if (
                    path.name.startswith(
                        "requirements"
                    )
                    and path.suffix == ".txt"
                ):

                    self.dependencies.request_check()

                return

            except Exception as e:

                if note_auth_error(e):
                    return

                if session_guard.is_offline_error(e):
                    if session_guard.OFFLINE_STATE.mark_offline():
                        session_guard.log(
                            "[OFFLINE] Kaggle server is unreachable; retrying"
                        )

                if isinstance(e, (PermissionError, OSError)):

                    if attempt < 2:
                        time.sleep(0.3)
                        continue

                print(
                    "[ERROR]",
                    path,
                    session_guard.format_exception(e, self.client.token),
                )
                return


    def _remove_manifest_entry(self, relative):

        try:
            StateManager(self.project_root).delete_file(str(relative))
        except Exception:
            pass


    def on_created(self, event):

        if event.is_directory:
            return

        self.sync_file(
            event.src_path
        )


    def on_modified(self, event):

        if event.is_directory:
            return

        self.sync_file(
            event.src_path
        )


    def on_moved(self, event):

        if event.is_directory:
            return

        if session_guard.SESSION_DEAD.is_set():
            return

        src_path = Path(event.src_path)
        dest_path = Path(event.dest_path)

        src_skipped = should_skip(src_path)
        dest_skipped = should_skip(dest_path)

        if not src_skipped and is_ignored_path(
            self.project_root,
            src_path,
        ):
            # Ignored local files are never deleted remotely.
            src_skipped = True

        if src_skipped and dest_skipped:
            return

        if is_recently_downloaded(src_path) and (
            dest_skipped or is_recently_downloaded(dest_path)
        ):
            return

        if not src_skipped:

            try:

                old_remote = remote_path_for(
                    event.src_path,
                    self.project_root,
                )

                delete_response = self.client.request_session().delete(
                    self.client.api_url(old_remote),
                    timeout=REQUEST_TIMEOUT,
                )

                if delete_response.status_code not in (
                    200,
                    204,
                    404,
                ):
                    delete_response.raise_for_status()

            except Exception as e:

                if note_auth_error(e):
                    return

            try:
                old_relative = Path(
                    event.src_path
                ).resolve().relative_to(
                    self.project_root
                )
                self._remove_manifest_entry(
                    str(old_relative)
                )
            except Exception:
                pass

        if not dest_skipped:
            self.sync_file(event.dest_path)


    def on_deleted(self, event):

        if event.is_directory:
            return

        if session_guard.SESSION_DEAD.is_set():
            return

        src_path = Path(event.src_path)

        if should_skip(src_path):
            return

        if is_ignored_path(self.project_root, src_path):
            try:
                relative = src_path.resolve().relative_to(
                    self.project_root
                )
                self._remove_manifest_entry(
                    str(relative)
                )
            except Exception:
                pass

            return

        if is_recently_downloaded(src_path):
            return

        try:

            remote = remote_path_for(
                event.src_path,
                self.project_root,
            )

            print(
                "[DELETE]",
                event.src_path
            )

            delete_response = self.client.request_session().delete(
                self.client.api_url(remote),
                timeout=REQUEST_TIMEOUT,
            )

            if delete_response.status_code not in (
                200,
                204,
                404,
            ):
                delete_response.raise_for_status()

            try:
                relative = src_path.resolve().relative_to(
                    self.project_root
                )
                self._remove_manifest_entry(
                    str(relative)
                )
            except Exception:
                pass

        except Exception as e:

            if note_auth_error(e):
                return

            print(
                "[ERROR]",
                session_guard.format_exception(e, self.client.token),
            )


def format_bytes(count):

    try:
        number = float(count)
    except (TypeError, ValueError):
        return "unknown size"

    if number < 0:
        return "unknown size"

    if number < 1024:
        return f"{int(number)} B"

    for unit in ("KB", "MB", "GB", "TB"):
        number /= 1024.0

        if number < 1024 or unit == "TB":
            return f"{number:.1f} {unit}"

    return f"{number:.1f} TB"


def auto_download_eligible(relative, size):

    if DOWNLOAD_POLICY == "off":
        return False

    if DOWNLOAD_POLICY == "all":
        return True

    if not isinstance(size, (int, float)):
        return False

    if size > DOWNLOAD_MAX_BYTES:
        return False

    extension = os.path.splitext(relative)[1].lower()

    return extension in DOWNLOAD_EXTENSIONS


def _remote_should_descend(project_root, relative_dir):
    """True when remote polling may list a remote directory."""

    if not relative_dir:
        return True

    parts = relative_dir.split("/")

    for part in parts:

        if part in EXCLUDED_DIRS:
            return False

        if part in REMOTE_POLL_SKIP_DIRS:
            return False

        if part.lower() in REMOTE_EXCLUDED_DIRS:
            return False

    try:

        if is_ignored_path(
            project_root,
            Path(project_root) / relative_dir,
        ):
            return False

    except Exception:
        pass

    return True


def _list_remote_dir(client, remote_dir_path):

    response = client.request_session().get(
        client.api_url(remote_dir_path),
        params={"content": 1},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        return []

    if data.get("type") == "file":
        return [data]

    return [
        entry
        for entry in (data.get("content") or [])
        if isinstance(entry, dict)
    ]


def list_remote_tree_bfs(client, project_root):
    """List remote files with iterative BFS, pruning heavy dirs.

    Never descends into directories matched by ignore rules,
    EXCLUDED_DIRS, REMOTE_EXCLUDED_DIRS or the poll skip list.
    Up to REMOTE_POLL_CONCURRENCY directories are fetched
    concurrently. Directory last_modified is never used to skip
    subtrees (it does not change when nested files change).
    """

    found_files = []
    pending_dirs = [REMOTE_ROOT]

    executor = ThreadPoolExecutor(max_workers=REMOTE_POLL_CONCURRENCY)
    try:

        while pending_dirs:

            batch = pending_dirs[:REMOTE_POLL_CONCURRENCY]
            pending_dirs = pending_dirs[REMOTE_POLL_CONCURRENCY:]

            futures = {
                executor.submit(
                    _list_remote_dir,
                    client,
                    directory,
                ): directory
                for directory in batch
            }

            for future in as_completed(futures):

                for entry in future.result():

                    entry_type = entry.get("type")
                    entry_path = entry.get("path", "").replace("\\", "/")

                    if not entry_path.startswith(f"{REMOTE_ROOT}/"):
                        continue

                    relative = entry_path[len(REMOTE_ROOT) + 1:]

                    if entry_type == "directory":

                        if _remote_should_descend(
                            project_root,
                            relative,
                        ):
                            pending_dirs.append(entry_path)

                    elif entry_type == "file":
                        found_files.append(entry)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return found_files


def remote_sync_loop(client, project_root, stop_event):

    project_root = Path(project_root).resolve()
    remote_manifest = load_remote_manifest(project_root)
    interval = REMOTE_SYNC_INTERVAL
    idle_cycles = 0

    while True:

        if session_guard.SESSION_DEAD.is_set():
            break

        wait_interval = interval
        try:
            with STATE_LOCK:
                local_manifest = load_sync_manifest(project_root)
                pending = load_pending_remote(project_root)

            remote_files = list_remote_tree_bfs(
                client,
                project_root,
            )
            if session_guard.OFFLINE_STATE.mark_online():
                session_guard.log("[ONLINE] Kaggle connection restored")
            if session_guard.OFFLINE_STATE.recovered.is_set():
                session_guard.OFFLINE_STATE.recovered.clear()
                sync_once(client, project_root, verbose=True)
            current_remote = {}
            changed = False

            for entry in remote_files:

                remote_path = entry.get("path", "").replace("\\", "/")

                if not remote_path.startswith(f"{REMOTE_ROOT}/"):
                    continue

                relative = remote_path[len(REMOTE_ROOT) + 1:]

                if should_skip(Path(relative)):
                    continue

                if should_skip_remote(relative):
                    continue

                if is_ignored_path(
                    project_root,
                    project_root / Path(relative),
                ):
                    continue

                remote_signature = {
                    key: entry.get(key)
                    for key in (
                        "size",
                        "last_modified",
                        "created",
                    )
                    if entry.get(key) is not None
                }

                current_remote[relative] = remote_signature
                local_path = project_root / Path(relative)
                old_remote_signature = remote_manifest.get(relative)

                if (
                    old_remote_signature == remote_signature
                    and local_path.exists()
                ):
                    continue

                if local_path.exists():
                    current_local_signature = file_signature(local_path)
                    synced_local_signature = local_manifest.get(relative)

                    if synced_local_signature is not None and not signatures_match(synced_local_signature, current_local_signature):
                        print(
                            "[CONFLICT] Local file changed; keeping local:",
                            relative,
                        )
                        continue

                size = entry.get("size")
                last_modified = entry.get("last_modified")

                if not auto_download_eligible(relative, size):

                    if DOWNLOAD_POLICY != "off":

                        record = {
                            "size": size,
                            "last_modified": last_modified,
                        }

                        if pending.get(relative) != record:
                            pending[relative] = record
                            changed = True

                            print(
                                f"[REMOTE NEW] {relative} "
                                f"({format_bytes(size)}) - "
                                "fetch with: "
                                f"kaggle-pull {relative}"
                            )

                    continue

                # Register BEFORE writing so the watcher ignores
                # both the tmp file and the final downloaded file.
                mark_recently_downloaded(local_path)

                print("[DOWNLOAD]", relative)
                client.download_file(remote_path, local_path)

                pending.pop(relative, None)
                changed = True

                with STATE_LOCK:
                    fresh = load_sync_manifest(project_root)
                    fresh[relative] = file_signature_full(local_path)
                    save_sync_manifest(project_root, fresh)
                    local_manifest = fresh

            for relative in list(pending):

                if relative not in current_remote:
                    del pending[relative]
                    changed = True

            with STATE_LOCK:
                merged_local = load_sync_manifest(project_root)
                for key, value in local_manifest.items():
                    merged_local[key] = value
                save_sync_manifest(project_root, merged_local)
                save_remote_manifest(project_root, current_remote)
                save_pending_remote(project_root, pending)

            remote_manifest = current_remote

            if changed or _POLL_RESET.is_set():
                _POLL_RESET.clear()
                interval = REMOTE_SYNC_INTERVAL
                idle_cycles = 0
            else:
                idle_cycles += 1

                if idle_cycles >= REMOTE_POLL_IDLE_CYCLES:
                    idle_cycles = 0
                    interval = min(
                        interval * 1.5,
                        REMOTE_POLL_MAX_INTERVAL,
                    )

        except Exception as e:

            if note_auth_error(e):
                try:
                    stop_event.set()
                except Exception:
                    pass
                break

            if session_guard.is_offline_error(e):
                if session_guard.OFFLINE_STATE.mark_offline():
                    session_guard.log(
                        "[OFFLINE] Kaggle server is unreachable; retrying"
                    )
                wait_interval = session_guard.OFFLINE_STATE.get_backoff()

            print(
                "[REMOTE SYNC ERROR]",
                session_guard.format_exception(e, client.token),
            )

        if stop_event.wait(wait_interval):
            break


# ============================================================
# Main
# ============================================================

def run_sync(project_root: Path, server_url: str):
    bind_state_lock(project_root)
    warn_if_onedrive(project_root)
    migrate_legacy_state(project_root)
    print_dataset_tip(project_root)

    print()
    print("Connecting to Kaggle Jupyter Server...")

    client = JupyterClient(server_url)

    # Verify API
    try:
        response = client.session.get(
            f"{client.base_url}/api",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            session_guard.log_session_expired()
            urlstore.delete_url(project_root)
            sys.exit(session_guard.EXIT_EXPIRED)
        print(
            "ERROR: Could not connect to Kaggle: "
            f"{session_guard.format_exception(e, client.token)}"
        )
        sys.exit(session_guard.EXIT_FAILURE)

    print("Kaggle Jupyter Server: OK")

    # Start Heartbeat thread
    heartbeat = urlstore.Heartbeat(project_root)
    heartbeat.start()
    remote_stop_event = threading.Event()
    dependency_manager = None
    handler = None
    remote_thread = None
    observer = None
    exit_code = session_guard.EXIT_OK
    try:
        initial_sync(client, project_root)

        dependency_manager = DependencyManager(client, project_root)
        try:
            dependency_manager.check()
        except Exception as e:
            print(
                "Initial dependency check failed:",
                session_guard.format_exception(e, client.token),
            )

        handler = SyncHandler(client, project_root, dependency_manager)
        remote_thread = threading.Thread(
            target=remote_sync_loop,
            args=(client, project_root, remote_stop_event),
            name="kaggle-remote-sync",
            daemon=True,
        )
        remote_thread.start()

        observer = Observer()
        observer.schedule(handler, str(project_root), recursive=True)
        observer.start()

        print()
        print("=" * 60)
        print("KAGGLE AUTOMATIC SYNCHRONIZATION IS RUNNING")
        print("=" * 60)
        print()
        print("Local project:")
        print(project_root)
        print()
        print("Remote project:")
        print(f"/kaggle/working/{REMOTE_ROOT}")
        print()
        print("Requirements are installed only when their contents change.")
        print()
        if DOWNLOAD_POLICY == "all":
            print("Remote auto-download: everything new.")
        elif DOWNLOAD_POLICY == "off":
            print("Remote auto-download: off; fetch with kaggle-pull.")
        else:
            print(f"Remote auto-download: small text/image files <= {DOWNLOAD_MAX_MB:g} MB; others via kaggle-pull.")
        print()
        print(f"Kaggle-created files are checked every {REMOTE_SYNC_INTERVAL:g} seconds.")
        print()
        print("Press Ctrl+C to stop.")
        print()

        while True:
            time.sleep(1)
            if session_guard.SESSION_DEAD.is_set():
                session_guard.log_session_expired()
                exit_code = session_guard.EXIT_EXPIRED
                break
    except KeyboardInterrupt:
        print()
        print("Stopping synchronization...")
    finally:
        remote_stop_event.set()
        if observer is not None:
            observer.stop()
        pending_timers = handler.stop() if handler is not None else []
        heartbeat.stop()
        if dependency_manager is not None:
            try:
                dependency_manager.stop()
            except Exception as e:
                print(
                    "WARNING: dependency worker shutdown failed: "
                    f"{session_guard.format_exception(e, client.token)}"
                )
        urlstore.delete_url(project_root)
        threads = [heartbeat]
        if observer is not None:
            threads.append(observer)
        if remote_thread is not None:
            threads.append(remote_thread)
        dependency_thread = (
            dependency_manager._dep_thread
            if dependency_manager is not None
            else None
        )
        if dependency_thread is not None:
            threads.append(dependency_thread)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
        for timer in pending_timers:
            if timer.is_alive():
                print(
                    "WARNING: thread still alive after shutdown timeout: "
                    f"{timer.name}"
                )
        for thread in threads:
            if thread.is_alive():
                print(
                    "WARNING: thread still alive after shutdown timeout: "
                    f"{thread.name}"
                )

    sys.exit(exit_code)


def _main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if argv is None:
        argv = sys.argv[1:]

    if "--version" in argv or "-V" in argv:
        print(f"kaggle-sync {__version__}")
        sys.exit(session_guard.EXIT_OK)

    if argv and argv[0] == "doctor":
        from .doctor import main as doctor_cli
        doctor_cli(argv[1:])
        return

    if argv and argv[0] == "forget":
        parser = argparse.ArgumentParser(
            prog="kaggle-sync forget",
            description="Delete this project's URL file, heartbeat, and legacy URL files.",
        )
        parser.add_argument("--project", default=None, help="Project directory (default: cwd)")
        args = parser.parse_args(argv[1:])
        proj = Path(args.project).resolve() if args.project else Path.cwd().resolve()
        removed = urlstore.forget(proj)
        if removed:
            print("Removed:")
            for p in removed:
                print(f"  - {p}")
        else:
            print("Nothing removed: no URL, heartbeat, or legacy files found.")
        sys.exit(session_guard.EXIT_OK)

    parser = argparse.ArgumentParser(
        prog="kaggle-sync",
        description="Synchronize local project files to Kaggle GPU environment.",
    )
    parser.add_argument("url", nargs="?", help="Kaggle Jupyter Server URL")
    parser.add_argument("--project", default=None, help="Project directory (default: cwd)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    try:
        args = parser.parse_args(argv)
    except SystemExit as se:
        sys.exit(se.code)

    proj = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    if not proj.exists() or not proj.is_dir():
        print(f"ERROR: Project directory does not exist: {proj}", file=sys.stderr)
        sys.exit(session_guard.EXIT_USAGE)

    url = args.url
    if not url:
        env_url = os.getenv("KAGGLE_RUNNER_URL")
        if env_url and env_url.strip():
            url = env_url.strip()
        elif urlstore.is_interactive():
            import getpass
            try:
                url = getpass.getpass("Enter Kaggle URL: ").strip()
            except (KeyboardInterrupt, EOFError):
                sys.exit(session_guard.EXIT_USAGE)
            if not url:
                print("Usage: kaggle-sync [URL] [--project DIR]", file=sys.stderr)
                sys.exit(session_guard.EXIT_USAGE)
        else:
            print("Usage: kaggle-sync [URL] [--project DIR]", file=sys.stderr)
            sys.exit(session_guard.EXIT_USAGE)

    # Save URL
    if args.url:
        print(
            "Warning: URL on the command line can end up in shell history; "
            "run kaggle-sync without arguments to use the hidden prompt"
        )
    urlstore.save_url(proj, url)

    # Clean legacy file
    urlstore.cleanup_legacy_url_file()

    # Secret files count
    sec_count = ignore_rules.count_secret_files(proj)
    if sec_count > 0:
        print(f"{sec_count} secret-looking files never synced")

    run_sync(proj, url)


def main(argv=None):
    return session_guard.cli_main(_main, argv)


if __name__ == "__main__":
    main()
