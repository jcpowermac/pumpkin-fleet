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


def run_cli(tmp_path, data_jars, log_text):
    data_dir = tmp_path / "srv" / "plugins" / "data"
    data_dir.mkdir(parents=True)
    for name in data_jars:
        (data_dir / name).write_bytes(b"jar")
    expected = tmp_path / "expected-plugins.papermc.txt"
    expected.write_text("\n".join(sorted(DECLARED)) + "\n")
    log = tmp_path / "paper.log"
    log.write_text(log_text)
    status = tmp_path / "fleet-verify.status"
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            str(VERIFY),
            "--image",
            "papermc",
            "--expected",
            str(expected),
            "--data-dir",
            str(data_dir),
            "--log-from",
            f"cat {log}",
            "--status-file",
            str(status),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out, status.read_text()


def test_status_file_reports_a_missing_jar_but_the_unit_still_exits_zero(tmp_path):
    """The ruling: a failing oneshot puts the node in a failed-unit state, which then
    confuses the reboot gate. The human sees /run/fleet-verify.status instead."""
    out, status = run_cli(tmp_path, ["LuckPerms.jar"], CLEAN)
    assert out.returncode == 0, out.stderr
    assert status.startswith("missing: "), status
    assert "ViaVersion.jar" in status


def test_status_file_says_ok_on_a_healthy_node_and_still_quotes_the_log(tmp_path):
    out, status = run_cli(tmp_path, sorted(DECLARED), CLEAN)
    assert out.returncode == 0, out.stderr
    assert status == "ok\n"
    assert "missing: " not in status


def test_status_file_names_the_failing_log_line(tmp_path):
    out, status = run_cli(tmp_path, sorted(DECLARED), FAILING)
    assert out.returncode == 0, out.stderr
    assert status.startswith("failure: "), status
    assert "Could not load plugin ViaBackwards" in status


def test_the_check_never_writes_to_the_data_dir(tmp_path):
    """The check reads /data; a verify step that changed the volume would be a deploy."""
    run_cli(tmp_path, ["LuckPerms.jar"], FAILING)
    data_dir = tmp_path / "srv" / "plugins" / "data"
    assert sorted(entry.name for entry in data_dir.iterdir()) == ["LuckPerms.jar"]


def test_default_log_command_is_podman_logs_for_this_node():
    """The quadlet unit sets ContainerName=<node> and the data dir is /srv/<node>/data,
    so the node name is already in the argument the unit passes."""
    assert verify_plugins.default_log_command("/srv/plugins/data") == "podman logs plugins"
