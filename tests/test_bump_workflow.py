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
    text = _text()
    # One block, top level: a job-level block would be granted on top of this one and widen the
    # token, which is the thing this test exists to catch.
    assert [line for line in text.splitlines() if line.startswith("permissions:")] == ["permissions:"]
    match = re.search(r"^permissions:\n((?:  \S.*\n)+)", text, re.M)
    assert match, "the workflow states its permissions"
    assert match.group(1).split() == ["contents:", "write", "pull-requests:", "write"]


def test_it_never_merges_approves_or_promotes():
    text = _text()
    for forbidden in ("pr merge", "pr review", "--auto", "promote.sh", "gh api -X", "--method"):
        assert forbidden not in text, f"the bot must never run {forbidden!r}"


def test_it_stages_only_the_manifest():
    staged = [line.strip() for line in _text().splitlines() if line.strip().startswith("git add")]
    assert staged == ["git add containers/images.json"]


def test_the_branch_is_namespaced_by_run_number():
    assert "bump/plugins-${{ github.run_number }}" in _text()


def test_it_runs_the_gate_itself_because_no_check_will_run_on_its_pr():
    """A PR opened with GITHUB_TOKEN gets no pull_request workflows, so containers.yml — and the
    gate in it — never runs on the bot's PR. The gap has to be covered by the bot, against the
    commit the bump replaces, before the PR can exist."""
    text = _text()
    assert 'git show HEAD:containers/images.json > "$RUNNER_TEMP/base.json"' in text
    gate = text.index("scripts/ci_gate.py")
    assert '--base "$RUNNER_TEMP/base.json" --head containers/images.json' in text
    assert gate < text.index("gh pr create"), "a rejected bump must fail the run, not open a PR"


def test_a_resolver_crash_fails_the_run_instead_of_reading_as_nothing_to_bump():
    """The resolver's own exit codes are 0 and 2. An uncaught exception exits 1, which
    `[ $? -le 2 ]` accepted: a crash then wrote an empty body, left the manifest untouched and
    reported "Nothing to bump: every pin already resolves" on a green run. Only 0 and 2 are
    normal here."""
    text = _text()
    assert "-le 2" not in text, "-le 2 accepts exit 1, which is what a crash returns"
    assert text.count("rc=$?") == 2, "both resolver calls must check the code they got"
    assert text.count("case \"$rc\" in 0|2) ;; *) exit 1 ;; esac") == 2


def test_uv_is_installed_before_it_is_used():
    """`uv run` with no setup step is an assumption about the runner image, not a pin."""
    text = _text()
    assert "astral-sh/setup-uv@" in text
    assert text.index("astral-sh/setup-uv@") < text.index("uv run")


def test_the_duplicate_guard_runs_before_anything_is_pushed():
    text = _text()
    assert text.index("gh pr list") < text.index("git push")
    assert "--limit 100" in text, "gh pr list truncates at 30, so the guard would miss older PRs"
