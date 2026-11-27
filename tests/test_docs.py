"""The operator guide is the doc a human follows at 1am, so it rots loudly or not at all.

Two checks: every script in the repo is documented, and every repo path the guide names still
exists. Both fail as plain assertions — no docker, no pve3, no network needed."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "operations.md"

# The spike scripts are documented as a group (`scripts/spike/*` + `make spike`) in README.md.
EXEMPT = ("scripts/spike/",)


def _doc():
    return DOC.read_text()


def test_every_script_is_documented():
    text = _doc()
    missing = []
    for script in sorted((REPO / "scripts").rglob("*")) + sorted((REPO / "tasks").glob("*.py")):
        if not script.is_file() or script.suffix not in (".py", ".sh"):
            continue
        rel = script.relative_to(REPO).as_posix()
        if rel.startswith(EXEMPT):
            continue
        if rel not in text:
            missing.append(rel)
    assert not missing, f"undocumented scripts: {missing}"


def test_every_repo_path_the_guide_names_exists():
    """A command that names a file that moved is worse than no command: the reader follows it."""
    names = set(re.findall(r"`([^`\s]+(?:/[^`\s]+)+)`", _doc()))
    stale = [n for n in names
             if any(n.startswith(p) for p in ("scripts/", "tasks/", "containers/", "configs/",
                                              "docs/", "recipes/", ".github/"))
             and "*" not in n and "<" not in n and not (REPO / n).exists()]
    assert not stale, f"the guide names paths that do not exist: {sorted(stale)}"
