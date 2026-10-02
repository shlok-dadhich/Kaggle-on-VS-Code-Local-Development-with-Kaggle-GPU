"""Dedicated runner kernel management and notebook cwd helper for kaggle-run.

Keeps a persistent kernel for script runs, avoiding conflicts with
the user's notebook kernel. The kernel is created on first use and
reused across runs; it is shut down on clean exit.
"""

from typing import List, Optional

import requests


DEFAULT_KERNEL_NAME = "python3"
KERNEL_CREATE_TIMEOUT = 30.0
KERNEL_SHUTDOWN_TIMEOUT = 10.0

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
    """Return Python code snippet for Kaggle notebooks to set CWD and PYTHONPATH."""
    return NOTEBOOK_CWD_SNIPPET


class KernelManager:
    """Manages a dedicated kernel for kaggle-run script execution."""

    def __init__(self, client):
        self.client = client
        self._kernel_id: Optional[str] = None
        self._kernel_owner = False

    def ensure_kernel(self) -> str:
        """Get or create the dedicated runner kernel."""
        if self._kernel_id and self._kernel_owner:
            if self._is_kernel_alive(self._kernel_id):
                return self._kernel_id
            self._kernel_id = None
            self._kernel_owner = False

        kernel_id = self._find_available_kernel()
        if kernel_id:
            self._kernel_id = kernel_id
            self._kernel_owner = False
            return kernel_id

        kernel_id = self._create_kernel()
        self._kernel_id = kernel_id
        self._kernel_owner = True
        return kernel_id

    def _find_available_kernel(self) -> Optional[str]:
        try:
            response = self.client.session.get(
                f"{self.client.base_url}/api/kernels",
                timeout=10,
            )
            response.raise_for_status()
            kernels = response.json() or []
        except Exception:
            return None

        sessions = self._get_sessions()
        notebook_kernel_ids = {
            s.get("kernel", {}).get("id")
            for s in sessions
            if s.get("path", "").endswith(".ipynb")
        }

        for kernel in kernels:
            kernel_id = kernel.get("id")
            name = kernel.get("name", "").lower()
            if name and name != "python3":
                continue
            if kernel_id in notebook_kernel_ids:
                continue
            if kernel.get("execution_state") == "idle":
                return kernel_id
        return None

    def _get_sessions(self) -> List[dict]:
        try:
            response = self.client.session.get(
                f"{self.client.base_url}/api/sessions",
                timeout=10,
            )
            response.raise_for_status()
            return response.json() or []
        except Exception:
            return []

    def _is_kernel_alive(self, kernel_id: str) -> bool:
        try:
            response = self.client.session.get(
                f"{self.client.base_url}/api/kernels/{kernel_id}",
                timeout=5,
            )
            if response.status_code != 200:
                return False
            return response.json().get("execution_state") != "dead"
        except Exception:
            return False

    def _create_kernel(self) -> str:
        session = self.client.request_session() if hasattr(self.client, "request_session") else self.client.session
        name = "python3"
        try:
            response = session.get(
                f"{self.client.base_url}/api/kernelspecs",
                timeout=10,
            )
            if response.status_code == 200:
                specs = response.json() or {}
                name = specs.get("default") or "python3"
        except Exception:
            pass

        response = requests.post(
            f"{self.client.base_url}/api/kernels",
            json={"name": name},
            timeout=KERNEL_CREATE_TIMEOUT,
            headers={"Authorization": f"token {self.client.token}"},
        )
        response.raise_for_status()
        created = response.json() or {}
        kernel_id = created.get("id")
        if not kernel_id:
            raise RuntimeError("Could not create dedicated Kaggle kernel.")
        return kernel_id

    def shutdown(self):
        """Shut down the managed kernel if we own it."""
        if self._kernel_owner and self._kernel_id:
            try:
                session = self.client.request_session() if hasattr(self.client, "request_session") else self.client.session
                session.delete(
                    f"{self.client.base_url}/api/kernels/{self._kernel_id}",
                    timeout=KERNEL_SHUTDOWN_TIMEOUT,
                )
            except Exception:
                pass
            self._kernel_id = None
            self._kernel_owner = False