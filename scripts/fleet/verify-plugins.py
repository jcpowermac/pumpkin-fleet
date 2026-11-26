#!/usr/bin/python3
"""Prove the plugins this image declares are in the data dir and did not fail to load.

Run on the node by fleet-verify.service, a system oneshot that starts after the node's
container unit and writes its verdict to /run/fleet-verify.status. It exists because
podman's `AutoUpdate=registry` does not roll back a container that starts and then fails
to load a plugin: `Restart=always` crash-loops it, the port HEALTHCHECK still passes, and
nothing else notices the bump. This check is the detector.

Two deliberate limits:

- It exits 0 even when it reports failures. A failing oneshot leaves the node in a
  failed-unit state, which is exactly what the reboot gate then trips over; the human
  reads /run/fleet-verify.status (and `tasks/verify.py`) instead of the exit code.
- It never touches /data and never restarts the container: it only reads.

Usage: verify-plugins.py --image papermc --data-dir /srv/plugins/data \\
           [--expected /etc/fleet/expected-plugins.papermc.txt] \\
           [--log-from "podman logs plugins"] [--status-file /run/fleet-verify.status]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_STATUS_FILE = "/run/fleet-verify.status"

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
    image = next(i for i in images if i["name"] == image_name)
    return {plugin["install_as"] for plugin in image.get("plugins", [])}


def missing_jars(expected: set[str], listing: list[str]) -> set[str]:
    """Declared jars that are not in the data dir. Anything else there is not ours."""
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


def default_log_command(data_dir: str) -> str:
    """The quadlet unit sets ContainerName=<node>; the data dir is /srv/<node>/data."""
    return f"podman logs {Path(data_dir).parent.name}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check this node's declared plugins loaded.")
    parser.add_argument("--image", required=True, help="image name in containers/images.json")
    parser.add_argument("--data-dir", required=True, help="the volume mounted at /data")
    parser.add_argument("--expected", help="default: /etc/fleet/expected-plugins.<image>.txt")
    parser.add_argument("--log-from", help="command printing the server log; default: podman logs")
    parser.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    args = parser.parse_args(argv)

    expected_path = Path(args.expected or f"/etc/fleet/expected-plugins.{args.image}.txt")
    expected = {line.strip() for line in expected_path.read_text().splitlines() if line.strip()}
    listing = os.listdir(args.data_dir)

    log_command = args.log_from or default_log_command(args.data_dir)
    run = subprocess.run(log_command, shell=True, capture_output=True, text=True, timeout=120)
    result = verdict(expected, listing, run.stdout)
    if run.returncode != 0:
        # "could not read the log" is not "loaded fine": say so instead of reporting ok.
        print(f"verify-plugins: `{log_command}` exited {run.returncode}", file=sys.stderr)
        result["failures"].append(f"log unreadable: `{log_command}` exited {run.returncode}")
        result["ok"] = False

    text = status_text(result)
    print(text, end="")
    try:
        Path(args.status_file).write_text(text)
    except OSError as error:  # e.g. run by hand as core, where /run is not writable
        print(f"verify-plugins: cannot write {args.status_file}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
