#!/usr/bin/python3
"""Reboot into the staged OS deployment — unless somebody is playing.

Run by fleet-reboot.service, which fleet-reboot.timer fires inside the randomised
maintenance window. The OS update itself is automatic (AutomaticUpdatePolicy=stage),
so this is the half that decides *when* the new deployment becomes the running one.

A server that is not answering does NOT block the reboot: a dead container is not
hosting players, and blocking would wedge the node on a crashed server until a human
noticed. The cost of that choice: if the container is down because it crashed, the
reboot takes the crash evidence (the journal's podman logs survive, the container
does not) with it, and nothing here restarts or reports the server — the reboot is
allowed, the container comes back with the unit, and the crash is only visible as
`podman ps -a` state plus logs from before the reboot.

Usage: reboot-gate.py [--dry-run]   # --dry-run prints the decision, never reboots
"""
import argparse
import json
import subprocess
import sys

MC_PING = "/usr/local/lib/fleet/mc-ping.py"


def ping() -> dict | None:
    """The status ping's JSON, or None when the server does not answer in time."""
    try:
        out = subprocess.run(
            [sys.executable, MC_PING, "127.0.0.1", "25565"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def should_reboot(ping: dict | None) -> bool:
    if ping is None:
        print("reboot allowed: the server is not answering, so nobody is playing")
        return True
    # A payload without a player count must not raise: an exception here stops every
    # automatic OS-update reboot on the node, silently.
    online = (ping.get("players") or {}).get("online", 0)
    if online:
        print(f"skip: {online} players online")
        return False
    print("reboot allowed: no players online")
    return True


def main() -> None:
    # argparse, not a substring scan: `--dryrun` / `-n` / `--dry-run=true` used to fall
    # through to the reboot. An unknown flag now exits 2 before anything is probed.
    parser = argparse.ArgumentParser(description="Reboot into the staged OS deployment.")
    parser.add_argument("--dry-run", action="store_true", help="print the decision, never reboot")
    args = parser.parse_args()
    if not should_reboot(ping()):
        return
    if args.dry_run:
        return
    subprocess.run(["systemctl", "reboot"], check=True)


if __name__ == "__main__":
    main()
