"""User-controlled ignore rules and built-in secret exclusions for kaggle-sync.

Backed by pathspec (gitwildmatch syntax, same as .gitignore). Reads
<project>/.kagglesyncignore, creating it with sane defaults the first time.
Built-in secret patterns cannot be overridden by negation patterns unless
KAGGLE_SYNC_ALLOW_SECRETS=1 is set.
"""

import fnmatch
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pathspec
except ImportError:  # pragma: no cover
    pathspec = None


IGNORE_FILENAME = ".kagglesyncignore"

DEFAULT_IGNORE_TEXT = """\
# Files and folders excluded from Kaggle sync.
# One gitignore-style pattern per line (gitwildmatch syntax).
# A trailing "/" matches a directory and everything under it.
# Lines starting with "#" are comments; blank lines are ignored.
#
# Prefix a pattern with "!" to re-include a file, for example:
#   !data/small_sample.csv
#
# data folders are usually attached as Kaggle Datasets instead
# of being uploaded (see the readme "Datasets" section).
data/
dataset/
datasets/
kaggle_datasets/
dataset_extracted/
outputs/
output/
checkpoints/
runs/
wandb/
lightning_logs/
mlruns/
*.pt
*.pth
*.ckpt
*.safetensors
*.onnx
*.zip
*.tar
*.tar.gz
*.7z
*.log
"""

# Secret patterns that no "!" rule can override
SECRET_DIR_NAMES = frozenset({".kaggle", ".aws", ".ssh"})
SECRET_FILE_PATTERNS = (
    ".env",
    "*.pem",
    "*.key",
    "*.pfx",
    ".netrc",
    "kaggle.json",
    "*.kdbx",
)

_ALLOW_SECRETS_WARNED = False


def is_secret_path(rel_posix_path: str, is_dir: bool = False) -> bool:
    """Return True if path matches built-in secret exclusions."""
    global _ALLOW_SECRETS_WARNED
    if os.getenv("KAGGLE_SYNC_ALLOW_SECRETS") == "1":
        if not _ALLOW_SECRETS_WARNED:
            _ALLOW_SECRETS_WARNED = True
            print("[WARNING] KAGGLE_SYNC_ALLOW_SECRETS=1 is set: secret-looking files WILL be synced!")
        return False

    norm = str(rel_posix_path).replace("\\", "/").strip("/")
    parts = norm.split("/")
    if not parts or parts == [""]:
        return False

    filename = parts[-1]

    # Directory checks
    for part in parts:
        if part in SECRET_DIR_NAMES:
            return True

    # Filename checks
    if filename == ".env" or filename.startswith(".env."):
        return True
    if filename.startswith("id_rsa") or filename.startswith("id_ed25519"):
        return True
    if filename.startswith("credentials") and filename.endswith(".json"):
        return True
    for pat in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatch(filename, pat):
            return True

    return False


def count_secret_files(project_root) -> int:
    """Count existing files in the project matching secret exclusions."""
    root = Path(project_root).resolve()
    if not root.exists():
        return 0

    count = 0
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".kaggle-runner"}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        # Check if dir itself is secret
        if rel_dir and is_secret_path(rel_dir, is_dir=True):
            count += len(filenames)
            continue

        for fname in filenames:
            rel_file = f"{rel_dir}/{fname}" if rel_dir else fname
            if is_secret_path(rel_file, is_dir=False):
                count += 1

    return count


def _read_lines(path) -> List[str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []

    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _stat_id(path) -> Optional[tuple]:
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


class _ProjectIgnore:
    def __init__(self, spec, has_negation: bool):
        self._spec = spec
        self.has_negation = has_negation

    def is_ignored(self, rel_posix_path: str, is_dir: bool = False) -> bool:
        path = str(rel_posix_path).replace("\\", "/").lstrip("/")
        if not path:
            return False
        if is_dir and not path.endswith("/"):
            path = path + "/"
        try:
            return bool(self._spec.match_file(path))
        except Exception:
            return False


_CACHE: Dict[str, dict] = {}


def load_spec(project_root) -> _ProjectIgnore:
    """Load or reload on mtime change the ignore rules for a project."""
    if pathspec is None:
        raise RuntimeError(
            "The 'pathspec' package is required for ignore rules. "
            "Install it with: pip install -e ."
        )

    try:
        root = Path(project_root).resolve()
    except Exception:
        root = Path(os.path.abspath(str(project_root)))

    key = str(root)
    ignore_path = root / IGNORE_FILENAME

    if not ignore_path.exists():
        try:
            ignore_path.write_text(DEFAULT_IGNORE_TEXT, encoding="utf-8")
        except OSError:
            pass

    use_gitignore = os.getenv("KAGGLE_SYNC_USE_GITIGNORE", "") == "1"
    git_path = root / ".gitignore"

    ignore_id = _stat_id(ignore_path)
    git_id = _stat_id(git_path) if use_gitignore else None

    cached = _CACHE.get(key)
    if (
        cached is not None
        and cached["ignore_id"] == ignore_id
        and cached["git_id"] == git_id
        and cached["use_gitignore"] == use_gitignore
    ):
        return cached["rules"]

    lines = _read_lines(ignore_path)
    if use_gitignore and git_path.exists():
        lines = lines + _read_lines(git_path)

    spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
    has_neg = any(line.startswith("!") for line in lines)
    rules = _ProjectIgnore(spec, has_neg)

    _CACHE[key] = {
        "ignore_id": ignore_id,
        "git_id": git_id,
        "use_gitignore": use_gitignore,
        "rules": rules,
    }
    return rules


def is_ignored(project_root, rel_posix_path: str, is_dir: bool = False) -> bool:
    """True when a project-relative path is ignored or matches secret exclusion."""
    if is_secret_path(rel_posix_path, is_dir=is_dir):
        return True
    try:
        return load_spec(project_root).is_ignored(rel_posix_path, is_dir=is_dir)
    except Exception:
        return False


def has_negation(project_root) -> bool:
    try:
        return bool(load_spec(project_root).has_negation)
    except Exception:
        return False


def clear_cache():
    _CACHE.clear()
