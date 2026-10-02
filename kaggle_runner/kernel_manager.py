"""Runner-kernel ownership and per-project run coordination."""

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

from . import runner_paths, urlstore


DEFAULT_KERNEL_NAME = "python3"
KERNEL_CREATE_TIMEOUT = 30.0
KERNEL_SHUTDOWN_TIMEOUT = 10.0
RUNNER_BUSY_MESSAGE = (
    "Another kaggle-run is still active "
    "(use --stop, or --new-kernel to run in parallel)"
)

NOTEBOOK_CWD_SNIPPET = """\
# Kaggle Runner Notebook Helper: align working directory and sys.path
import os, sys
_PROJECT_DIR = '/kaggle/working/local-project'
if os.path.exists(_PROJECT_DIR):
    if os.getcwd() != _PROJECT_DIR:
        try:
            os.chdir(_PROJECT_DIR)
        except OSError:
            pass
    if _PROJECT_DIR not in sys.path:
        sys.path.insert(0, _PROJECT_DIR)
"""


def get_notebook_cwd_snippet() -> str:
    return NOTEBOOK_CWD_SNIPPET


class KernelManager:
    """Own one persistent runner kernel and optional ephemeral kernels."""

    def __init__(self, client, project_root):
        self.client = client
        self.project_root = Path(project_root).resolve()
        self._kernel_id: Optional[str] = None
        self._ephemeral_kernel_id: Optional[str] = None
        self._lock_path = runner_paths.lock_file(self.project_root).with_suffix(
            ".runner.lock"
        )
        self._lock_owned = False

    def _state(self):
        from .sync import StateManager

        return StateManager(self.project_root)

    def _request(self, method, path, **kwargs):
        response = getattr(self.client.session, method)(
            f"{self.client.base_url}/api/{path.lstrip('/')}",
            timeout=kwargs.pop("timeout", KERNEL_CREATE_TIMEOUT),
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _create_kernel(self) -> str:
        kernelspecs = self.client.session.get(
            f"{self.client.base_url}/api/kernelspecs",
            timeout=10,
        )
        if kernelspecs.status_code == 200:
            name = (kernelspecs.json() or {}).get("default") or DEFAULT_KERNEL_NAME
        else:
            name = DEFAULT_KERNEL_NAME
        response = self._request(
            "post",
            "kernels",
            json={"name": name},
        )
        kernel_id = (response.json() or {}).get("id")
        if not kernel_id:
            raise RuntimeError("Could not create a Kaggle runner kernel.")
        return kernel_id

    def _is_alive(self, kernel_id: str) -> bool:
        response = self.client.session.get(
            f"{self.client.base_url}/api/kernels/{kernel_id}",
            timeout=10,
        )
        return (
            response.status_code == 200
            and (response.json() or {}).get("execution_state") != "dead"
        )

    def acquire_runner_slot(self) -> bool:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created": time.time(),
        }).encode("utf-8")

        for _ in range(2):
            try:
                fd = os.open(
                    os.fspath(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    owner = json.loads(self._lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    owner = {}
                pid = owner.get("pid")
                host = owner.get("host")
                if (
                    host == socket.gethostname()
                    and isinstance(pid, int)
                    and not urlstore._is_pid_alive(pid)
                ):
                    try:
                        self._lock_path.unlink()
                    except OSError:
                        return False
                    continue
                return False

            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._lock_owned = True
            return True
        return False

    def release_runner_slot(self):
        if not self._lock_owned:
            return
        try:
            owner = json.loads(self._lock_path.read_text(encoding="utf-8"))
            if (
                owner.get("pid") == os.getpid()
                and owner.get("host") == socket.gethostname()
            ):
                self._lock_path.unlink()
        except (OSError, ValueError):
            pass
        self._lock_owned = False

    def ensure_runner_kernel(self) -> str:
        kernel_id = self._state().load_runner_kernel()
        if kernel_id:
            try:
                if self._is_alive(kernel_id):
                    self._kernel_id = kernel_id
                    return kernel_id
            except Exception:
                pass
        kernel_id = self._create_kernel()
        self._state().save_runner_kernel(kernel_id)
        self._kernel_id = kernel_id
        return kernel_id

    def ensure_pinned_kernel(self, kernel_id: str) -> str:
        if not self._is_alive(kernel_id):
            raise RuntimeError(f"Configured Kaggle kernel is not active: {kernel_id}")
        return kernel_id

    def ensure_shared_kernel(self) -> str:
        response = self._request("get", "sessions", timeout=15)
        sessions = response.json() or []
        for session in sessions:
            kernel = session.get("kernel", {})
            kernel_id = kernel.get("id")
            if (
                kernel_id
                and session.get("path", "").endswith(".ipynb")
                and "python" in kernel.get("name", "").lower()
            ):
                return kernel_id
        raise RuntimeError(
            "No active notebook Python kernel found for --shared."
        )

    def restart_runner_kernel(self) -> str:
        self.stop_runner_kernel()
        return self.ensure_runner_kernel()

    def stop_runner_kernel(self) -> bool:
        kernel_id = self._state().load_runner_kernel()
        if not kernel_id:
            return False
        response = self.client.session.delete(
            f"{self.client.base_url}/api/kernels/{kernel_id}",
            timeout=KERNEL_SHUTDOWN_TIMEOUT,
        )
        if response.status_code not in (200, 202, 204, 404):
            response.raise_for_status()
        self._state().save_runner_kernel(None)
        self._kernel_id = None
        return True

    def create_ephemeral_kernel(self) -> str:
        self._ephemeral_kernel_id = self._create_kernel()
        return self._ephemeral_kernel_id

    def shutdown_ephemeral_kernel(self):
        if self._ephemeral_kernel_id:
            kernel_id = self._ephemeral_kernel_id
            self._ephemeral_kernel_id = None
            response = self.client.session.delete(
                f"{self.client.base_url}/api/kernels/{kernel_id}",
                timeout=KERNEL_SHUTDOWN_TIMEOUT,
            )
            if response.status_code not in (200, 202, 204, 404):
                response.raise_for_status()
