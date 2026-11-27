"""scripts/render_quadlet.py writes the text pyinfra pushes to each node.

Three things here are load-bearing and invisible in a run log: a server unit must
follow a *channel* tag and never `latest` (a `latest` server unit would run whatever
CI last built, canary discipline gone), and a quadlet unit cannot be `systemctl
enable`d at all — it is generated into a generator directory at every daemon-reload,
so boot persistence is the `[Install]` section the generator applies. The handover
shell is the one-time move from a `podman run` container to the unit that replaces
it, and it must never SIGKILL a loaded world.
"""
import os
import subprocess
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


def test_unit_never_starts_without_its_data_mount():
    """A boot with the zvol missing or renamed leaves no /srv/<node>/data mount; the
    container would bind-mount a plain directory on the disposable OS disk and Paper would
    generate a fresh world into it. The condition stops the unit instead."""
    text = unit()
    unit_section = text.split("[Container]")[0]
    assert "ConditionPathIsMountPoint=/srv/lobby/data" in unit_section


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


def handover_calls(
    tmp_path, *, unit_active: bool, container_exists: bool, container_running: bool = True
) -> list[str]:
    """Run the emitted handover shell under bash with stub podman/systemctl on PATH.

    Substring assertions on the command would still pass with the guard inverted to
    `is-active … || exit 0`, which hands over nothing or kills a live server; only the
    calls the shell actually makes tell the branches apart.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "calls.log"
    (stub / "systemctl").write_text(
        '#!/bin/sh\n'
        'echo "systemctl $*" >> "$CALL_LOG"\n'
        'case "$*" in\n'
        '  "--user is-active --quiet lobby.service") [ "$UNIT_ACTIVE" = 1 ] ;;\n'
        '  *) exit 1 ;;\n'
        'esac\n'
    )
    # `podman stop` exits non-zero for a container that is already `exited`; the stub models
    # that, so the `|| true` the handover needs is visible in the call log and the exit code.
    (stub / "podman").write_text(
        '#!/bin/sh\n'
        'echo "podman $*" >> "$CALL_LOG"\n'
        'case "$1" in\n'
        '  container) [ "$CONTAINER_EXISTS" = 1 ] ;;\n'
        '  stop) [ "$CONTAINER_RUNNING" = 1 ] ;;\n'
        '  *) : ;;\n'
        'esac\n'
    )
    for script in stub.iterdir():
        script.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub}:{os.environ['PATH']}",
        "CALL_LOG": str(log),
        "UNIT_ACTIVE": "1" if unit_active else "0",
        "CONTAINER_EXISTS": "1" if container_exists else "0",
        "CONTAINER_RUNNING": "1" if container_running else "0",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", render_quadlet.handover_command("lobby")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_handover_leaves_the_quadlet_owned_container_alone(tmp_path):
    """Unit active: not one podman call — the unit already owns that container."""
    calls = handover_calls(tmp_path, unit_active=True, container_exists=True)
    assert calls == ["systemctl --user is-active --quiet lobby.service"], calls


def test_handover_stops_a_foreign_container_gracefully_before_removing_it(tmp_path):
    """`podman rm -f` would SIGKILL a loaded world; the old deploy did exactly that."""
    calls = handover_calls(tmp_path, unit_active=False, container_exists=True)
    assert calls == [
        "systemctl --user is-active --quiet lobby.service",
        "podman container exists lobby",
        "podman stop --time 60 lobby",
        "podman rm lobby",
    ], calls
    assert "-f" not in calls[-2]


def test_handover_still_removes_a_container_that_is_already_stopped(tmp_path):
    """`podman stop` exits non-zero for a container already in state `exited` — the first
    deploy after a crash or a hand stop. `stop && rm` skipped the rm and failed the deploy
    with the old container still holding the name the unit needs."""
    calls = handover_calls(tmp_path, unit_active=False, container_exists=True, container_running=False)
    assert calls == [
        "systemctl --user is-active --quiet lobby.service",
        "podman container exists lobby",
        "podman stop --time 60 lobby",
        "podman rm lobby",
    ], calls
    # No -f anywhere: `podman rm` alone must still refuse a running container.
    assert "-f" not in " ".join(calls)


def test_handover_is_a_no_op_when_no_container_exists(tmp_path):
    """Second run of a fresh deploy: the container is gone, nothing to stop."""
    calls = handover_calls(tmp_path, unit_active=False, container_exists=False)
    assert calls == [
        "systemctl --user is-active --quiet lobby.service",
        "podman container exists lobby",
    ], calls


def test_verify_unit_is_a_oneshot_after_the_container_and_reads_the_log_as_core():
    """`After=<node>.service` plus a oneshot: the check runs after the server is up, and
    it never restarts anything (a restart would drop a loaded world)."""
    _, text = render_quadlet.render_verify_units("lobby", "papermc", "/srv/lobby/data")
    assert "Type=oneshot" in text
    assert "After=lobby.service" in text
    # ...and said out loud to be inert, so the next reader does not trust it for ordering.
    assert "# No effect" in text
    assert (
        "ExecStart=/usr/bin/python3 /usr/local/lib/fleet/verify-plugins.py "
        "--image papermc --data-dir /srv/lobby/data --container lobby" in text
    )
    # The container is rootless under core; root's own podman store is empty.
    assert 'sudo -u core env XDG_RUNTIME_DIR=/run/user/1000 podman logs lobby' in text
    assert "Restart=" not in text
    assert "podman restart" not in text
    assert "podman rm" not in text


def test_verify_unit_is_pointed_at_the_volume_root_the_entrypoint_seeds():
    """The volume is what the quadlet unit mounts at /data, and the entrypoint seeds
    /data/plugins (Paper) or /data/mods (Fabric). Handing the check a jar directory makes
    every healthy node report every jar missing, so it gets the root and reads both."""
    _, text = render_quadlet.render_verify_units("lobby", "papermc", "/srv/lobby/data")
    assert "--data-dir /srv/lobby/data " in text
    assert "/srv/lobby/data/plugins" not in text
    assert "/srv/lobby/data/mods" not in text


def test_verify_timer_rechecks_regularly_and_after_boot():
    """`podman-auto-update.timer` replaces the container inside core's user manager, so
    without a timer nobody re-runs the check and /run/fleet-verify.status keeps the `ok`
    of the last deploy — a stale pass for exactly the failure this check exists for."""
    timer, _ = render_quadlet.render_verify_units("lobby", "papermc", "/srv/lobby/data")
    assert "OnBootSec=60" in timer
    assert "OnUnitActiveSec=15min" in timer
    assert "WantedBy=timers.target" in timer
    # A timer activates the service of the same name, so fleet-verify.timer re-runs
    # fleet-verify.service with no [Timer] Unit= line to get wrong.
    assert "ExecStart" not in timer


def test_canary_is_chosen_by_node_name_and_nothing_else():
    """This is the only thing that puts a node on the pre-promotion tag."""
    assert render_quadlet.channel_for("plugins") == "stable-next"
    assert render_quadlet.channel_for("lobby") == "stable"
    assert render_quadlet.channel_for("vanilla") == "stable"
    assert render_quadlet.channel_for("neoforge") == "stable"
