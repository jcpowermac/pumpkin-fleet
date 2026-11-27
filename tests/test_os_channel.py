"""The OS channel's promote job, pinned as text the way the bump bot's is.

`.github/workflows/os.yml` is the only thing that moves the OS channel tag the nodes follow, and
two rulings shape it: 6a (it accepts a digest ghcr publishes for *any* tag of `pumpkin-os`, so
an older dated build is a rollback) and the pins contract (`OS_TAG` names the tag the nodes
follow, so the job must move *that* tag). Both are invisible in a run log until a node is stuck
on a tag nothing moves.
"""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/os.yml"


def _promote_job():
    text = WORKFLOW.read_text()
    return text[text.index("  promote-os:"):]


def test_promote_os_is_a_dispatch_with_one_input():
    text = WORKFLOW.read_text()
    assert "workflow_dispatch:" in text
    assert "digest:" in text, "the digest is the only thing a human can promote"
    assert "if: github.event_name == 'workflow_dispatch' && github.event.inputs.digest != ''" in _promote_job()


def test_a_promote_dispatch_does_not_rebuild_latest():
    """Rebuilding on a promote run would re-push :latest, moving the digest the promote job was
    just asked to verify out from under the canary node."""
    text = WORKFLOW.read_text()
    bluebuild = text[text.index("  bluebuild:"):text.index("  promote-os:")]
    assert "if: github.event_name != 'workflow_dispatch' || github.event.inputs.digest == ''" in bluebuild


def test_it_accepts_any_published_digest_ruling_6a():
    """Anything narrower than the server images' rule — a pinned canary tag, say — means stable
    can only move forward and rollback becomes impossible."""
    job = _promote_job()
    assert "scripts/promote.sh" in job
    for narrowing in ("EXPECTED_TAG", "OS_TAG_CANARY"):
        assert narrowing not in job, f"{narrowing} narrows the accepted digests to one tag"


def test_a_promote_dispatch_cannot_cancel_a_build_mid_push():
    """The concurrency group is what serialises a promote against the scheduled build; cancelling
    the run in progress would kill whichever of the two started first, mid-push."""
    text = WORKFLOW.read_text()
    concurrency = text[text.index("concurrency:"):text.index("jobs:")]
    assert "cancel-in-progress: false" in concurrency
    assert "group: ${{ github.workflow }}" in concurrency, "overlapping runs must still serialise"


def test_it_moves_the_tag_pins_env_names_not_a_hardcoded_stable():
    """promote.sh retags whatever TARGET_TAG says, and the nodes follow OS_TAG: passing the pins
    value is what stops the promoted tag and the followed tag from drifting apart."""
    job = _promote_job()
    assert 'TARGET_TAG="$OS_TAG"' in job
    assert "source configs/pins.env" in job
