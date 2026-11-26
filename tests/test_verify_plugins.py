"""scripts/fleet/verify-plugins.py proves the declared plugins are on the node.

podman's `AutoUpdate=registry` does not roll back a container that starts and then
fails to load its plugins — `Restart=always` just crash-loops it — so this check is
the only thing that notices a bad plugin bump. The declared set comes from
containers/images.json (never hand-maintained), and the node-side run must exit 0
even when it reports a failure: a failing oneshot leaves the node in a failed-unit
state, which is what the reboot gate then trips over. The failure is visible in
/run/fleet-verify.status, not in the exit code.
"""
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "fleet" / "verify-plugins.py"
MANIFEST = REPO / "containers" / "images.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("verify_plugins", VERIFY)
verify_plugins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_plugins)

CLEAN = (FIXTURES / "paper_log_clean.txt").read_text()
FAILING = (FIXTURES / "paper_log_plugin_failure.txt").read_text()
DECLARED = {"LuckPerms.jar", "ViaVersion.jar", "ViaBackwards.jar", "ViaRewind.jar"}


def test_expected_jars_come_from_the_manifest_as_canonical_names():
    """The list must be the manifest's `install_as` names, never a versioned filename:
    an upstream name changes with every bump and the check would go blind."""
    assert verify_plugins.expected_jars(MANIFEST, "papermc") == DECLARED
    assert "LuckPerms-Bukkit-5.5.71.jar" not in verify_plugins.expected_jars(MANIFEST, "papermc")


def test_image_without_declared_plugins_expects_nothing():
    """velocity and neoforge declare `"plugins": []`; an empty set must still be ok."""
    for image in ("velocity", "neoforge"):
        assert verify_plugins.expected_jars(MANIFEST, image) == set()


def test_an_unknown_image_says_so():
    with pytest.raises(KeyError, match="'pumpkin' is not an image in"):
        verify_plugins.expected_jars(MANIFEST, "pumpkin")


def test_a_plugin_without_install_as_says_so(tmp_path):
    manifest = tmp_path / "images.json"
    manifest.write_text('[{"name": "papermc", "plugins": [{"name": "LuckPerms"}]}]')
    with pytest.raises(KeyError, match="LuckPerms.*has no install_as"):
        verify_plugins.expected_jars(manifest, "papermc")


def test_missing_jar_is_reported():
    listing = ["LuckPerms.jar", "LuckPerms-Bukkit-5.5.71.jar", "luckperms", ".fleet-plugins"]
    assert verify_plugins.missing_jars({"LuckPerms.jar", "ViaVersion.jar"}, listing) == {
        "ViaVersion.jar"
    }


def test_clean_log_has_no_failures():
    assert verify_plugins.load_failures(CLEAN) == []


def test_plugin_load_failure_is_flagged():
    failures = verify_plugins.load_failures(FAILING)
    assert any("Failed to load plugin" in line or "Could not load plugin ViaBackwards" in line
               for line in failures), failures
    assert any("NoClassDefFoundError" in line for line in failures), failures


def test_dependency_failure_is_flagged():
    assert any("requires a newer version" in line for line in verify_plugins.load_failures(FAILING))


@pytest.mark.parametrize(
    "line",
    [
        "[12:00:06 ERROR]: [LuckPerms] Failed to load plugin LuckPerms v5.5.71",
        "[12:00:06 ERROR]: [ViaBackwards] Could not load plugin ViaBackwards v5.12.3",
        "[12:00:06 ERROR]: [ViaBackwards] requires a newer version of ViaVersion",
        "[12:00:06 ERROR]: [Vault] Vault is not compatible with this server version",
        "[12:00:07 ERROR]: java.lang.NoClassDefFoundError: org/bukkit/event/Listener",
        "[12:00:06 WARN]: [ViaVersion] Unable to hook into the server implementation",
    ],
)
def test_each_failure_signature_is_detected(line):
    """All six, one by one: a signature that silently stops matching is a blind spot."""
    assert verify_plugins.load_failures(line) == [line]


def test_a_crash_loop_reports_each_failure_once():
    """`Restart=always` plus a bad plugin repeats the same error on every restart; the
    status file is in /run, so it must not grow with the loop."""
    once = "[12:00:07 ERROR]: java.lang.NoClassDefFoundError: org/bukkit/event/Listener"
    assert verify_plugins.load_failures("\n".join([once] * 50)) == [once]


def test_benign_lookalike_is_not_a_failure():
    """A plugin name in an info line, and the word "compatible" in a plugin's own success
    message, are not failure signatures — the clean fixture carries both."""
    log = "\n".join(
        [
            "[12:00:06 INFO]: [LuckPerms] 4 plugins are compatible with this server version",
            "[12:00:07 INFO]: [ViaRewind] ViaRewind 4.2.0 is now loaded. Compatible with ViaVersion 5.12.3",
            "[12:00:07 INFO]: [ViaVersion] Loaded 4 plugin dependencies",
        ]
    )
    assert verify_plugins.load_failures(log) == []


def test_verdict_ok_when_present_and_clean():
    result = verify_plugins.verdict(DECLARED, sorted(DECLARED) + ["luckperms"], CLEAN)
    assert result == {"missing": [], "failures": [], "ok": True}


def test_verdict_is_not_ok_when_a_present_plugin_failed_to_load():
    """Jar presence alone is not proof: the bump that started this task's fire ships the
    jar and still fails, because the dependency it needs is older."""
    result = verify_plugins.verdict(DECLARED, sorted(DECLARED), FAILING)
    assert result["missing"] == []
    assert result["ok"] is False
    assert any("Could not load plugin ViaBackwards" in line for line in result["failures"])


def run_cli(
    tmp_path,
    jars,
    log_text,
    *,
    image="papermc",
    expected=None,
    jar_dir="plugins",
    volume=None,
    log_from=None,
    extra_args=(),
):
    """A volume shaped the way the entrypoint leaves it: <root>/plugins|mods, logs, eula."""
    volume = Path(volume) if volume else tmp_path / "srv" / "plugins" / "data"
    if jar_dir:
        (volume / jar_dir).mkdir(parents=True)
        for name in jars:
            (volume / jar_dir / name).write_bytes(b"jar")
    (volume / "logs").mkdir(exist_ok=True)
    (volume / "eula.txt").write_text("eula=true\n")
    expected_file = tmp_path / f"expected-plugins.{image}.txt"
    declared = DECLARED if expected is None else expected
    expected_file.write_text("".join(f"{name}\n" for name in sorted(declared)))
    log = tmp_path / "paper.log"
    log.write_text(log_text)
    status = tmp_path / "fleet-verify.status"
    argv = [
        sys.executable,
        "-I",
        str(VERIFY),
        "--image",
        image,
        "--container",
        "plugins",
        "--expected",
        str(expected_file),
        "--data-dir",
        str(volume),
        "--log-from",
        log_from or f"cat {log}",
        "--status-file",
        str(status),
    ]
    out = subprocess.run(argv + list(extra_args), capture_output=True, text=True, timeout=120)
    return out, status.read_text() if status.exists() else None


def test_a_volume_shaped_tree_is_ok(tmp_path):
    """The regression the review caught: the volume root is what the unit mounts at /data,
    and the entrypoint seeds /data/plugins. Reading the root itself made every healthy
    node report every declared jar missing, so the gate could never be green."""
    out, status = run_cli(tmp_path, sorted(DECLARED), CLEAN)
    assert out.returncode == 0, out.stderr
    assert status == "ok\n", status


def test_a_fabric_volume_reads_mods(tmp_path):
    """Fabric seeds /data/mods, and a node can be redeployed with another image: both
    directories are read, so the check survives an image change."""
    jars = ["fabric-api.jar", "opanel-fabric.jar", "LuckPerms.jar"]
    out, status = run_cli(tmp_path, jars, CLEAN, image="fabric", expected=set(jars), jar_dir="mods")
    assert out.returncode == 0, out.stderr
    assert status == "ok\n", status


def test_a_volume_with_neither_plugins_nor_mods_is_a_failure(tmp_path):
    volume = tmp_path / "srv" / "plugins" / "data"
    volume.mkdir(parents=True)
    (volume / "eula.txt").write_text("eula=true\n")
    out, status = run_cli(tmp_path, [], CLEAN, volume=volume, jar_dir=None)
    assert out.returncode == 0, out.stderr
    assert "failure: no plugins/ or mods/ under" in status, status
    assert "missing: ViaVersion.jar" in status, status


def test_an_empty_expected_set_is_ok(tmp_path):
    """velocity and neoforge declare no plugins; an image that ships none must not look
    broken just because its volume holds only server state."""
    out, status = run_cli(tmp_path, [".fleet-plugins", "luckperms"], CLEAN, image="velocity", expected=set())
    assert out.returncode == 0, out.stderr
    assert status == "ok\n", status


def test_status_file_reports_a_missing_jar_but_the_unit_still_exits_zero(tmp_path):
    """The ruling: a failing oneshot puts the node in a failed-unit state, which then
    confuses the reboot gate. The human sees /run/fleet-verify.status instead."""
    out, status = run_cli(tmp_path, ["LuckPerms.jar"], CLEAN)
    assert out.returncode == 0, out.stderr
    assert status.startswith("missing: "), status
    assert "ViaVersion.jar" in status


def test_status_file_names_the_failing_log_line(tmp_path):
    out, status = run_cli(tmp_path, sorted(DECLARED), FAILING)
    assert out.returncode == 0, out.stderr
    assert status.startswith("failure: "), status
    assert "Could not load plugin ViaBackwards" in status


def test_a_log_without_the_startup_line_is_still_evaluated(tmp_path):
    """The deploy starts the check as soon as the port opens, which is before plugins
    finish loading: the check waits for `Done (` first, but the wait is bounded, so a log
    that never completes is evaluated as it stands rather than hanging."""
    truncated = FAILING.split("Done (")[0]
    started = time.monotonic()
    out, status = run_cli(
        tmp_path, sorted(DECLARED), truncated, extra_args=["--startup-timeout", "0"]
    )
    assert time.monotonic() - started < 30
    assert out.returncode == 0, out.stderr
    assert "Could not load plugin ViaBackwards" in status, status


def test_the_wait_polls_until_the_startup_line_appears(tmp_path, monkeypatch):
    """One read is not enough: the container's log is short when the port opens and fills
    in as plugins enable. The loop re-reads until `Done (` shows up."""
    counter = tmp_path / "calls"
    log_script = tmp_path / "log.sh"
    log_script.write_text(
        "#!/bin/sh\n"
        f'echo run >> {counter}\n'
        f'[ "$(wc -l < {counter})" -ge 2 ] && echo "[12:00:11 INFO]: Done (7.412s)! For help, type help"\n'
        "exit 0\n"
    )
    log_script.chmod(0o755)
    monkeypatch.setattr(verify_plugins, "POLL_SECONDS", 0.01)

    log_text, error = verify_plugins.read_until_started(str(log_script), timeout=5)

    assert error is None
    assert "Done (" in log_text
    assert counter.read_text().count("run") == 2, "it stopped polling too early"


def test_the_wait_ends_once_the_server_says_it_is_done(tmp_path):
    """A complete log is evaluated on the first read, not after the whole timeout."""
    started = time.monotonic()
    out, status = run_cli(tmp_path, sorted(DECLARED), CLEAN, extra_args=["--startup-timeout", "60"])
    assert time.monotonic() - started < 30
    assert status == "ok\n", status


def test_an_unreadable_expected_file_still_writes_a_status(tmp_path):
    """A traceback would exit 1 and write no status file at all — the opposite of this
    script's contract. Say it in the verdict instead."""
    out, status = run_cli(tmp_path, sorted(DECLARED), CLEAN, extra_args=["--expected", "/nonexistent"])
    assert out.returncode == 0, out.stderr
    assert status.startswith("failure: check failed:"), status


def test_an_unreadable_log_still_writes_a_status(tmp_path):
    out, status = run_cli(
        tmp_path, sorted(DECLARED), CLEAN, log_from="false",
        extra_args=["--startup-timeout", "0"],
    )
    assert out.returncode == 0, out.stderr
    assert status.startswith("failure: log unreadable:"), status


def test_the_container_is_a_required_argument_when_the_log_command_is_not_given(tmp_path):
    """The default log command is `podman logs <container>`; deriving it from the data dir
    would silently ask podman for a container named `data`."""
    out = subprocess.run(
        [sys.executable, "-I", str(VERIFY), "--image", "papermc", "--data-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 2, out.stderr
    assert "--container" in out.stderr


def test_the_check_never_writes_to_the_data_dir(tmp_path):
    """The check reads /data; a verify step that changed the volume would be a deploy."""
    run_cli(tmp_path, ["LuckPerms.jar"], FAILING)
    volume = tmp_path / "srv" / "plugins" / "data"
    assert sorted(entry.name for entry in volume.iterdir()) == ["eula.txt", "logs", "plugins"]
    assert sorted(entry.name for entry in (volume / "plugins").iterdir()) == ["LuckPerms.jar"]
