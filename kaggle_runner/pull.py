"""Fetch files created on Kaggle back to the local project.

kaggle-sync auto-downloads only small text/image files. Everything
else (checkpoints, datasets, videos, ...) shows up as [REMOTE NEW]
and is fetched on demand with this tool:

    kaggle-pull <path-or-glob> [more ...]
    kaggle-pull --list
    kaggle-pull --all-pending
    kaggle-pull outputs/model.pt --dest runs/exp1 --force

Run from the project folder. Paths are relative to
/kaggle/working/local-project.

Exit codes: 0 all ok, 1 some failed, 2 usage error,
3 session expired.
"""

import argparse
import fnmatch
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from . import session_guard, sync, urlstore
from . import __version__
from .sync import (
    JupyterClient,
    REMOTE_ROOT,
    REQUEST_TIMEOUT,
)


PART_SUFFIX = ".kaggle-pull.part"

CHUNK_BYTES = 1024 * 1024

# Whole-file fallback ceiling for the Contents API.
CONTENTS_FALLBACK_LIMIT_BYTES = 100 * 1024 * 1024

# Retries for raw-byte downloads on timeout or 5xx.
PULL_RETRY_BACKOFFS = (1, 2, 4)


def fail_usage(message):

    print()
    print(f"ERROR: {message}")
    print()
    print(
        "Usage: kaggle-pull <path-or-glob> [...] "
        "[--list] [--all-pending] [--dest DIR] "
        "[--force] [--max-mb N]"
    )
    print()
    sys.exit(2)


def fail_session_expired():

    print()
    print(
        "Kaggle session expired - "
        "run kaggle-sync with a new URL"
    )
    print()
    sys.exit(3)


def is_auth_status(status):

    return status in (401, 403)


def parse_args(argv):

    parser = argparse.ArgumentParser(
        prog="kaggle-pull",
        description=(
            "Fetch Kaggle-created files back to the local project. "
            "Paths are relative to /kaggle/working/local-project."
        ),
    )

    parser.add_argument(
        "paths",
        nargs="*",
        help="Remote path, glob or directory to pull.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List pending remote files (name, size, age).",
    )

    parser.add_argument(
        "--all-pending",
        action="store_true",
        help="Pull every pending remote file.",
    )

    parser.add_argument(
        "--dest",
        default=None,
        help="Local destination directory for pulled files.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite differing local files.",
    )

    parser.add_argument(
        "--max-mb",
        default=None,
        help="Skip files larger than N megabytes.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    try:
        namespace = parser.parse_args(argv)
    except SystemExit as e:
        sys.exit(2 if e.code != 0 else 0)

    max_bytes = None

    if namespace.max_mb is not None:

        max_bytes = None

        try:
            max_bytes = int(float(namespace.max_mb) * 1024 * 1024)
        except (TypeError, ValueError):
            fail_usage(
                f"Invalid --max-mb value: {namespace.max_mb}"
            )

        if max_bytes is None or max_bytes < 0:
            fail_usage(
                f"Invalid --max-mb value: {namespace.max_mb}"
            )

        assert max_bytes is not None

    if (
        not namespace.paths
        and not namespace.list
        and not namespace.all_pending
    ):
        fail_usage("Nothing to pull.")

    return namespace, max_bytes


def to_relative(spec):
    """Normalize a user path to project-relative posix form.

    Accepts "outputs/model.pt" as well as the absolute
    "/kaggle/working/local-project/outputs/model.pt" form. Rejects
    anything escaping the project root.
    """

    text = str(spec).replace("\\", "/").strip()

    if text.startswith("/kaggle/working/"):
        text = text[len("/kaggle/working/"):]

        if text == REMOTE_ROOT:
            text = ""
        elif text.startswith(REMOTE_ROOT + "/"):
            text = text[len(REMOTE_ROOT) + 1:]
        else:
            raise ValueError(
                f"Path is outside the synced project: {spec}"
            )

    elif text.startswith("/"):
        raise ValueError(
            f"Absolute path outside the synced project: {spec}"
        )

    if len(text) >= 2 and text[1] == ":":
        raise ValueError(
            f"Absolute path outside the synced project: {spec}"
        )

    parts = [
        part
        for part in text.split("/")
        if part not in ("", ".")
    ]

    if not parts:
        raise ValueError(
            f"Empty path after normalization: {spec}"
        )

    if any(part == ".." for part in parts):
        raise ValueError(
            f"Path escapes the project root: {spec}"
        )

    return "/".join(parts)


def remote_url_for_file(client, relative):

    encoded = "/".join(
        quote(part, safe="")
        for part in relative.split("/")
    )

    return f"{client.base_url}/files/{encoded}"


def fetch_remote_model(client, relative):
    """File model (with size) via the Contents API, or None (404)."""

    response = client.request_session().get(
        client.api_url(f"{REMOTE_ROOT}/{relative}"),
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 404:
        return None

    if is_auth_status(response.status_code):
        fail_session_expired()

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        return None

    return data


def refresh_pending(client, project_root):
    """One BFS listing: drop vanished pendings, add new ones."""

    project_root = Path(project_root).resolve()

    with sync.STATE_LOCK:
        pending = sync.load_pending_remote(project_root)
        remote_manifest = sync.load_remote_manifest(project_root)

    remote_files = sync.list_remote_tree_bfs(client, project_root)
    current = {}

    for entry in remote_files:

        remote_path = entry.get("path", "").replace("\\", "/")

        if not remote_path.startswith(f"{REMOTE_ROOT}/"):
            continue

        relative = remote_path[len(REMOTE_ROOT) + 1:]
        current[relative] = entry

    with sync.STATE_LOCK:
        pending = sync.load_pending_remote(project_root)
        remote_manifest = sync.load_remote_manifest(project_root)

        for relative, entry in current.items():

            if sync.should_skip(Path(relative)):
                continue

            if sync.should_skip_remote(relative):
                continue

            if sync.ignore_rules.is_ignored(
                project_root,
                relative,
                is_dir=False,
            ):
                continue

            old = remote_manifest.get(relative)

            signature = {
                key: entry.get(key)
                for key in ("size", "last_modified", "created")
                if entry.get(key) is not None
            }

            if old == signature:
                continue

            size = entry.get("size")

            if sync.auto_download_eligible(relative, size):
                continue

            if sync.DOWNLOAD_POLICY == "off":
                continue

            pending[relative] = {
                "size": size,
                "last_modified": entry.get("last_modified"),
            }

        for relative in list(pending):

            if relative not in current:
                del pending[relative]

        sync.save_pending_remote(project_root, pending)
        sync.save_remote_manifest(
            project_root,
            {
                relative: {
                    key: entry.get(key)
                    for key in ("size", "last_modified", "created")
                    if entry.get(key) is not None
                }
                for relative, entry in current.items()
            },
        )

    return pending


def print_pending(pending):

    if not pending:
        print("No pending remote files.")
        return

    now = time.time()

    print(f"{'path':60} {'size':>12}  {'age':>10}")
    print("-" * 88)

    for relative in sorted(pending):

        info = pending[relative] or {}
        size = info.get("size")
        age = "?"

        try:

            if info.get("last_modified"):

                stamp = time.mktime(
                    time.strptime(
                        str(info["last_modified"])[:19],
                        "%Y-%m-%dT%H:%M:%S",
                    )
                )
                seconds = max(0, now - stamp)

                if seconds < 3600:
                    age = f"{int(seconds // 60)}m"
                elif seconds < 86400:
                    age = f"{int(seconds // 3600)}h"
                else:
                    age = f"{int(seconds // 86400)}d"

        except Exception:
            age = "?"

        print(
            f"{relative[:60]:60} "
            f"{sync.format_bytes(size):>12}  "
            f"{age:>10}"
        )


def expand_specs(client, project_root, specs):
    """Expand globs/directories to remote relative paths."""

    remote_files = sync.list_remote_tree_bfs(client, project_root)

    files = []
    directories = set()

    for entry in remote_files:

        remote_path = entry.get("path", "").replace("\\", "/")

        if not remote_path.startswith(f"{REMOTE_ROOT}/"):
            continue

        relative = remote_path[len(REMOTE_ROOT) + 1:]

        if entry.get("type") == "directory":
            directories.add(relative)
        elif entry.get("type") == "file":
            files.append(relative)

    wanted = []
    problems = []

    for spec in specs:

        try:
            normalized = to_relative(spec)
        except ValueError as e:
            problems.append(str(e))
            continue

        matched = set()

        for directory in directories:

            if (
                normalized == directory
                or directory.startswith(normalized + "/")
            ):
                matched.update(
                    path
                    for path in files
                    if (
                        path == directory
                        or path.startswith(directory + "/")
                    )
                )

        for path in files:

            if path == normalized:
                matched.add(path)
                continue

            if fnmatch.fnmatch(path, normalized):
                matched.add(path)
                continue

            if "/" not in normalized and fnmatch.fnmatch(
                path.rsplit("/", 1)[-1],
                normalized,
            ):
                matched.add(path)

        # A literal path need not appear in the listing (the entry
        # may be new); it is verified at download time.
        if not matched and not any(
            character in normalized for character in "*?["
        ):
            matched.add(normalized)

        if not matched:
            problems.append(f"No remote match: {spec}")
            continue

        wanted.extend(sorted(matched))

    # Deduplicate, keeping order.
    seen = set()
    unique = []

    for path in wanted:

        if path not in seen:
            seen.add(path)
            unique.append(path)

    return unique, problems


def show_progress(done, total, started, last_state):
    """Progress line: MB done/total, MB/s, ETA. Returns state dict."""

    now = time.time()
    elapsed = max(now - started, 1e-6)
    speed = done / elapsed
    fraction = 0.0

    if total and total > 0:
        fraction = min(done / total, 1.0)
        remaining = (total - done) / speed if speed > 0 else 0.0
        line = (
            f"\r{done / 1048576:.1f} / {total / 1048576:.1f} MB "
            f"({fraction * 100:.0f}%) "
            f"{speed / 1048576:.1f} MB/s "
            f"ETA {int(remaining)}s"
        )
    else:
        line = (
            f"\r{done / 1048576:.1f} MB "
            f"{speed / 1048576:.1f} MB/s"
        )

    if sys.stdout.isatty():
        sys.stdout.write(line)
        sys.stdout.flush()
        last_state["shown"] = fraction if total else 0.0
        return last_state

    shown_10ths = int((fraction if total else 0.0) * 10)

    if shown_10ths > last_state.get("shown_10ths", -1):
        last_state["shown_10ths"] = shown_10ths
        print(line.strip())

    return last_state


def part_size(part_path):

    try:

        if part_path.exists() and part_path.is_file():
            return part_path.stat().st_size

    except OSError:
        pass

    return 0


def download_raw(client, url, part_path, total, display):
    """Stream /files/ URL to part_path. Returns bytes on disk.

    Resumes from an existing .part file (Range) and recomputes the
    resume offset from disk before every attempt, so a retry after
    a mid-stream timeout never duplicates bytes. Raises on
    unrecoverable HTTP errors; retries timeout/5xx 3x.
    """

    done = part_size(part_path)

    if total is not None and done >= total:
        done = total

    if done > 0:
        print(f"[RESUME] {display} from {done} bytes")

    started = time.time()
    progress = {}
    attempts = 0

    while True:

        headers = {}

        if done > 0:
            headers["Range"] = f"bytes={done}-"

        try:

            response = client.request_session().get(
                url,
                headers=headers,
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )

        except requests.Timeout:

            attempts += 1

            if attempts > len(PULL_RETRY_BACKOFFS):
                raise

            time.sleep(PULL_RETRY_BACKOFFS[attempts - 1])
            done = part_size(part_path)
            continue

        status = response.status_code

        if status in (401, 403):
            fail_session_expired()

        if status >= 500:

            attempts += 1

            if attempts > len(PULL_RETRY_BACKOFFS):
                response.raise_for_status()

            time.sleep(PULL_RETRY_BACKOFFS[attempts - 1])
            done = part_size(part_path)
            continue

        if status == 404:
            raise FileNotFoundError(url)

        if status == 416:
            # Range beyond EOF: the part file already holds it all.
            return part_size(part_path)

        if done > 0 and status == 200:
            # Server ignored Range: restart from zero.
            done = 0

            print("Server ignored resume; restarting download.")

        if status not in (200, 206):
            response.raise_for_status()

        mode = "ab" if done > 0 else "wb"

        with open(part_path, mode) as handle:

            for chunk in response.iter_content(CHUNK_BYTES):

                if not chunk:
                    continue

                handle.write(chunk)
                done += len(chunk)
                progress = show_progress(
                    done, total, started, progress)

        if sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()

        return done


def pull_one(client, project_root, relative, size, args, max_bytes):
    """Download one remote file. Returns True on success."""

    project_root = Path(project_root).resolve()

    if max_bytes is not None and size is not None and size > max_bytes:
        print(
            f"[SKIP] {relative} "
            f"({sync.format_bytes(size)} exceeds --max-mb)"
        )
        return True

    if args.dest:
        destination = (
            Path(args.dest).expanduser() / Path(relative).name
        )
    else:
        destination = project_root / Path(relative)

    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and destination.is_file():

        try:
            local_size = destination.stat().st_size
        except OSError:
            local_size = None

        if local_size == size:
            print(
                f"[SKIP identical] {relative} "
                "(size-only comparison: sizes match)"
            )
            return True

        if not args.force:

            try:
                local_mtime = time.ctime(
                    destination.stat().st_mtime)
            except OSError:
                local_mtime = "?"

            print(
                f"[REFUSE] {relative}: local file differs "
                f"(local {sync.format_bytes(local_size)}, "
                f"mtime {local_mtime}; "
                f"remote {sync.format_bytes(size)}). "
                "Re-run with --force to overwrite."
            )
            return False

    part_path = destination.with_name(
        destination.name + PART_SUFFIX
    )

    url = remote_url_for_file(client, relative)

    try:
        download_raw(
            client, url, part_path, size, relative)
    except FileNotFoundError:

        if size is not None and size > CONTENTS_FALLBACK_LIMIT_BYTES:
            print(
                f"[FAIL] {relative}: /files/ endpoint returned 404 "
                f"and the file ({sync.format_bytes(size)}) exceeds "
                "the 100 MB Contents API fallback limit."
            )
            return False

        print(
            f"[FALLBACK contents API] {relative} "
            "(/files/ returned 404)"
        )

        try:

            if part_path.exists():
                part_path.unlink()

            client.download_file(
                f"{REMOTE_ROOT}/{relative}",
                part_path,
            )

        except Exception as e:

            if is_auth_status(
                getattr(getattr(e, "response", None),
                        "status_code", None)
            ):
                fail_session_expired()

            print(f"[FAIL] {relative}: {e}")
            return False

    except Exception as e:

        print(f"[FAIL] {relative}: {e}")
        return False

    if size is not None:

        try:
            final_size = part_path.stat().st_size
        except OSError:
            final_size = None

        if final_size != size:
            print(
                f"[FAIL] {relative}: size mismatch "
                f"(got {sync.format_bytes(final_size)}, "
                f"expected {sync.format_bytes(size)}); "
                "re-run to resume."
            )
            return False

    os.replace(part_path, destination)

    sync.mark_recently_downloaded(destination)

    with sync.STATE_LOCK:

        manifest = sync.load_sync_manifest(project_root)
        manifest[relative] = sync.file_signature_full(destination)
        sync.save_sync_manifest(project_root, manifest)

        pending = sync.load_pending_remote(project_root)
        pending.pop(relative, None)
        sync.save_pending_remote(project_root, pending)

    print(f"[PULLED] {relative}")

    if not sync.is_ignored_path(project_root, destination):
        print(
            f"Note: {relative} is not ignored; kaggle-sync will "
            "treat it as a normal project file."
        )

    return True


def main(argv=None):

    try:
        # sys.stdout.reconfigure is available on Python 3.7+
        sys.stdout.reconfigure(  # type: ignore[attr-defined]
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        pass

    if argv is None:
        argv = sys.argv[1:]

    namespace, max_bytes = parse_args(argv)

    project_root = Path.cwd().resolve()

    sync.bind_state_lock(project_root)

    server_url = urlstore.load_url(project_root)

    if not server_url:
        print()
        print("ERROR: No Kaggle URL saved for this project.")
        print()
        print("Run kaggle-sync from this project folder first:")
        print()
        print('  kaggle-sync "KAGGLE_VSCODE_URL"')
        print()
        sys.exit(session_guard.EXIT_FAILURE)

    client = JupyterClient(server_url)
    session_state = session_guard.probe(client)
    if session_state == "expired":
        fail_session_expired()
    if session_state == "offline":
        print("[OFFLINE] Kaggle server is unreachable.")
        sys.exit(session_guard.EXIT_OFFLINE)

    try:
        response = client.session.get(
            f"{client.base_url}/api",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as e:

        status = getattr(
            getattr(e, "response", None), "status_code", None)

        if is_auth_status(status):
            fail_session_expired()

        print()
        print("ERROR: Could not connect to Kaggle.")
        print(e)
        print()
        sys.exit(1)

    failed = 0

    if namespace.list or namespace.all_pending:

        try:
            pending = refresh_pending(client, project_root)
        except Exception as e:

            status = getattr(
                getattr(e, "response", None), "status_code", None)

            if is_auth_status(status):
                fail_session_expired()

            print(f"ERROR: remote listing failed: {e}")
            sys.exit(1)

        if namespace.list:
            print_pending(pending)

        targets = sorted(pending) if namespace.all_pending else []

        if namespace.all_pending and not targets:
            print("No pending remote files.")

    else:

        try:
            targets, problems = expand_specs(
                client, project_root, namespace.paths)
        except Exception as e:
            print(f"ERROR: remote listing failed: {e}")
            sys.exit(1)

        for problem in problems:
            print(f"[FAIL] {problem}")
            failed += 1

    for relative in targets:

        try:
            model = fetch_remote_model(client, relative)
        except Exception as e:
            print(f"[FAIL] {relative}: {e}")
            failed += 1
            continue

        if model is None:
            print(f"[FAIL] {relative}: not found on Kaggle.")
            failed += 1
            continue

        size = model.get("size")

        if not isinstance(size, (int, float)):
            size = None

        try:

            if not pull_one(
                client, project_root, relative, size,
                namespace, max_bytes,
            ):
                failed += 1

        except SystemExit:
            raise
        except Exception as e:
            print(f"[FAIL] {relative}: {e}")
            failed += 1

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()