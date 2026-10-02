import sys
import json
import uuid
import time
import base64
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests
import websocket


REMOTE_ROOT = "/kaggle/working/local-project"


class KaggleClient:
    def __init__(self, server_url):
        self.original_url = server_url.rstrip("/")

        parsed = urlsplit(self.original_url)

        parts = [p for p in parsed.path.split("/") if p]

        # Expected:
        # /k/<session>/<token>/proxy
        if (
            len(parts) != 4
            or parts[0] != "k"
            or parts[3] != "proxy"
        ):
            raise RuntimeError(
                "\nInvalid Kaggle Jupyter URL.\n\n"
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

    def api(self, path=""):
        path = path.strip("/")

        if path:
            encoded = "/".join(
                quote(part, safe="")
                for part in path.split("/")
            )
            return f"{self.base_url}/api/{encoded}"

        return f"{self.base_url}/api"

    def check(self):
        response = self.session.get(
            self.api(""),
            timeout=15
        )

        response.raise_for_status()
        return response.json()

    def upload_file(self, local_path, remote_path):
        local_path = Path(local_path)
        remote_parts = Path(remote_path).parts

        for index in range(1, len(remote_parts)):
            directory = "/".join(remote_parts[:index])
            response = self.session.put(
                self.api(f"contents/{directory}"),
                json={"type": "directory"},
                timeout=15,
            )
            if response.status_code not in (200, 201, 409):
                response.raise_for_status()

        content = base64.b64encode(
            local_path.read_bytes()
        ).decode("ascii")
        response = self.session.put(
            self.api(f"contents/{remote_path}"),
            json={
                "type": "file",
                "format": "base64",
                "content": content,
            },
            timeout=30,
        )
        response.raise_for_status()

    def find_kernel(self):
        response = self.session.get(
            self.api("sessions"),
            timeout=15
        )

        response.raise_for_status()

        sessions = response.json()

        # Prefer Python kernel
        for session in sessions:
            kernel = session.get("kernel", {})
            name = kernel.get("name", "").lower()

            if "python" in name:
                return kernel.get("id")

            path = session.get("path", "")

            if path.endswith(".ipynb"):
                return kernel.get("id")

        # Fallback
        if sessions:
            return sessions[0].get(
                "kernel",
                {}
            ).get("id")

        return None

    def execute(self, code, timeout=3600):
        kernel_id = self.find_kernel()

        if not kernel_id:
            raise RuntimeError(
                "\nNo active Kaggle Python kernel found.\n\n"
                "Open/connect your Kaggle notebook kernel first."
            )

        session_id = uuid.uuid4().hex

        ws_url = (
            f"{self.websocket_base}"
            f"/api/kernels/{kernel_id}/channels"
            f"?session_id={session_id}"
        )

        ws = websocket.create_connection(
            ws_url,
            header=[
                f"Authorization: token {self.token}"
            ],
            timeout=30,
            origin=self.base_url
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

        ws.send(json.dumps(message))

        deadline = time.time() + timeout
        had_error = False

        try:
            while time.time() < deadline:

                raw = ws.recv()

                if not raw:
                    continue

                msg = json.loads(raw)

                msg_type = msg.get("msg_type")
                parent = msg.get("parent_header", {})

                # Ignore unrelated messages
                if parent.get("msg_id") != msg_id:
                    continue

                content = msg.get("content", {})

                if msg_type == "stream":
                    text = content.get("text", "")
                    print(text, end="", flush=True)

                elif msg_type == "execute_result":
                    data = content.get("data", {})

                    if "text/plain" in data:
                        print(
                            data["text/plain"],
                            flush=True
                        )

                elif msg_type == "display_data":
                    data = content.get("data", {})

                    if "text/plain" in data:
                        print(
                            data["text/plain"],
                            flush=True
                        )

                elif msg_type == "error":
                    had_error = True

                    traceback = content.get(
                        "traceback",
                        []
                    )

                    print(
                        "\n".join(traceback),
                        flush=True
                    )

                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        break

        finally:
            ws.close()

        if had_error:
            return False

        return True


def find_project_root(script_path):
    script = Path(script_path).resolve()

    current = script.parent

    while True:
        # We assume the current working directory is the
        # local project root when kaggle-run is called.
        break

    return Path.cwd()


def main():

    if len(sys.argv) != 2:
        print()
        print("Usage:")
        print("  kaggle-run train.py")
        print("  kaggle-run LAB_1\\train.py")
        print()
        sys.exit(1)

    script_argument = sys.argv[1]

    local_script = Path(script_argument).resolve()

    if not local_script.exists():
        print()
        print(f"ERROR: File not found:")
        print(f"  {local_script}")
        print()
        sys.exit(1)

    if local_script.suffix.lower() != ".py":
        print()
        print("ERROR: kaggle-run only runs .py files.")
        print()
        sys.exit(1)

    project_root = Path.cwd().resolve()

    try:
        relative_path = local_script.relative_to(
            project_root
        )
    except ValueError:
        print()
        print("ERROR:")
        print("The Python file must be inside the")
        print("current project directory.")
        print()
        print(f"Project : {project_root}")
        print(f"File    : {local_script}")
        print()
        sys.exit(1)

    remote_script = (
        f"{REMOTE_ROOT}/"
        + str(relative_path).replace("\\", "/")
    )

    # kaggle-sync stores the active URL here
    config_file = (
        Path.home()
        / ".kaggle-runner-url"
    )

    if not config_file.exists():
        print()
        print("ERROR: Kaggle sync is not running.")
        print()
        print("Start it first:")
        print()
        print('  kaggle-sync "KAGGLE_VSCODE_URL"')
        print()
        sys.exit(1)

    server_url = config_file.read_text(
        encoding="utf-8"
    ).strip()

    if not server_url:
        print()
        print("ERROR: Kaggle URL is empty.")
        print()
        sys.exit(1)

    print()
    print("=" * 60)
    print("KAGGLE PYTHON RUNNER")
    print("=" * 60)
    print()
    print(f"Local : {local_script}")
    print(f"Remote: {remote_script}")
    print()

    client = KaggleClient(server_url)

    print("Connecting to Kaggle...")

    try:
        client.check()
    except Exception as e:
        print()
        print("ERROR: Could not connect to Kaggle.")
        print(e)
        print()
        sys.exit(1)

    print("Kaggle Jupyter Server: OK")

    print(f"Ensuring remote script: {remote_script}")
    client.upload_file(local_script, remote_script)

    # IMPORTANT:
    # The synchronized file already exists remotely.
    #
    # We use runpy.run_path() instead of %run so that the
    # script behaves like a normal Python script.
    code = f"""
import os
import sys
import runpy

os.chdir({REMOTE_ROOT!r})

if {REMOTE_ROOT!r} not in sys.path:
    sys.path.insert(0, {REMOTE_ROOT!r})

print("============================================================")
print("RUNNING LOCAL PROJECT ON KAGGLE")
print("============================================================")
print("Script : {remote_script}")
print("CWD    :", os.getcwd())

try:
    import torch

    print("CUDA   :", torch.cuda.is_available())
    print("GPUs   :", torch.cuda.device_count())

    for i in range(torch.cuda.device_count()):
        print("GPU", i, ":", torch.cuda.get_device_name(i))

except Exception:
    pass

print("============================================================")
print()

runpy.run_path(
    {remote_script!r},
    run_name="__main__"
)
"""

    print("Executing on Kaggle...")
    print()

    success = client.execute(code)

    print()
    print("=" * 60)

    if success:
        print("KAGGLE RUN FINISHED")
    else:
        print("KAGGLE RUN FAILED")

    print("=" * 60)
    print()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()