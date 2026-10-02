import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests
import websocket

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ============================================================
# Configuration
# ============================================================

REMOTE_ROOT = "local-project"

MAX_FILE_SIZE_MB = int(
    os.getenv("KAGGLE_SYNC_MAX_FILE_MB", "2048")
)
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# Increase this for slow connections or large repositories. Override with
# KAGGLE_SYNC_REQUEST_TIMEOUT without modifying this file.
REQUEST_TIMEOUT = int(
    os.getenv("KAGGLE_SYNC_REQUEST_TIMEOUT", "300")
)

SYNC_WORKERS = int(
    os.getenv("KAGGLE_SYNC_WORKERS", "4")
)

REMOTE_SYNC_INTERVAL = float(
    os.getenv("KAGGLE_REMOTE_SYNC_INTERVAL", "2")
)

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
# Jupyter / Kaggle client
# ============================================================

class JupyterClient:

    def __init__(self, server_url):

        self.original_url = server_url.rstrip("/")

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

        self.session_id = parts[1]
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


    def upload_file(self, local_path, remote_path):

        local_path = Path(local_path)

        if not local_path.exists():
            return

        size = local_path.stat().st_size

        if size > MAX_FILE_SIZE:

            print(
                f"[SKIP] {local_path} "
                f"({size / 1024 / 1024:.1f} MB > "
                f"{MAX_FILE_SIZE / 1024 / 1024:.0f} MB)"
            )

            return

        parent = os.path.dirname(remote_path)

        if parent:
            self.ensure_directory(parent)

        with open(local_path, "rb") as f:

            content = base64.b64encode(
                f.read()
            ).decode("ascii")

        payload = {
            "type": "file",
            "format": "base64",
            "content": content,
        }

        response = self.request_session().put(
            self.api_url(remote_path),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()


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

        response = self.session.delete(
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


    # --------------------------------------------------------
    # Find Python kernel
    # --------------------------------------------------------

    def find_kernel(self):

        sessions = self.get_sessions()

        if not sessions:
            return None

        # Prefer a Python notebook session.
        for session in sessions:

            kernel = session.get("kernel", {})

            name = (
                session.get("kernel", {})
                .get("name", "")
                .lower()
            )

            path = session.get("path", "")

            if (
                "python" in name
                or path.endswith(".ipynb")
            ):
                return kernel.get("id")

        return sessions[0].get(
            "kernel",
            {}
        ).get("id")


    # --------------------------------------------------------
    # Execute Python in remote kernel
    # --------------------------------------------------------

    def execute(self, code, timeout=600):

        kernel_id = self.find_kernel()

        if not kernel_id:

            raise RuntimeError(
                "\nNo active Kaggle Python kernel found.\n\n"
                "Open your local notebook and make sure "
                "the Kaggle kernel is connected before "
                "automatic dependency installation."
            )

        session_id = uuid.uuid4().hex

        ws_url = (
            f"{self.websocket_base}"
            f"/api/kernels/{kernel_id}/channels"
            f"?session_id={session_id}"
        )

        headers = [
            f"Authorization: token {self.token}"
        ]

        ws = websocket.create_connection(
            ws_url,
            header=headers,
            timeout=REQUEST_TIMEOUT,
            origin=self.base_url,
        )

        msg_id = uuid.uuid4().hex

        message = {
            "header": {
                "msg_id": msg_id,
                "username": "kaggle-runner",
                "session": session_id,
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": False,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }

        ws.send(
            json.dumps(message)
        )

        deadline = time.time() + timeout

        outputs = []

        try:

            while time.time() < deadline:

                raw = ws.recv()

                if not raw:
                    continue

                data = json.loads(raw)

                parent = data.get(
                    "parent_header",
                    {}
                )

                if parent.get("msg_id") != msg_id:
                    continue

                msg_type = (
                    data.get("header", {})
                    .get("msg_type")
                )

                content = data.get(
                    "content",
                    {}
                )

                if msg_type == "stream":

                    text = content.get(
                        "text",
                        ""
                    )

                    outputs.append(text)

                    print(
                        "[KAGGLE]",
                        text,
                        end=""
                    )

                elif msg_type == "error":

                    traceback = content.get(
                        "traceback",
                        []
                    )

                    raise RuntimeError(
                        "\n".join(traceback)
                    )

                elif msg_type in (
                    "execute_reply",
                ):

                    status = content.get(
                        "status"
                    )

                    if status == "error":

                        raise RuntimeError(
                            str(content)
                        )

                    break

        finally:

            ws.close()

        return "".join(outputs)


# ============================================================
# Helpers
# ============================================================

def should_skip(path):

    path = Path(path)

    if path.name in EXCLUDED_FILES:
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

    with open(path, "rb") as f:

        while True:

            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def file_signature(path):

    stat = Path(path).stat()

    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def load_sync_manifest(project_root):

    state_file = Path(project_root) / ".kaggle-sync-state.json"

    if not state_file.exists():
        return {}

    try:

        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        return state.get("files", {})

    except (OSError, ValueError):
        return {}


def save_sync_manifest(project_root, manifest):

    state_file = Path(project_root) / ".kaggle-sync-state.json"
    temporary_file = state_file.with_suffix(".tmp")
    state = {}

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}

    state["files"] = manifest

    with open(temporary_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temporary_file, state_file)


def load_remote_manifest(project_root):

    state_file = Path(project_root) / ".kaggle-sync-state.json"

    if not state_file.exists():
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f).get("remote", {})
    except (OSError, ValueError):
        return {}


def save_remote_manifest(project_root, manifest):

    state_file = Path(project_root) / ".kaggle-sync-state.json"
    state = {}

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}

    state["remote"] = manifest
    temporary_file = state_file.with_suffix(".tmp")

    with open(temporary_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temporary_file, state_file)


def find_requirements(project_root):

    project_root = Path(project_root)

    candidates = []

    # Project-level
    candidates.extend(
        project_root.glob("requirements*.txt")
    )

    # Anywhere below project
    candidates.extend(
        project_root.rglob("requirements*.txt")
    )

    result = []

    seen = set()

    for path in candidates:

        path = path.resolve()

        if path in seen:
            continue

        if should_skip(path):
            continue

        seen.add(path)

        result.append(path)

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

        self.state_file = (
            self.project_root
            / ".kaggle-sync-state.json"
        )

        self.state = self.load_state()


    def load_state(self):

        if not self.state_file.exists():
            return {
                "requirements": {}
            }

        try:

            with open(
                self.state_file,
                "r",
                encoding="utf-8",
            ) as f:

                return json.load(f)

        except Exception:

            return {
                "requirements": {}
            }


    def save_state(self):

        with open(
            self.state_file,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                self.state,
                f,
                indent=2,
            )


    def install_requirements(
        self,
        requirements_file,
    ):

        requirements_file = Path(
            requirements_file
        ).resolve()

        relative = requirements_file.relative_to(
            self.project_root
        )

        key = str(relative)

        digest = sha256_file(
            requirements_file
        )

        old_digest = (
            self.state
            .get("requirements", {})
            .get(key)
        )

        if old_digest == digest:

            return

        print()
        print("=" * 60)
        print("Requirements changed")
        print("=" * 60)
        print()
        print(
            "File:",
            relative
        )
        print(
            "Installing dependencies on Kaggle..."
        )
        print()

        remote_requirements = remote_path_for(
            requirements_file,
            self.project_root,
        )

        remote_requirements = (
            f"/kaggle/working/"
            f"{remote_requirements}"
        )

        code = f"""
import subprocess
import sys
from pathlib import Path

requirements = Path(
    {remote_requirements!r}
)

print("Installing:", requirements)

subprocess.check_call([
    sys.executable,
    "-m",
    "pip",
    "install",
    "-r",
    str(requirements),
])

print("DEPENDENCIES_INSTALLED")
"""

        try:

            self.client.execute(
                code,
                timeout=1800,
            )

        except Exception as e:

            print()
            print(
                "Dependency installation failed:"
            )
            print(e)
            print()

            return

        self.state.setdefault(
            "requirements",
            {}
        )[key] = digest

        self.save_state()

        print()
        print(
            "Dependencies installed successfully."
        )
        print()


    def check(self):

        files = find_requirements(
            self.project_root
        )

        for requirements_file in files:

            self.install_requirements(
                requirements_file
            )


# ============================================================
# Project synchronization
# ============================================================

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

    for path in project_root.rglob("*"):

        if not path.is_file():
            continue

        if should_skip(path):
            continue

        relative = str(path.relative_to(project_root))
        signature = file_signature(path)

        if manifest.get(relative) == signature:
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

        client.upload_file(
            path,
            remote,
        )

        return relative, signature

    with ThreadPoolExecutor(
        max_workers=SYNC_WORKERS
    ) as executor:

        futures = [
            executor.submit(sync_one, item)
            for item in files
        ]

        synced = []

        for future in as_completed(futures):
            synced.append(future.result())

    for relative, signature in synced:
        manifest[relative] = signature

    save_sync_manifest(project_root, manifest)

    count = len(synced)

    print()
    print(
        f"Initial synchronization complete: "
        f"{count} files"
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

        self.last_event = {}


    def sync_file(self, filename):

        path = Path(filename).resolve()

        if not path.exists():
            return

        if not path.is_file():
            return

        if should_skip(path):
            return

        now = time.time()

        previous = self.last_event.get(
            str(path),
            0,
        )

        if now - previous < 0.5:
            return

        self.last_event[str(path)] = now

        try:

            remote = remote_path_for(
                path,
                self.project_root,
            )

            print(
                "[SYNC]",
                path.relative_to(
                    self.project_root
                ),
            )

            self.client.upload_file(
                path,
                remote,
            )

            manifest = load_sync_manifest(self.project_root)
            manifest[str(path.relative_to(self.project_root))] = file_signature(path)
            save_sync_manifest(self.project_root, manifest)

            if (
                path.name.startswith(
                    "requirements"
                )
                and path.suffix == ".txt"
            ):

                self.dependencies.install_requirements(
                    path
                )

        except Exception as e:

            print(
                "[ERROR]",
                path,
                e,
            )


def remote_sync_loop(client, project_root, stop_event):

    project_root = Path(project_root).resolve()
    remote_manifest = load_remote_manifest(project_root)

    while not stop_event.wait(REMOTE_SYNC_INTERVAL):

        try:
            local_manifest = load_sync_manifest(project_root)
            remote_files = client.list_files(REMOTE_ROOT)
            current_remote = {}

            for entry in remote_files:

                remote_path = entry.get("path", "").replace("\\", "/")

                if not remote_path.startswith(f"{REMOTE_ROOT}/"):
                    continue

                relative = remote_path[len(REMOTE_ROOT) + 1:]

                if should_skip(Path(relative)):
                    continue

                if should_skip_remote(relative):
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

                    if synced_local_signature != current_local_signature:
                        print(
                            "[CONFLICT] Local file changed; keeping local:",
                            relative,
                        )
                        continue

                print("[DOWNLOAD]", relative)
                client.download_file(remote_path, local_path)

                local_manifest[relative] = file_signature(local_path)

            save_sync_manifest(project_root, local_manifest)
            save_remote_manifest(project_root, current_remote)
            remote_manifest = current_remote

        except Exception as e:
            print("[REMOTE SYNC ERROR]", e)


    def on_created(self, event):

        if not event.is_directory:

            self.sync_file(
                event.src_path
            )


    def on_modified(self, event):

        if not event.is_directory:

            self.sync_file(
                event.src_path
            )


    def on_moved(self, event):

        if event.is_directory:
            return

        try:

            old_remote = remote_path_for(
                event.src_path,
                self.project_root,
            )

            self.client.delete(
                old_remote
            )

        except Exception:
            pass

        self.sync_file(
            event.dest_path
        )


    def on_deleted(self, event):

        if event.is_directory:
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

            self.client.delete(
                remote
            )

        except Exception as e:

            print(
                "[ERROR]",
                e
            )


# ============================================================
# Main
# ============================================================

def main():

    if len(sys.argv) != 3:

        print(
            "Usage:\n"
            "  python sync.py "
            "<project-folder> "
            "<kaggle-vscode-url>"
        )

        sys.exit(1)

    project_root = Path(
        sys.argv[1]
    ).resolve()

    server_url = sys.argv[2]

    if not project_root.exists():

        raise RuntimeError(
            f"Project does not exist:\n"
            f"{project_root}"
        )

    print()
    print(
        "Connecting to Kaggle Jupyter Server..."
    )

    client = JupyterClient(
        server_url
    )

    # Verify API
    response = client.session.get(
        f"{client.base_url}/api",
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    print(
        "Kaggle Jupyter Server: OK"
    )

    # --------------------------------------------------------
    # Initial project upload
    # --------------------------------------------------------

    initial_sync(
        client,
        project_root,
    )

    # --------------------------------------------------------
    # Dependencies
    # --------------------------------------------------------

    dependency_manager = (
        DependencyManager(
            client,
            project_root,
        )
    )

    try:

        dependency_manager.check()

    except Exception as e:

        print(
            "Initial dependency check failed:"
        )

        print(e)

    # --------------------------------------------------------
    # Watcher
    # --------------------------------------------------------

    handler = SyncHandler(
        client,
        project_root,
        dependency_manager,
    )

    remote_stop_event = threading.Event()
    remote_thread = threading.Thread(
        target=remote_sync_loop,
        args=(client, project_root, remote_stop_event),
        name="kaggle-remote-sync",
        daemon=True,
    )
    remote_thread.start()

    observer = Observer()

    observer.schedule(
        handler,
        str(project_root),
        recursive=True,
    )

    observer.start()

    print()
    print("=" * 60)
    print(
        "KAGGLE AUTOMATIC SYNCHRONIZATION IS RUNNING"
    )
    print("=" * 60)
    print()
    print(
        "Local project:"
    )
    print(project_root)
    print()
    print(
        "Remote project:"
    )
    print(
        f"/kaggle/working/{REMOTE_ROOT}"
    )
    print()
    print(
        "Requirements are installed only when "
        "their contents change."
    )
    print()
    print(
            "Kaggle-created files are checked every "
            f"{REMOTE_SYNC_INTERVAL:g} seconds."
    )
    print()
    print(
        "Press Ctrl+C to stop."
    )
    print()

    try:

        while True:

            time.sleep(1)

    except KeyboardInterrupt:

        print()
        print(
            "Stopping synchronization..."
        )

        remote_stop_event.set()
        observer.stop()

    observer.join()


if __name__ == "__main__":
    main()