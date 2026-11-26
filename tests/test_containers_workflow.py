"""The containers workflow is the only thing that publishes a server image or moves a channel.

Pinned as text, the way `os.yml` is, because a run log does not show either rule until it has
already broken: a promote dispatch must not rebuild (a rebuild re-pushes a published version tag
and moves `stable-next` onto bytes no human reviewed), and a merge must not publish before the
repo's own tests have run.
"""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/containers.yml"
JOBS = ("tests", "matrix", "build", "promote")


def _text():
    return WORKFLOW.read_text()


def _job(name):
    text = _text()
    start = text.index(f"  {name}:")
    later = [text.index(f"  {other}:") for other in JOBS
             if text.index(f"  {other}:") > start]
    return text[start:min(later, default=len(text))]


def test_a_promote_dispatch_does_not_rebuild():
    """A digest dispatch is a promote run. Rebuilding here re-pushes the published version tag
    with a new revision label and moves stable-next to bytes nobody canaried — in the same run
    that promotes stable to the old digest."""
    build = _job("build")
    assert "if: github.event_name != 'workflow_dispatch' || github.event.inputs.digest == ''" in build
    # The image input is the force-rebuild lever, not the promote selector.
    assert "github.event.inputs.image != ''" not in build


def test_an_image_only_dispatch_still_forces_a_build():
    """Ruling 4b: the force lever is an image with no digest. It is what rebuilds after a
    Dockerfile-only fix — and the only dispatch that reaches the build job."""
    text = _text()
    assert "FORCE: ${{ github.event.inputs.image }}" in text
    assert "--force-image" in text
    assert "digest == ''" in _job("build")


def test_the_promote_job_takes_a_digest_and_names_the_image():
    """promote.sh refuses an empty IMAGE, so a digest-only dispatch fails at the promote step
    rather than silently rebuilding. The guard keys on the digest, which is what selects the job."""
    promote = _job("promote")
    assert "if: github.event_name == 'workflow_dispatch' && github.event.inputs.digest != ''" in promote
    assert "IMAGE: ${{ github.event.inputs.image }}" in promote
    assert "bash scripts/promote.sh" in promote


def test_the_tests_job_runs_the_suite_and_the_shell_checks():
    """Nothing else in the repo runs these, and the shell suites are the only coverage the
    seed/promote/render code has. They stub curl, podman and ssh: no fleet, no ghcr secret."""
    tests = _job("tests")
    assert "uv run pytest -q" in tests
    for suite in ("tests/test_plugin_seed.sh", "tests/test_promote.sh",
                  "tests/test_create_nodes_render.sh"):
        assert suite in tests, f"{suite} is never run in CI"


def test_the_build_waits_for_the_tests():
    """A publishing job that can start before the tests is the gap this job closed."""
    needs = next(line for line in _job("build").splitlines() if line.strip().startswith("needs:"))
    assert "tests" in needs and "matrix" in needs, needs


def test_uv_is_installed_before_any_job_uses_it():
    """`uv run` with no setup step is an assumption about the runner image, not a pin."""
    for name in JOBS:
        job = _job(name)
        if "uv run" not in job:
            continue
        assert "astral-sh/setup-uv@" in job, f"the {name} job runs uv without installing it"
        assert job.index("astral-sh/setup-uv@") < job.index("uv run"), name


def test_no_latest_tag_is_pushed_for_a_server_image():
    """A deploy follows stable, never latest: latest is the OS channel only."""
    text = _text()
    tags = text[text.index("tags: |"):text.index("labels: |")]
    assert "latest" not in tags
