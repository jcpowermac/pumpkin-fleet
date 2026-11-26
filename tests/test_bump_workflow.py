"""The bump bot may open a PR and nothing else.

`.github/workflows/bump-plugins.yml` is the only automated PR author in the repo, so what it
must never do is pinned here rather than trusted to review: no merge, no approval, no
`stable` move, one file in the commit, and no permission beyond the two it needs.
"""
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/bump-plugins.yml"


def _text():
    return WORKFLOW.read_text()


def test_it_runs_weekly_and_on_demand():
    text = _text()
    assert "cron: '0 6 * * 3'" in text
    assert "workflow_dispatch:" in text
    assert "runs-on: ubuntu-latest" in text


def test_it_asks_for_exactly_the_two_permissions_it_needs():
    match = re.search(r"^permissions:\n((?:  \S.*\n)+)", _text(), re.M)
    assert match, "the workflow states its permissions"
    assert match.group(1).split() == ["contents:", "write", "pull-requests:", "write"]


def test_it_never_merges_approves_or_promotes():
    text = _text()
    for forbidden in ("pr merge", "pr review", "--auto", "promote.sh", "gh api -X"):
        assert forbidden not in text, f"the bot must never run {forbidden!r}"


def test_it_stages_only_the_manifest():
    staged = [line.strip() for line in _text().splitlines() if line.strip().startswith("git add")]
    assert staged == ["git add containers/images.json"]


def test_the_branch_is_namespaced_by_run_number():
    assert "bump/plugins-${{ github.run_number }}" in _text()
