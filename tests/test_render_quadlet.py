"""scripts/render_quadlet.py writes the text pyinfra pushes to each node.

Three things here are load-bearing and invisible in a run log: a server unit must
follow a *channel* tag and never `latest` (a `latest` server unit would run whatever
CI last built, canary discipline gone), and a quadlet unit cannot be `systemctl
enable`d at all — it is generated into a generator directory at every daemon-reload,
so boot persistence is the `[Install]` section the generator applies. The handover
shell is the one-time move from a `podman run` container to the unit that replaces
it, and it must never SIGKILL a loaded world.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# scripts/ holds plain scripts, not a package; import it the way pyinfra does.
sys.path.insert(0, str(REPO / "scripts"))
import render_quadlet  # noqa: E402

IMAGE = "ghcr.io/jcpowermac/pumpkin-fleet/papermc"


def unit(channel="stable", node="lobby"):
    return render_quadlet.render_container_unit(
        node, IMAGE, channel, "4g", "2G", f"/srv/{node}/data"
    )


def test_unit_carries_image_channel_and_volume():
    text = unit()
    assert f"Image={IMAGE}:stable" in text
    assert "Volume=/srv/lobby/data:/data:Z" in text
    assert "ContainerName=lobby" in text


def test_unit_canary_uses_stable_next():
    assert f"Image={IMAGE}:stable-next" in unit("stable-next")
    assert f"\nImage={IMAGE}:stable\n" not in unit("stable-next")


def test_unit_opts_into_registry_auto_update():
    text = unit()
    assert "AutoUpdate=registry" in text
    assert "Restart=always" in text
    # podman's grace window for a stop, then systemd's outer bound on the whole stop.
    assert "PodmanArgs=--stop-timeout 60" in text
    assert "TimeoutStopSec=90" in text
    assert "Memory=4g" in text
    assert "Environment=JAVA_MEMORY=2G" in text
    assert "Network=host" in text


def test_unit_never_names_latest():
    """No server channel is `latest`; the canary is a real tag so a rollback is a repoint."""
    for channel in ("stable", "stable-next"):
        assert ":latest" not in unit(channel)


def test_unit_is_a_rootless_user_unit_with_install_section():
    """`systemctl --user enable` is rejected for quadlet units (they are generated), so
    boot persistence has to come from the `[Install]` section the generator applies."""
    text = unit()
    assert "[Install]\nWantedBy=default.target" in text
    assert "RunInit=true" in text, "the old `podman run --init` behaviour must survive"


def test_reboot_timer_is_randomised():
    timer, _ = render_quadlet.render_reboot_units("lobby", "Sun *-*-* 05:00")
    assert "OnCalendar=Sun *-*-* 05:00" in timer
    assert "RandomizedDelaySec=" in timer, "four nodes must not reboot together"
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_reboot_service_is_a_oneshot_running_the_gate():
    _, service = render_quadlet.render_reboot_units("lobby", "Sun *-*-* 05:00")
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/fleet/reboot-gate.py" in service
    assert "lobby" in service


def test_handover_stops_a_foreign_container_gracefully_before_removing_it():
    """`podman rm -f` would SIGKILL a loaded world; the old deploy did exactly that."""
    command = render_quadlet.handover_command("lobby")
    assert "podman rm -f" not in command
    assert "podman stop --time 60 lobby" in command
    assert command.index("podman stop") < command.index("podman rm lobby")


def test_handover_leaves_the_quadlet_owned_container_alone():
    command = render_quadlet.handover_command("lobby")
    assert command.index("is-active --quiet lobby.service") < command.index("podman stop")
    assert command.index("is-active --quiet lobby.service") < command.index("podman rm lobby")
