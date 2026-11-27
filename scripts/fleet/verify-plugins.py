#!/usr/bin/python3
"""Prove the plugins this image declares are in the data dir and did not fail to load.

Run on the node by fleet-verify.timer → fleet-verify.service, a system oneshot that writes
its verdict to /run/fleet-verify.status, and by hand after a deploy. It exists because
podman's `AutoUpdate=registry` does not roll back a container that starts and then fails to
load a plugin: `Restart=always` crash-loops it, the port HEALTHCHECK still passes, and
nothing else notices the bump. This check is the detector.

Two deliberate limits:

- It exits 0 even when it reports failures. A failing oneshot leaves the node in a
  failed-unit state, which is exactly what the reboot gate then trips over; the human
  reads /run/fleet-verify.status (and `tasks/verify.py`) instead of the exit code.
- It never touches /data and never restarts the container: it only reads.

Usage: verify-plugins.py --image papermc --container plugins --data-dir /srv/plugins/data \\
           [--expected /etc/fleet/expected-plugins.papermc.txt] \\
           [--log-from "podman logs plugins"] [--status-file /run/fleet-verify.status]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_STATUS_FILE = "/run/fleet-verify.status"

# The volume the quadlet unit mounts at /data; the entrypoints seed these inside it.
JAR_DIRS = ("plugins", "mods")

# Paper prints this once the world is up and every plugin has had its chance to enable.
STARTUP_COMPLETE = "Done ("

# shortcut: bounded wait for startup-complete; tighten against a real log in Task 10.
STARTUP_TIMEOUT = 120.0
POLL_SECONDS = 3.0

# shortcut: verdict keys off failure signatures plus jar presence, not Paper's
# per-plugin "Loading …" lines, whose exact wording is unpinned; Task 10 Step 4
# tightens the patterns against a real log.
FAILURE_SIGNATURES = (
    "Failed to load plugin",
    "Could not load plugin",
    "requires a newer version",
    "is not compatible",
    "NoClassDefFoundError",
    "Unable to hook into",
)


def expected_jars(manifest_path: Path, image_name: str) -> set[str]:
    """The canonical `install_as` names this image declares, from containers/images.json.

    Never a hand-written list and never an upstream filename: `install_as` is stable
    across versions, an upstream name changes with every bump.
    """
    images = json.loads(Path(manifest_path).read_text())
    image = next((i for i in images if i["name"] == image_name), None)
    if image is None:
        raise KeyError(f"{image_name!r} is not an image in {manifest_path}")
    jars = set()
    for plugin in image.get("plugins", []):
        if "install_as" not in plugin:
            raise KeyError(f"plugin {plugin.get('name', plugin)!r} in {image_name!r} has no install_as")
        jars.add(plugin["install_as"])
    return jars


def jar_dirs(volume: str) -> list[Path]:
    """The jar directories the entrypoints seed, whichever of them exist."""
    return [Path(volume) / name for name in JAR_DIRS if (Path(volume) / name).is_dir()]


def jar_listing(dirs: list[Path]) -> list[str]:
    """Every name in those directories, unioned: a node's image can change, and Paper
    reads plugins/ while Fabric reads mods/."""
    return [name for directory in dirs for name in os.listdir(directory)]


def missing_jars(expected: set[str], listing: list[str]) -> set[str]:
    """Declared jars that are not in the volume. Anything else there is not ours."""
    return set(expected) - set(listing)


def load_failures(log_text: str) -> list[str]:
    """The log lines carrying one of the failure signatures, in log order, deduped: a
    crash-looping container repeats the same stack trace on every restart."""
    matched = [
        line.strip()
        for line in log_text.splitlines()
        if any(signature in line for signature in FAILURE_SIGNATURES)
    ]
    return list(dict.fromkeys(matched))


def verdict(expected: set[str], listing: list[str], log: str) -> dict:
    missing = sorted(missing_jars(expected, listing))
    failures = load_failures(log)
    return {"missing": missing, "failures": failures, "ok": not missing and not failures}


def status_text(result: dict) -> str:
    """`ok`, or one line per problem — this is what /run/fleet-verify.status holds."""
    if result["ok"]:
        return "ok\n"
    return "".join(f"missing: {name}\n" for name in result["missing"]) + "".join(
        f"failure: {line}\n" for line in result["failures"]
    )


def default_log_command(container: str) -> str:
    """The quadlet unit sets ContainerName=<node>; `--container` carries that name."""
    return f"podman logs {container}"


def read_until_started(command: str, timeout: float) -> tuple[str, str | None]:
    """Run `command` until its output carries the startup-complete line, or until timeout.

    The deploy starts this check as soon as the port opens, which is well before plugins
    finish loading: evaluating the log then records a failing plugin as `ok`. Returns the
    log text and, when the command itself failed, a failure message.
    """
    deadline = time.monotonic() + timeout
    while True:
        run = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout + 60)
        if run.returncode != 0:
            error = f"log unreadable: `{command}` exited {run.returncode}"
        elif STARTUP_COMPLETE in run.stdout:
            return run.stdout, None
        else:
            # Not seeing the startup line is a finding of its own, not a clean log: a jar that
            # kills the JVM never gets there, and `verdict()` only reads the jars and the
            # failure signatures, so a dead server would otherwise write a persistent `ok`.
            error = (
                f"the server never printed {STARTUP_COMPLETE!r} within {timeout:g}s — "
                "it may be dead, so the declared jars could not be checked"
            )
        if time.monotonic() >= deadline:
            return run.stdout, error
        time.sleep(POLL_SECONDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check this node's declared plugins loaded.")
    parser.add_argument("--image", required=True, help="image name in containers/images.json")
    parser.add_argument("--data-dir", required=True, help="the volume the unit mounts at /data")
    parser.add_argument("--container", help="container name; required unless --log-from is given")
    parser.add_argument("--expected", help="default: /etc/fleet/expected-plugins.<image>.txt")
    parser.add_argument("--log-from", help="command printing the server log; default: podman logs")
    parser.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    parser.add_argument("--startup-timeout", type=float, default=STARTUP_TIMEOUT)
    args = parser.parse_args(argv)
    if not args.log_from and not args.container:
        parser.error("--container is required when --log-from is not given")

    expected_path = Path(args.expected or f"/etc/fleet/expected-plugins.{args.image}.txt")
    log_command = args.log_from or default_log_command(args.container)
    missing: list[str] = []
    failures: list[str] = []
    try:
        expected = {line.strip() for line in expected_path.read_text().splitlines() if line.strip()}
        dirs = jar_dirs(args.data_dir)
        log_text, log_error = read_until_started(log_command, args.startup_timeout)
    except (OSError, subprocess.SubprocessError) as error:
        # A traceback would exit 1 and write no status file at all — the opposite of this
        # script's contract. Say what went wrong in the verdict instead.
        failures.append(f"check failed: {error}")
    else:
        result = verdict(expected, jar_listing(dirs), log_text)
        missing, failures = result["missing"], result["failures"]
        if not dirs:
            failures.append(f"no plugins/ or mods/ under {args.data_dir}")
        if log_error:
            failures.append(log_error)
    result = {"missing": missing, "failures": failures, "ok": not missing and not failures}

    text = status_text(result)
    print(text, end="")
    try:
        Path(args.status_file).write_text(text)
    except OSError as error:  # e.g. run by hand as core, where /run is not writable
        print(f"verify-plugins: cannot write {args.status_file}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
