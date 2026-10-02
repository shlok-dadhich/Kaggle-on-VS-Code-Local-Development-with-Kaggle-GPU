"""URL storage and heartbeat management for kaggle-sync and kaggle-run.

Manages per-project server URLs, permissions, TTL expiry, legacy migration,
and background sync heartbeats.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

from . import runner_paths


DEFAULT_TTL_HOURS = 13.0
_PERMISSION_WARNING_PRINTED = False


def get_ttl_hours() -> float:
    raw = os.getenv("KAGGLE_RUNNER_URL_TTL_H")
    if raw:
        try:
            return float(raw.strip())
        except ValueError:
            pass
    return DEFAULT_TTL_HOURS


def restrict_permissions(path: Union[str, Path]) -> bool:
    """Restrict file/dir permissions to current user only.

    On POSIX: 0o600 for files, 0o700 for directories.
    On Windows: icacls "<path>" /inheritance:r /grant:r "<current user>:(F)"
    Prints at most one warning on failure, never crashes.
    """
    global _PERMISSION_WARNING_PRINTED
    p = Path(path)
    if not p.exists():
        return False

    try:
        if sys.platform == "win32":
            import getpass
            user = getpass.getuser()
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            res = subprocess.run(
                ["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=creationflags,
            )
            if res.returncode == 0 and "Successfully processed" in res.stdout:
                return True
            if not _PERMISSION_WARNING_PRINTED:
                _PERMISSION_WARNING_PRINTED = True
                print(f"Warning: Failed to restrict permissions on {p} via icacls")
            return False
        else:
            mode = 0o700 if p.is_dir() else 0o600
            os.chmod(p, mode)
            return True
    except Exception as e:
        if not _PERMISSION_WARNING_PRINTED:
            _PERMISSION_WARNING_PRINTED = True
            print(f"Warning: Could not restrict permissions on {p}: {e}")
        return False


def save_url(project, url: str) -> Path:
    """Save URL atomically as JSON {"url": ..., "saved_at": ...} with restricted permissions."""
    target = runner_paths.url_file(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    restrict_permissions(target.parent)

    data = {
        "url": url.strip(),
        "saved_at": time.time(),
    }
    payload = json.dumps(data)

    temp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    temp.write_text(payload, encoding="utf-8")
    restrict_permissions(temp)
    os.replace(temp, target)
    restrict_permissions(target)
    return target


def load_url(project, check_ttl: bool = True) -> Optional[str]:
    """Resolve session URL: env KAGGLE_RUNNER_URL > project url file.

    Handles JSON payload and legacy plain text. Checks TTL (default 13 h);
    if expired, prints new-URL notice and returns None.
    """
    env_url = os.getenv("KAGGLE_RUNNER_URL")
    if env_url and env_url.strip():
        return env_url.strip()

    target = runner_paths.url_file(project)
    if not target.exists():
        # Fall back to legacy ~/.kaggle-runner-url if present
        legacy = Path.home() / ".kaggle-runner-url"
        if legacy.exists():
            try:
                raw_legacy = legacy.read_text(encoding="utf-8").strip()
                if raw_legacy:
                    return raw_legacy
            except OSError:
                pass
        return None

    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not raw:
        return None

    url: Optional[str] = None
    saved_at: Optional[float] = None

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            url = data.get("url")
            saved_at = float(data.get("saved_at", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        # Plain-text legacy content in the file
        url = raw
        try:
            saved_at = target.stat().st_mtime
        except OSError:
            saved_at = time.time()

    if not url:
        return None

    if check_ttl and saved_at is not None:
        ttl = get_ttl_hours()
        age_hours = (time.time() - saved_at) / 3600.0
        if age_hours > ttl:
            print(
                f"\nSaved Kaggle session URL expired ({age_hours:.1f}h old, limit is {ttl:g}h).\n"
                f"Please obtain a fresh URL from Kaggle and run: kaggle-sync <URL>\n"
            )
            delete_url(project)
            return None

    return url


def delete_url(project) -> bool:
    """Remove this project's URL file."""
    target = runner_paths.url_file(project)
    try:
        if target.exists():
            target.unlink()
            return True
    except OSError:
        pass
    return False


def cleanup_legacy_url_file() -> Optional[Path]:
    """Check for legacy ~/.kaggle-runner-url, print notice and delete it."""
    legacy = Path.home() / ".kaggle-runner-url"
    try:
        if legacy.exists():
            legacy.unlink()
            print("Notice: Removed legacy global URL file ~/.kaggle-runner-url")
            return legacy
    except OSError:
        pass
    return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def sync_state(project_root) -> str:
    """Return 'running' if heartbeat within 20 s and pid alive, else 'stale' or 'absent'."""
    hb_file = runner_paths.heartbeat_file(project_root)
    if not hb_file.exists():
        return "absent"

    try:
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        ts = float(data["ts"])
    except Exception:
        return "stale"

    now = time.time()
    if (now - ts) <= 20.0 and _is_pid_alive(pid):
        return "running"
    return "stale"


def remove_heartbeat(project_root) -> bool:
    hb_file = runner_paths.heartbeat_file(project_root)
    try:
        if hb_file.exists():
            hb_file.unlink()
            return True
    except OSError:
        pass
    return False


class Heartbeat(threading.Thread):
    """Daemon thread writing atomic heartbeat file every 5 s while sync is alive."""

    def __init__(self, project_root, interval: float = 5.0):
        super().__init__(name="kaggle-heartbeat", daemon=True)
        self.project_root = project_root
        self.interval = interval
        self.stop_event = threading.Event()
        self.hb_file = runner_paths.heartbeat_file(project_root)

    def _write_beat(self):
        try:
            payload = json.dumps({"pid": os.getpid(), "ts": time.time()})
            temp = self.hb_file.with_name(f"{self.hb_file.name}.tmp.{os.getpid()}")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.hb_file)
        except Exception:
            pass

    def run(self):
        self.hb_file.parent.mkdir(parents=True, exist_ok=True)
        self._write_beat()
        while not self.stop_event.wait(self.interval):
            self._write_beat()

    def stop(self):
        self.stop_event.set()
        remove_heartbeat(self.project_root)


def forget(project_root) -> List[str]:
    """Delete this project's URL file, heartbeat, and any legacy URL file. Returns list of removed paths."""
    removed: List[str] = []

    target = runner_paths.url_file(project_root)
    try:
        if target.exists():
            target.unlink()
            removed.append(str(target))
    except OSError:
        pass

    hb_file = runner_paths.heartbeat_file(project_root)
    try:
        if hb_file.exists():
            hb_file.unlink()
            removed.append(str(hb_file))
    except OSError:
        pass

    legacy = Path.home() / ".kaggle-runner-url"
    try:
        if legacy.exists():
            legacy.unlink()
            removed.append(str(legacy))
    except OSError:
        pass

    return removed