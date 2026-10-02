"""kaggle-run: Run local Python scripts on Kaggle GPU.

Uploads local scripts to Kaggle and executes them in a fresh subprocess
inside the Jupyter environment. Streams outputs, supports Ctrl+C interruption,
and performs a one-shot sync if kaggle-sync is not running.
"""

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests

from . import session_guard, sync, urlstore
from . import __version__
from .kernel_exec import execute_in_kernel


REMOTE_ROOT = "/kaggle/working/local-project"
REMOTE_CONTENTS_PREFIX = "local-project"
DRAIN_TIMEOUT = 20


class KaggleClient:
    def __init__(self, server_url):
        parsed = urlsplit(server_url)
        parts = [p for p in parsed.path.split("/") if p]

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
        self._last_kernel_id = None

    def api(self, path=""):
        path = path.strip("/")
        if path:
            encoded = "/".join(quote(part, safe="") for part in path.split("/"))
            return f"{self.base_url}/api/{encoded}"
        return f"{self.base_url}/api"

    def check(self):
        response = self.session.get(self.api(""), timeout=15)
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

        content = base64.b64encode(local_path.read_bytes()).decode("ascii")
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
        response = self.session.get(self.api("sessions"), timeout=15)
        response.raise_for_status()
        sessions = response.json() or []

        for s in sessions:
            kernel = s.get("kernel", {})
            name = kernel.get("name", "").lower()
            if "python" in name or s.get("path", "").endswith(".ipynb"):
                return kernel.get("id")

        if sessions:
            return sessions[0].get("kernel", {}).get("id")

        # Fallback to kernel list
        k_resp = self.session.get(self.api("kernels"), timeout=15)
        if k_resp.status_code == 200:
            kernels = k_resp.json() or []
            if kernels:
                return kernels[0].get("id")

        return None

    def interrupt_kernel(self, kernel_id=None):
        kernel_id = (
            kernel_id
            or self._last_kernel_id
            or self.find_kernel()
        )
        if not kernel_id:
            return False

        response = self.session.post(
            f"{self.base_url}/api/kernels/{kernel_id}/interrupt",
            timeout=15,
        )
        response.raise_for_status()
        return True

    def execute(self, code, timeout=None, user_expressions=None):
        kernel_id = self.find_kernel()
        if not kernel_id:
            raise RuntimeError(
                "\nNo active Kaggle Python kernel found.\n\n"
                "Open your Kaggle notebook or connect to the kernel first."
            )

        self._last_kernel_id = kernel_id

        def on_text(text):
            print(text, end="", flush=True)

        return execute_in_kernel(
            self.websocket_base,
            self.base_url,
            self.token,
            kernel_id,
            code,
            on_text=on_text,
            user_expressions=user_expressions,
            timeout=timeout,
        )


def build_remote_code(remote_script, remote_cwd, script_args):
    """Build the kernel cell that runs the script in a FRESH subprocess."""
    lines = [
        "import codecs as _codecs",
        "import os as _os",
        "import signal as _sig",
        "import subprocess as _sp",
        "import sys as _sys",
        "",
        f"_REMOTE_SCRIPT = {json.dumps(remote_script)}",
        f"_REMOTE_CWD = {json.dumps(remote_cwd)}",
        f"_SCRIPT_ARGS = {json.dumps(list(script_args))}",
        "_KAGGLE_RUN_RC = 1",
        "_proc = None",
        "try:",
        '    print("=" * 60)',
        '    print("RUNNING LOCAL PROJECT ON KAGGLE")',
        '    print("=" * 60)',
        '    print("Script :", _REMOTE_SCRIPT)',
        "    try:",
        '        _smi = _sp.run(',
        '            ["nvidia-smi",',
        '             "--query-gpu=name,memory.total",',
        '             "--format=csv,noheader"],',
        "            capture_output=True,",
        "            text=True,",
        "            timeout=30,",
        "        )",
        '        _smi_out = (_smi.stdout or "").strip()',
        '        print("GPU(s) :", _smi_out if _smi_out else "nvidia-smi returned no output")',
        "    except Exception as _e:",
        '        print("GPU(s) : nvidia-smi unavailable:", _e)',
        '    print("=" * 60)',
        "    _sys.stdout.flush()",
        "    _env = dict(_os.environ)",
        '    _env["PYTHONPATH"] = _REMOTE_CWD + (_os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")',
        '    _env["PYTHONUNBUFFERED"] = "1"',
        '    _env["PYTHONIOENCODING"] = "utf-8"',
        "    _cmd = [_sys.executable, '-u', _REMOTE_SCRIPT] + list(_SCRIPT_ARGS)",
        '    print("CMD    :", " ".join(_cmd))',
        '    print("CWD    :", _REMOTE_CWD)',
        "    _sys.stdout.flush()",
        "    _proc = _sp.Popen(",
        "        _cmd,",
        "        cwd=_REMOTE_CWD,",
        "        env=_env,",
        "        stdout=_sp.PIPE,",
        "        stderr=_sp.STDOUT,",
        "        start_new_session=True,",
        "    )",
        '    _decoder = _codecs.getincrementaldecoder("utf-8")(errors="replace")',
        "    _interrupted = False",
        "    try:",
        "        while True:",
        "            _chunk = _proc.stdout.read1(65536)",
        "            if not _chunk:",
        "                break",
        "            _sys.stdout.write(_decoder.decode(_chunk))",
        "            _sys.stdout.flush()",
        "    except KeyboardInterrupt:",
        "        _interrupted = True",
        "        print()",
        '        print("[INTERRUPTED] stopping remote process group...")',
        "        raise",
        "    finally:",
        "        try:",
        "            if _proc.poll() is None and not _interrupted:",
        "                try:",
        "                    _proc.wait(timeout=60)",
        "                except Exception:",
        "                    pass",
        "            if _proc.poll() is None:",
        "                _kill = getattr(_os, 'killpg', None)",
        "                if _kill is not None:",
        "                    _kill(_proc.pid, _sig.SIGTERM)",
        "                else:",
        "                    _proc.terminate()",
        "                try:",
        "                    _proc.wait(timeout=5)",
        "                except Exception:",
        "                    if _kill is not None:",
        "                        _kill(_proc.pid, _sig.SIGKILL)",
        "                    else:",
        "                        _proc.kill()",
        "                    _proc.wait(timeout=5)",
        "        except Exception:",
        "            pass",
        "        try:",
        "            _tail = _proc.stdout.read()",
        "            if _tail:",
        "                _sys.stdout.write(_decoder.decode(_tail, True))",
        "                _sys.stdout.flush()",
        "        except Exception:",
        "            pass",
        "    _KAGGLE_RUN_RC = _proc.returncode if _proc.returncode is not None else 1",
        "except KeyboardInterrupt:",
        "    _KAGGLE_RUN_RC = 130",
        "except SystemExit as _se:",
        "    _KAGGLE_RUN_RC = _se.code if isinstance(_se.code, int) else 1",
        "except Exception:",
        "    import traceback as _tb",
        "    _tb.print_exc()",
        "    _KAGGLE_RUN_RC = 1",
    ]
    return "\n".join(lines) + "\n"


def parse_remote_rc(result):
    try:
        expressions = result.get("user_expressions", {}) or {}
        node = expressions.get("rc", {}) or {}
        data = node.get("data", {}) or {}
        text = data.get("text/plain", "")
        if isinstance(text, list):
            text = "".join(text)
        text = str(text).strip().strip("'\"")
        return int(text)
    except Exception:
        return None


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="kaggle-run",
        description=(
            "Run a local Python script on Kaggle in a fresh subprocess. "
            "Extra arguments are passed through to the script."
        ),
    )
    parser.add_argument(
        "script",
        nargs="?",
        help="Local .py file inside the project directory.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip one-shot preflight sync before running.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the script.",
    )

    if not argv:
        parser.print_usage()
        sys.exit(session_guard.EXIT_USAGE)

    namespace = parser.parse_args(argv)

    if not namespace.script:
        parser.print_usage()
        sys.exit(session_guard.EXIT_USAGE)

    script_args = list(namespace.args or [])
    if script_args[:1] == ["--"]:
        script_args = script_args[1:]

    local_script = Path(namespace.script).resolve()

    if not local_script.exists():
        print()
        print(f"ERROR: File not found: {local_script}")
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    if local_script.suffix.lower() != ".py":
        print()
        print("ERROR: kaggle-run only runs .py files.")
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    project_root = Path.cwd().resolve()
    try:
        relative_path = local_script.relative_to(project_root)
    except ValueError:
        print()
        print("ERROR: The Python file must be inside the current project directory.")
        print(f"Project : {project_root}")
        print(f"File    : {local_script}")
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    relative_posix = str(relative_path).replace("\\", "/")
    contents_path = f"{REMOTE_CONTENTS_PREFIX}/{relative_posix}"
    remote_script = f"{REMOTE_ROOT}/{relative_posix}"

    server_url = urlstore.load_url(project_root)
    if not server_url:
        print()
        print("ERROR: No Kaggle URL saved for this project.")
        print("Run kaggle-sync from this project folder first:")
        print('  kaggle-sync "KAGGLE_VSCODE_URL"')
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    client = KaggleClient(server_url)
    session_state = session_guard.probe(client)
    if session_state == "expired":
        urlstore.delete_url(project_root)
        session_guard.log_session_expired()
        sys.exit(session_guard.EXIT_EXPIRED)
    if session_state == "offline":
        print("[OFFLINE] Kaggle server is unreachable.")
        sys.exit(session_guard.EXIT_OFFLINE)

    print()
    print("=" * 60)
    print("KAGGLE PYTHON RUNNER")
    print("=" * 60)
    print()
    print(f"Local : {local_script}")
    print(f"Remote: {remote_script}")
    print()

    print("Connecting to Kaggle...")
    try:
        session_guard.guarded_call(client, client.check)
    except session_guard.SessionExpired:
        urlstore.delete_url(project_root)
        sys.exit(session_guard.EXIT_EXPIRED)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            session_guard.log_session_expired()
            urlstore.delete_url(project_root)
            sys.exit(session_guard.EXIT_EXPIRED)
        print()
        print("ERROR: Could not connect to Kaggle.")
        print(e)
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    print("Kaggle Jupyter Server: OK")

    # Preflight sync check
    sync_status = urlstore.sync_state(project_root)
    if sync_status != "running" and not namespace.no_sync:
        try:
            synced_count = sync.sync_once(client, project_root)
            print(
                f"kaggle-sync is not running: synced {synced_count} changed file(s) "
                "before the run (live sync and notebook helpers are off)"
            )
        except session_guard.SessionExpired:
            urlstore.delete_url(project_root)
            sys.exit(session_guard.EXIT_EXPIRED)
        except Exception as e:
            print(f"[PREFLIGHT SYNC WARNING] {e}")

    # Ensure script itself is uploaded
    print(f"Ensuring remote script: {remote_script}")
    try:
        session_guard.guarded_call(client, client.upload_file, local_script, contents_path)
    except session_guard.SessionExpired:
        urlstore.delete_url(project_root)
        sys.exit(session_guard.EXIT_EXPIRED)

    code = build_remote_code(remote_script, REMOTE_ROOT, script_args)

    print("Executing on Kaggle...")
    print()

    try:
        result = session_guard.guarded_call(
            client,
            client.execute,
            code,
            user_expressions={"rc": "_KAGGLE_RUN_RC"},
        )
    except KeyboardInterrupt:
        print()
        print("Interrupting remote job...")
        try:
            client.interrupt_kernel()
        except Exception as e:
            print("Interrupt request failed:", e)

        try:
            session_guard.guarded_call(
                client,
                lambda: client.execute("pass", timeout=DRAIN_TIMEOUT),
            )
        except KeyboardInterrupt:
            print("Warning: exiting now; the remote job may still be running.")
            sys.exit(130)
        except Exception:
            pass

        print("Remote job stopped")
        sys.exit(130)
    except session_guard.SessionExpired:
        urlstore.delete_url(project_root)
        sys.exit(session_guard.EXIT_EXPIRED)

    status = (result or {}).get("status")
    exit_code = parse_remote_rc(result or {})
    if exit_code is None:
        exit_code = 0 if status == "ok" else 1

    print()
    print("=" * 60)
    if status == "ok" and exit_code == 0:
        print("KAGGLE RUN FINISHED")
    else:
        print(f"KAGGLE RUN FAILED (exit code {exit_code})")
    print("=" * 60)
    print()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()