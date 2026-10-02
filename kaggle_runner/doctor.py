"""Diagnostic health checker for kaggle-sync and kaggle-run.

Performs local environment verification and optional remote end-to-end
testing against the Kaggle Jupyter server.
"""

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import ignore_rules, runner_paths, session_guard, urlstore
from .kernel_exec import execute_in_kernel


KAGGLE_PROXY_URL_RE = re.compile(
    r"https?://[^\s\"']*kaggle\.net/k/[^/\s]+/[^/\s]+/proxy"
)
KAGGLE_KEY_RE = re.compile(
    r"""(?:KAGGLE_KEY|kaggle\.json|KG_KEY)["']?\s*[:=]\s*["']?([a-zA-Z0-9_\-]{16,})"""
)


class DoctorResult:
    def __init__(self, name: str, status: str, message: str, hint: str = ""):
        self.name = name
        self.status = status  # PASS, WARN, FAIL
        self.message = session_guard.redact(message)
        self.hint = session_guard.redact(hint)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "hint": self.hint,
        }

    def format_line(self) -> str:
        line = f"[{self.status}] {self.name}: {self.message}"
        if self.status != "PASS" and self.hint:
            line += f" -> {self.hint}"
        return line


def check_python_os() -> DoctorResult:
    ver = sys.version_info
    platform = sys.platform
    msg = f"Python {ver.major}.{ver.minor}.{ver.micro} on {platform}"
    if ver < (3, 9):
        return DoctorResult("Python/OS", "FAIL", msg, "Upgrade to Python >= 3.9")
    return DoctorResult("Python/OS", "PASS", msg)


def check_dependencies() -> DoctorResult:
    missing = []
    versions = []
    for pkg in ("requests", "websocket", "watchdog", "pathspec"):
        try:
            mod = __import__(pkg)
            v = getattr(mod, "__version__", "installed")
            versions.append(f"{pkg}=={v}")
        except ImportError:
            missing.append(pkg)

    if missing:
        return DoctorResult(
            "Dependencies",
            "FAIL",
            f"Missing dependencies: {', '.join(missing)}",
            "Run pip install -e . to install dependencies",
        )
    return DoctorResult("Dependencies", "PASS", ", ".join(versions))


def check_console_encoding() -> DoctorResult:
    enc = sys.stdout.encoding or "unknown"
    if "utf" in enc.lower():
        return DoctorResult("Console Encoding", "PASS", f"Encoding is {enc}")
    return DoctorResult(
        "Console Encoding",
        "WARN",
        f"Console encoding is {enc}",
        "Set PYTHONIOENCODING=utf-8 or enable Windows UTF-8 mode in Region settings",
    )


def check_old_launchers() -> DoctorResult:
    which = shutil.which("kaggle-sync")
    if which:
        which_lower = which.lower()
        is_cmd = which_lower.endswith(".cmd")
        is_shadow_dir = (
            "kaggle-runner" in which_lower
            and "site-packages" not in which_lower
            and "scripts" not in which_lower
            and ".venv" not in which_lower
        )
        if is_cmd or is_shadow_dir:
            return DoctorResult(
                "Old Launchers",
                "WARN",
                f"Found shadowing launcher at {which}",
                "Remove the old folder from PATH, it shadows the new commands",
            )
    return DoctorResult("Old Launchers", "PASS", "No shadowing .cmd launchers found")


def check_project_root(project_root: Path) -> DoctorResult:
    if not project_root.exists():
        return DoctorResult(
            "Project Root",
            "FAIL",
            f"Path does not exist: {project_root}",
            "Specify a valid directory with --project",
        )
    if not project_root.is_dir():
        return DoctorResult(
            "Project Root",
            "FAIL",
            f"Path is not a directory: {project_root}",
            "Specify a directory path",
        )
    return DoctorResult("Project Root", "PASS", str(project_root))


def check_onedrive(project_root: Path) -> DoctorResult:
    path_str = str(project_root.resolve()).lower()
    if "onedrive" in path_str:
        if os.getenv("KAGGLE_SYNC_ALLOW_ONEDRIVE") != "1":
            return DoctorResult(
                "OneDrive Path",
                "WARN",
                "Project folder sits inside OneDrive",
                "Move project outside OneDrive to avoid sync conflicts and placeholder dehydration",
            )
    return DoctorResult("OneDrive Path", "PASS", "Project folder is outside OneDrive")


def check_runner_home_writable() -> DoctorResult:
    home = runner_paths.runner_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe_file = home / f".probe_{os.getpid()}_{int(time.time())}.tmp"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        return DoctorResult("~/.kaggle-runner", "PASS", f"{home} is writable")
    except Exception as e:
        return DoctorResult(
            "~/.kaggle-runner",
            "FAIL",
            f"Directory {home} not writable: {e}",
            "Check filesystem permissions for ~/.kaggle-runner or set KAGGLE_RUNNER_HOME",
        )


def check_url_source_and_age(project_root: Path, url: Optional[str]) -> DoctorResult:
    source = "provided argument" if url else None
    if not url:
        env_url = os.getenv("KAGGLE_RUNNER_URL")
        if env_url:
            url = env_url
            source = "environment variable KAGGLE_RUNNER_URL"
        else:
            url_f = runner_paths.url_file(project_root)
            if url_f.exists():
                source = f"project url file ({url_f.name})"
                url = urlstore.load_url(project_root, check_ttl=False)

    if not url:
        return DoctorResult(
            "URL Configuration",
            "WARN",
            "No Kaggle URL found for this project",
            "Run kaggle-sync with your Kaggle session URL",
        )

    parsed = urlsplit(url)
    parts = [p for p in parsed.path.split("/") if p]
    token_len = len(parts[2]) if len(parts) >= 3 else 0
    host = parsed.netloc

    return DoctorResult(
        "URL Configuration",
        "PASS",
        f"Host: {host}, token length: {token_len}, source: {source}",
    )


def check_url_permissions(project_root: Path) -> DoctorResult:
    u_file = runner_paths.url_file(project_root)
    if not u_file.exists():
        return DoctorResult("URL File Permissions", "PASS", "No URL file exists yet")

    if sys.platform != "win32":
        try:
            mode = u_file.stat().st_mode & 0o777
            if mode not in (0o600, 0o400):
                return DoctorResult(
                    "URL File Permissions",
                    "WARN",
                    f"Permissions are {oct(mode)}",
                    "Run kaggle-sync doctor --fix to tighten permissions to 0600",
                )
        except OSError:
            pass
    return DoctorResult("URL File Permissions", "PASS", "Permissions restricted to current user")


def check_legacy_url_file() -> DoctorResult:
    legacy = Path.home() / ".kaggle-runner-url"
    if legacy.exists():
        return DoctorResult(
            "Legacy URL File",
            "WARN",
            "Found legacy ~/.kaggle-runner-url",
            "Run kaggle-sync doctor --fix to remove legacy URL file",
        )
    return DoctorResult("Legacy URL File", "PASS", "No legacy URL file found")


def check_kagglesyncignore(project_root: Path) -> DoctorResult:
    path = project_root / ignore_rules.IGNORE_FILENAME
    if path.exists():
        return DoctorResult(".kagglesyncignore", "PASS", "Ignore file present")
    return DoctorResult(
        ".kagglesyncignore",
        "WARN",
        "No .kagglesyncignore found in project",
        "Run kaggle-sync doctor --fix to generate default ignore file",
    )


def check_requirements_file(project_root: Path) -> DoctorResult:
    from . import sync
    req_file, notices = sync.select_requirements_file(project_root)
    if req_file:
        message = f"Selected requirements: {req_file.relative_to(project_root)}"
        if notices:
            message += f" ({' '.join(notices)})"
        return DoctorResult(
            "Requirements File",
            "PASS",
            message,
        )
    if notices:
        return DoctorResult("Requirements File", "WARN", " ".join(notices))
    return DoctorResult(
        "Requirements File",
        "PASS",
        "No requirements.txt found (optional)",
    )


def check_cloud_placeholders(project_root: Path) -> DoctorResult:
    from . import sync
    placeholders = []
    for path in sync.iter_local_files(project_root):
        if sync.is_cloud_placeholder(path):
            placeholders.append(path.name)
            if len(placeholders) >= 5:
                break
    if placeholders:
        return DoctorResult(
            "Cloud Placeholders",
            "WARN",
            f"Found cloud-only OneDrive files (e.g. {', '.join(placeholders)})",
            "Right click folder -> 'Always keep on this device'",
        )
    return DoctorResult("Cloud Placeholders", "PASS", "No cloud placeholder files detected")


def check_files_to_sync(project_root: Path) -> DoctorResult:
    from . import sync
    to_sync_count = 0
    to_sync_bytes = 0
    ignored_count = 0
    oversized_count = 0

    for path in sync.iter_local_files(project_root):
        if sync.is_ignored_path(project_root, path):
            ignored_count += 1
            continue
        try:
            sz = path.stat().st_size
        except OSError:
            continue
        if sz > sync.MAX_FILE_SIZE:
            oversized_count += 1
            continue
        to_sync_count += 1
        to_sync_bytes += sz

    mb = to_sync_bytes / (1024 * 1024)
    msg = f"{to_sync_count} files would sync ({mb:.1f} MB), {ignored_count} ignored, {oversized_count} oversized"
    return DoctorResult("Files to Sync", "PASS", msg)


def check_sync_state(project_root: Path) -> DoctorResult:
    state = urlstore.sync_state(project_root)
    if state == "running":
        return DoctorResult("Sync State", "PASS", "kaggle-sync daemon is running")
    elif state == "stale":
        return DoctorResult(
            "Sync State",
            "WARN",
            "kaggle-sync heartbeat is stale",
            "Restart kaggle-sync to resume background synchronization",
        )
    return DoctorResult(
        "Sync State",
        "WARN",
        "kaggle-sync is not running",
        "Start kaggle-sync in a separate terminal for real-time live sync",
    )


def check_secrets_scan(project_root: Path) -> DoctorResult:
    hits = []
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".kaggle-runner"}

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for f in filenames:
            file_path = Path(dirpath) / f
            try:
                rel = file_path.relative_to(project_root).as_posix()
            except ValueError:
                continue

            if ignore_rules.is_ignored(project_root, rel):
                continue

            try:
                if file_path.stat().st_size > 1024 * 1024:
                    continue
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue

            for idx, line in enumerate(content.splitlines(), start=1):
                if KAGGLE_PROXY_URL_RE.search(line) or KAGGLE_KEY_RE.search(line):
                    hits.append(f"{rel}:{idx}")
                    break

    if hits:
        hit_summary = ", ".join(hits[:3])
        hint = "Remove secret tokens from project files"
        if (project_root / ".git").exists():
            hint = "Run 'git rm --cached <file>' and rotate token by starting a new Kaggle session"
        return DoctorResult(
            "Secret Scan",
            "WARN",
            f"Exposed Kaggle secret or URL pattern found in: {hit_summary}",
            hint,
        )
    return DoctorResult("Secret Scan", "PASS", "No exposed tokens found in project files")


def apply_safe_fixes(project_root: Path) -> List[str]:
    """Execute safe local actions."""
    fixed = []
    home = runner_paths.runner_home()
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)
        fixed.append(f"Created {home}")

    ignore_file = project_root / ignore_rules.IGNORE_FILENAME
    if not ignore_file.exists():
        ignore_file.write_text(ignore_rules.DEFAULT_IGNORE_TEXT, encoding="utf-8")
        fixed.append(f"Created {ignore_file.name}")

    u_file = runner_paths.url_file(project_root)
    if u_file.exists():
        if urlstore.restrict_permissions(u_file):
            fixed.append("Tightened URL file permissions")

    legacy = urlstore.cleanup_legacy_url_file()
    if legacy:
        fixed.append(f"Deleted legacy {legacy}")

    return fixed


def run_remote_checks(
    client,
    project_root: Path,
    deep: bool = False,
) -> List[DoctorResult]:
    results = []
    base_url = client.base_url

    # 1. Probe session
    t0 = time.time()
    state = session_guard.probe(client)
    latency_ms = int((time.time() - t0) * 1000)

    if state == "expired":
        results.append(
            DoctorResult(
                "Remote Session",
                "FAIL",
                "Session is expired (401/403)",
                "Restart session on Kaggle and copy the new URL",
            )
        )
        return results
    elif state == "offline":
        results.append(
            DoctorResult(
                "Remote Session",
                "FAIL",
                "Server unreachable or returning 5xx",
                "Check network connection and verify Kaggle notebook is active",
            )
        )
        return results

    # Get Jupyter version
    try:
        resp = client.session.get(f"{base_url}/api", timeout=10)
        jupyter_ver = resp.json().get("version", "unknown")
    except Exception:
        jupyter_ver = "unknown"

    results.append(
        DoctorResult(
            "Remote Session",
            "PASS",
            f"Alive (latency: {latency_ms} ms, Jupyter {jupyter_ver})",
        )
    )

    doctor_id = uuid.uuid4().hex[:8]
    temp_remote_dir = f"local-project/.kaggle-doctor-{doctor_id}"
    probe_name = "probe.bin"
    probe_remote = f"{temp_remote_dir}/{probe_name}"
    ephemeral_kernel_id: Optional[str] = None
    cleanup_ok = True

    try:
        # 2. Contents API round trip
        # Create dir
        client.ensure_directory(temp_remote_dir)

        # Small PUT
        small_payload = b"doctor-probe"
        client.upload_bytes(small_payload, probe_remote)

        # Read back and compare
        read_back = client.download_bytes(probe_remote)
        if read_back != small_payload:
            results.append(
                DoctorResult(
                    "Contents API Round Trip",
                    "FAIL",
                    "Uploaded probe content does not match read content",
                    "Check server filesystem and quota",
                )
            )
        else:
            # Chunked PUT (two tiny chunks, last chunk=-1) + rename
            chunk1 = b"ABCD"
            chunk2 = b"EFGH"
            chunk_remote = f"{temp_remote_dir}/chunked.bin"
            
            # Start chunked upload
            put_url = client.api(f"contents/{chunk_remote}")
            r1 = client.session.put(
                put_url,
                json={
                    "type": "file",
                    "format": "base64",
                    "content": base64.b64encode(chunk1).decode("ascii"),
                    "chunk": 1,
                },
                timeout=15,
            )
            r1.raise_for_status()

            r2 = client.session.put(
                put_url,
                json={
                    "type": "file",
                    "format": "base64",
                    "content": base64.b64encode(chunk2).decode("ascii"),
                    "chunk": -1,
                },
                timeout=15,
            )
            r2.raise_for_status()

            # Raw GET with Range
            files_url = f"{base_url}/files/{probe_remote}"
            range_resp = client.session.get(
                files_url,
                headers={"Range": "bytes=0-3"},
                timeout=15,
            )
            if range_resp.status_code not in (200, 206):
                results.append(
                    DoctorResult(
                        "Contents API Round Trip",
                        "FAIL",
                        f"Range GET returned status {range_resp.status_code}",
                        "Kaggle raw file server issue",
                    )
                )
            else:
                results.append(
                    DoctorResult(
                        "Contents API Round Trip",
                        "PASS",
                        "Directory creation, small PUT, chunked PUT, and Range GET succeeded",
                    )
                )

        # 3. Create ephemeral kernel
        k_resp = client.session.post(
            f"{base_url}/api/kernels",
            json={"name": "python3"},
            timeout=20,
        )
        k_resp.raise_for_status()
        ephemeral_kernel_id = k_resp.json().get("id")

        # Verify Jupyter root assumption
        check_code = (
            "import os\n"
            f"_root_probe = os.path.exists('/kaggle/working/{probe_remote}')\n"
        )
        res = execute_in_kernel(
            client.websocket_base,
            base_url,
            client.token,
            ephemeral_kernel_id,
            check_code,
            on_text=lambda t: None,
            user_expressions={"root_probe": "_root_probe"},
            timeout=30,
        )
        root_probe_val = (
            (res.get("user_expressions", {})
             .get("root_probe", {})
             .get("data", {})
             .get("text/plain", ""))
        )
        if "True" not in root_probe_val:
            results.append(
                DoctorResult(
                    "Jupyter Root Verification",
                    "FAIL",
                    f"Upload root is not /kaggle/working (found: {root_probe_val})",
                    "Kaggle environment filesystem path changed",
                )
            )
        else:
            results.append(
                DoctorResult(
                    "Jupyter Root Verification",
                    "PASS",
                    "Remote upload root matches /kaggle/working",
                )
            )

        # In that same ephemeral kernel:
        # print(1+1) user_expression
        calc_code = "print(1 + 1)\n"
        calc_res = execute_in_kernel(
            client.websocket_base,
            base_url,
            client.token,
            ephemeral_kernel_id,
            calc_code,
            on_text=lambda t: None,
            user_expressions={"two": "1 + 1"},
            timeout=20,
        )
        two_val = (
            (calc_res.get("user_expressions", {})
             .get("two", {})
             .get("data", {})
             .get("text/plain", ""))
        )
        if "2" not in two_val:
            results.append(
                DoctorResult(
                    "Kernel Execution",
                    "FAIL",
                    f"Expression 1+1 failed: {two_val}",
                    "Kernel failed basic evaluation",
                )
            )
        else:
            results.append(
                DoctorResult(
                    "Kernel Execution",
                    "PASS",
                    "Basic Python execution and expression evaluation verified",
                )
            )

        # Inspect GPU, Disk, Python, Internet
        inspect_code = """\
import json, shutil, subprocess, sys, urllib.request

info = {}
try:
    smi = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=10
    )
    info["gpu"] = (smi.stdout or "").strip()
except Exception:
    info["gpu"] = ""

try:
    total, used, free = shutil.disk_usage('/kaggle/working')
    info["free_gb"] = round(free / (1024 ** 3), 1)
except Exception:
    info["free_gb"] = 0

info["python"] = sys.version.split()[0]

try:
    with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=10) as r:
        info["internet"] = (r.status == 200)
except Exception:
    info["internet"] = False

_INSPECT_RESULT = json.dumps(info)
"""
        insp_res = execute_in_kernel(
            client.websocket_base,
            base_url,
            client.token,
            ephemeral_kernel_id,
            inspect_code,
            on_text=lambda t: None,
            user_expressions={"info": "_INSPECT_RESULT"},
            timeout=30,
        )
        info_json = (
            insp_res.get("user_expressions", {})
            .get("info", {})
            .get("data", {})
            .get("text/plain", "")
        )
        info_dict = {}
        try:
            if info_json.startswith("'") or info_json.startswith('"'):
                info_json = json.loads(info_json)
            info_dict = json.loads(info_json)
        except Exception:
            pass

        # GPU Check
        gpu_info = info_dict.get("gpu", "")
        if not gpu_info:
            results.append(
                DoctorResult(
                    "GPU Availability",
                    "WARN",
                    "No GPU detected (nvidia-smi empty or unavailable)",
                    "Enable GPU in Kaggle session options",
                )
            )
        else:
            results.append(
                DoctorResult("GPU Availability", "PASS", f"GPU: {gpu_info}")
            )

        # Disk check
        free_gb = info_dict.get("free_gb", 0)
        results.append(
            DoctorResult(
                "Disk Space",
                "PASS",
                f"Free disk in /kaggle/working: {free_gb} GB",
            )
        )

        # Python version
        py_ver = info_dict.get("python", "unknown")
        results.append(
            DoctorResult("Remote Python Version", "PASS", f"Python {py_ver}")
        )

        # Internet check
        if not info_dict.get("internet"):
            results.append(
                DoctorResult(
                    "Kaggle Internet",
                    "WARN",
                    "Cannot reach PyPI (Internet access disabled)",
                    "Kaggle Internet is off: enable it in notebook settings",
                )
            )
        else:
            results.append(
                DoctorResult(
                    "Kaggle Internet",
                    "PASS",
                    "Outbound internet access verified",
                )
            )

        # 4. Deep check (45s silent cell)
        if deep:
            deep_code = "import time\ntime.sleep(45)\n"
            deep_res = execute_in_kernel(
                client.websocket_base,
                base_url,
                client.token,
                ephemeral_kernel_id,
                deep_code,
                on_text=lambda t: None,
                timeout=60,
            )
            if deep_res.get("status") == "ok":
                results.append(
                    DoctorResult(
                        "Deep Keepalive Check",
                        "PASS",
                        "45s silent cell executed with ping keepalives",
                    )
                )
            else:
                results.append(
                    DoctorResult(
                        "Deep Keepalive Check",
                        "FAIL",
                        f"Silent cell failed: {deep_res.get('error_text', '')}",
                        "Websocket keepalive / ping failed",
                    )
                )

    except KeyboardInterrupt:
        results.append(
            DoctorResult(
                "Remote Checks",
                "WARN",
                "Interrupted by user (Ctrl+C)",
                "Cleaning up remote temporary resources...",
            )
        )
    except Exception as e:
        results.append(
            DoctorResult(
                "Remote Checks",
                "FAIL",
                f"Remote doctor check failed: {e}",
                "Check server status and connectivity",
            )
        )
    finally:
        # Clean up ephemeral resources
        try:
            if ephemeral_kernel_id:
                client.session.delete(
                    f"{base_url}/api/kernels/{ephemeral_kernel_id}",
                    timeout=10,
                )
        except Exception:
            cleanup_ok = False

        try:
            client.delete(temp_remote_dir)
        except Exception:
            cleanup_ok = False

        if cleanup_ok:
            results.append(
                DoctorResult(
                    "Cleanup",
                    "PASS",
                    "Temporary doctor directory and ephemeral kernel removed",
                )
            )
        else:
            results.append(
                DoctorResult(
                    "Cleanup",
                    "WARN",
                    "Could not completely delete temporary doctor files",
                    "Check /kaggle/working/local-project on server",
                )
            )

    return results


def run_doctor(
    project_root: Optional[Path] = None,
    url: Optional[str] = None,
    deep: bool = False,
    as_json: bool = False,
    fix: bool = False,
) -> int:
    if project_root is None:
        project_root = Path.cwd().resolve()
    else:
        project_root = Path(project_root).resolve()

    if fix:
        fixes = apply_safe_fixes(project_root)
        if not as_json:
            if fixes:
                print("Applied fixes:")
                for f in fixes:
                    print(f"  - {f}")
            else:
                print("No fixes needed.")
            print()

    results: List[DoctorResult] = []

    # Local Checks
    results.append(check_python_os())
    results.append(check_dependencies())
    results.append(check_console_encoding())
    results.append(check_old_launchers())
    results.append(check_project_root(project_root))
    results.append(check_onedrive(project_root))
    results.append(check_runner_home_writable())
    results.append(check_url_source_and_age(project_root, url))
    results.append(check_url_permissions(project_root))
    results.append(check_legacy_url_file())
    results.append(check_kagglesyncignore(project_root))
    results.append(check_requirements_file(project_root))
    results.append(check_cloud_placeholders(project_root))
    results.append(check_files_to_sync(project_root))
    results.append(check_sync_state(project_root))
    results.append(check_secrets_scan(project_root))

    # Resolve URL for remote checks
    resolved_url = url
    if not resolved_url:
        resolved_url = urlstore.load_url(project_root, check_ttl=False)

    if resolved_url:
        from .sync import JupyterClient
        try:
            client = JupyterClient(resolved_url)
            remote_results = run_remote_checks(client, project_root, deep=deep)
            results.extend(remote_results)
        except Exception as e:
            results.append(
                DoctorResult(
                    "Remote Checks",
                    "FAIL",
                    f"Could not initialize Jupyter client: {e}",
                    "Check URL format: https://<host>/k/<session>/<token>/proxy",
                )
            )
    else:
        results.append(
            DoctorResult(
                "Remote Checks",
                "WARN",
                "Skipped remote checks (no URL provided or saved)",
                "Run kaggle-sync <URL> or pass URL to doctor",
            )
        )

    has_fail = any(r.status == "FAIL" for r in results)

    if as_json:
        data = [r.to_dict() for r in results]
        print(json.dumps(data, indent=2))
    else:
        for r in results:
            print(r.format_line())

    remote_session = next(
        (result for result in results if result.name == "Remote Session"),
        None,
    )
    if remote_session is not None and remote_session.status == "FAIL":
        if "expired" in remote_session.message.lower():
            return session_guard.EXIT_EXPIRED
        if "unreachable" in remote_session.message.lower():
            return session_guard.EXIT_OFFLINE
    return session_guard.EXIT_FAILURE if has_fail else session_guard.EXIT_OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="kaggle-sync doctor",
        description="Diagnose local environment and remote Kaggle session.",
    )
    parser.add_argument("url", nargs="?", help="Optional Kaggle session URL")
    parser.add_argument("--project", default=None, help="Project directory (default: cwd)")
    parser.add_argument("--deep", action="store_true", help="Run 45s silent keepalive test")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output machine-readable JSON")
    parser.add_argument("--fix", action="store_true", help="Apply safe local fixes")

    args = parser.parse_args(argv)
    proj = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    rc = run_doctor(
        project_root=proj,
        url=args.url,
        deep=args.deep,
        as_json=args.as_json,
        fix=args.fix,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
