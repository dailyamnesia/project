"""Runs the README's own Quick Start, literally, the way a brand-new user
would copy-paste it — not just eyeballing the prose against the code.
"""

import io
import itertools
import os
import re
import shlex
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from flashback.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"


def _quick_start_lines():
    """Return the Quick Start section's fenced ```bash block as a list of
    (non-blank, non-comment-only) shell lines, trailing `# ...` explanations
    stripped, in the exact order README.md gives them.
    """
    text = README.read_text(encoding="utf-8")
    match = re.search(r"## Quick start\n\n```bash\n(.*?)```", text, re.DOTALL)
    assert match is not None, "couldn't find the Quick Start's fenced bash block in README.md"
    lines = []
    for raw in match.group(1).splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if line.strip():
            lines.append(line)
    return lines


class TestQuickStartIsSelfContained(unittest.TestCase):
    """The Install section presents two equally-valid ways to end up with the
    `flashback` command: `pip install git+https://...` on its own (no clone
    anywhere), or `git clone` + `pip install -e .` — "Either way you end up
    with a `flashback` command." The Quick Start that immediately follows
    both, though, has always opened with `cp examples/spanish-basics.md
    decks/`: a path that only resolves for the second, cloned way.
    `examples/` lives at the repo root and was never part of what actually
    gets installed (confirmed directly: a real `setuptools.build_meta.
    build_sdist()` of this project includes only `flashback/*.py`,
    `tests/*.py`, `LICENSE`, `README.md`, and `pyproject.toml` — no
    `examples/`). So the first, plainer install path's very next
    instruction — the second command a brand-new user following it is told
    to run — fails outright: `cp: cannot stat 'examples/spanish-basics.md':
    No such file or directory`.

    This runs the Quick Start's own commands for real, from a scratch
    directory that stands in for that first install path: `flashback` is
    importable (the equivalent of having actually run `pip install`), but
    nothing else from this repo — in particular no `examples/` — is
    anywhere nearby, unlike every previous README cross-check, which always
    ran from inside a real clone and so never actually exercised this path.
    """

    def _run_line(self, line, tmp):
        tokens = shlex.split(line)
        if tokens[0] == "mkdir":
            for name in tokens[1:]:
                (Path(tmp) / name).mkdir(parents=True, exist_ok=True)
            return
        if tokens[0] == "cp":
            src, dst = tokens[1], tokens[2]
            dst_path = Path(tmp) / dst
            if dst_path.is_dir():
                dst_path = dst_path / Path(src).name
            # Deliberately not seeded anywhere under `tmp` — a pip-only
            # install has no such file, so this raises exactly the
            # FileNotFoundError a real `cp` would print as "No such file or
            # directory" for the same reason.
            shutil.copy(Path(tmp) / src, dst_path)
            return
        if tokens[0] == "flashback":
            rc = main(tokens[1:])
            self.assertEqual(rc, 0, f"{line!r} exited {rc}")
            return
        self.fail(f"Quick Start line uses a command this test doesn't model: {line!r}")

    def test_quick_start_runs_with_only_flashback_installed(self):
        commands = _quick_start_lines()
        self.assertTrue(commands, "Quick Start block parsed to no commands")

        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                # Feed "reveal" then "good" for however many cards `review`
                # finds due — the Quick Start doesn't script specific
                # grades, it's just demonstrating that the command works.
                with patch("builtins.input", side_effect=itertools.cycle(["", "3"])), redirect_stdout(
                    io.StringIO()
                ):
                    for line in commands:
                        self._run_line(line, tmp)
            finally:
                os.chdir(original_cwd)

            self.assertTrue((Path(tmp) / "decks" / "spanish-basics.md").exists())


if __name__ == "__main__":
    unittest.main()
