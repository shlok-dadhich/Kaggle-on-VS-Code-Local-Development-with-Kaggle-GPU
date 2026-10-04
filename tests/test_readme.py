"""README verification tests."""

import re
import subprocess
from pathlib import Path


def test_readme_conformance():
    readme_path = Path(__file__).resolve().parent.parent / "readme.md"
    assert readme_path.exists(), "readme.md does not exist"

    lines = readme_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 110, f"readme.md has {len(lines)} lines; must be <= 110"

    full_text = "\n".join(lines)

    # Broken escapes or chat transcripts
    assert "\\#" not in full_text, "Found broken escape \\# in readme"
    assert "&#x20;" not in full_text, "Found broken entity &#x20; in readme"
    assert "test-run" not in full_text, "Found reference to non-existent test-run in readme"

    # No Windows personal username paths (e.g. C:\Users\...)
    user_pattern = re.compile(r"[A-Za-z]:\\Users\\[^\s`\"'\\]+", re.IGNORECASE)
    assert not user_pattern.search(full_text), "Found personal Windows user path in readme"


def test_commands_in_readme_code_blocks_answer_help():
    readme_path = Path(__file__).resolve().parent.parent / "readme.md"
    text = readme_path.read_text(encoding="utf-8")

    # Find fenced code blocks
    code_blocks = re.findall(r"```(?:bash|powershell|cmd|sh)?\n(.*?)```", text, re.DOTALL)
    tested_commands = set()

    for block in code_blocks:
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first_word = line.split()[0]
            if first_word.startswith("kaggle-"):
                tested_commands.add(first_word)

    assert len(tested_commands) > 0, "No kaggle-* commands found in code blocks"

    for cmd in tested_commands:
        res = subprocess.run([cmd, "--help"], capture_output=True, text=True)
        assert res.returncode == 0, f"Command {cmd} failed --help: {res.stderr}"
