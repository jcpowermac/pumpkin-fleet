#!/usr/bin/env python3
"""Resolve the plugin declarations in containers/images.json.

Task 1 declares each plugin as name/slug/source/mc/install_as. This fills in what a build
needs — `version`, `url`, `sha256`, `deps` — by asking the upstream APIs for the newest
release compatible with the pinned MC version, and can rewrite the manifest for a human to
review as a bump PR.

  --check    resolve and report; exit 2 when a bump or an incompatibility needs a human
  --dry-run  print the PR body, write nothing
  --bump     rewrite containers/images.json in place, moving an image's -pN when a plugin's
             version, url or sha256 changed or the plugin set grew — a version tag is never
             re-pushed with different bytes
  --only IMAGE   resolve one image

sha256 is the reason this exists: `ADD --checksum` accepts sha256 only, Hangar publishes it,
Modrinth (sha1/sha512) and GitHub (nothing) do not, so those artifacts are downloaded once
and the computed digest is what gets committed.

Tests replace the two I/O seams `fetch_json` (API JSON) and `sha256_of_url` (artifact
download); nothing else touches the network.

Two signatures differ from the plan's sketch deliberately: `resolve_github` takes the image's
loader, because a GitHub release declares neither loader nor MC version and repos like OPanel ship
one jar per loader+MC (39 assets in one release), so picking an asset without the loader would put
a forge jar on a Fabric node; and `resolve_manifest` takes an optional `problems` list so a
per-image call can surface incompatibilities without a module-level accumulator.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "containers" / "images.json"
USER_AGENT = "pumpkin-fleet-bumpbot/1.0 (+https://github.com/jcpowermac/pumpkin-fleet)"
HANGAR_SLEEP = 0.26  # Hangar allows 20 requests / 5 s
# Image -> the loader its plugins must declare. A new image name goes here.
LOADERS = {"papermc": "paper", "fabric": "fabric", "neoforge": "neoforge", "velocity": "velocity"}
# Modrinth tags a Bukkit-API plugin with every loader that can load it.
LOADER_FAMILIES = {"paper": {"paper", "bukkit", "spigot", "folia", "purpur"}}


@dataclasses.dataclass
class Plugin:
    name: str
    slug: str
    source: str
    version: str | None = None
    url: str | None = None
    sha256: str | None = None
    install_as: str = ""
    mc: list[str] = dataclasses.field(default_factory=list)
    # Recorded upstream dependencies: {"slug", "name", "required"}.
    deps: list[dict] = dataclasses.field(default_factory=list)
    note: str = ""
    stale: bool = False


class NoCandidate(Exception):
    """No upstream release is compatible with the pinned MC version, or a manual artifact
    no longer matches its committed sha256. The manifest entry stays exactly as committed
    and the reason (newest version, what it supports) is reported."""


def _headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}  # Modrinth requires an identifying UA
    if "api.github.com" in url:
        headers["Accept"] = "application/vnd.github+json"
    return headers


def fetch_json(url: str) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_headers(url))) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sha256_of_url(url: str, dest: Path) -> str:
    """Download once to `dest`, hashing as it streams."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers=_headers(url))
    with urllib.request.urlopen(request) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 16):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


_last_hangar = 0.0


def _hangar_json(url: str) -> Any:
    global _last_hangar
    wait = HANGAR_SLEEP - (time.monotonic() - _last_hangar)
    if wait > 0:
        time.sleep(wait)
    _last_hangar = time.monotonic()
    return fetch_json(url)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (0,)


def resolve_hangar(p: Plugin, mc: str) -> Plugin:
    # The fleet hosts its Hangar plugins on Paper, so the platform is fixed here.
    base = f"https://hangar.papermc.io/api/v1/projects/{p.slug}/versions"
    try:
        releases = _hangar_releases(_hangar_json(
            f"{base}?platform=PAPER&platformVersion={mc}&channel=Release&limit=10"))
        if not releases:
            newest = _hangar_releases(_hangar_json(f"{base}?platform=PAPER&channel=Release&limit=5"))
            top = newest[0]["name"] if newest else "none"
            raise NoCandidate(f"{p.name} ({p.slug}): no PAPER Release build for MC {mc}; "
                              f"newest upstream Release is {top}")
    except urllib.error.HTTPError as exc:
        raise NoCandidate(f"{p.name} ({p.slug}): Hangar lookup failed with HTTP {exc.code}") from exc

    top = releases[0]
    paper = top["downloads"]["PAPER"]
    resolved = dataclasses.replace(p, version=top["name"], url=paper["downloadUrl"],
                                   sha256=paper["fileInfo"]["sha256Hash"])
    resolved.deps = _hangar_deps(p, top.get("pluginDependencies", {}).get("PAPER", []))
    return resolved


def _hangar_releases(data: dict) -> list[dict]:
    """Newest first. Hangar answers every channel, and a Snapshot build is never installed."""
    releases = [v for v in data["result"]
                if v["channel"]["name"] == "Release" and v.get("downloads", {}).get("PAPER", {}).get("downloadUrl")]
    return sorted(releases, key=lambda v: v["createdAt"], reverse=True)


def _hangar_deps(p: Plugin, declared: list[dict]) -> list[dict]:
    """Take each dependency's slug from its own project lookup: a dependency must be the
    canonical project, never something a search would have found."""
    deps = []
    for declared_dep in declared:
        try:
            project = _hangar_json(
                f"https://hangar.papermc.io/api/v1/projects/{declared_dep['projectId']}")
        except urllib.error.HTTPError as exc:
            raise NoCandidate(f"{p.name}: Hangar dependency {declared_dep['name']} lookup "
                              f"failed with HTTP {exc.code}") from exc
        deps.append({"slug": project["namespace"]["slug"].lower(), "name": project["name"],
                     "required": declared_dep["required"]})
    # shortcut: Hangar does not order pluginDependencies, so an unsorted list makes the same
    # plugin set look like a manifest change on the next run. Sorting by slug keeps --check
    # and --bump idempotent; upgrade to Hangar's own ordering if it ever becomes meaningful.
    return sorted(deps, key=lambda dep: dep["slug"])


def dep_install_as(dep: dict) -> str:
    """The jar name a required dependency lands under."""
    return f"{dep['name']}.jar"


def expand_hangar_deps(p: Plugin) -> list[Plugin]:
    """p.deps as plugin seeds, ready to be declared. `p` must already be resolved."""
    return [Plugin(name=dep["name"], slug=dep["slug"], source="hangar",
                   install_as=dep_install_as(dep), mc=list(p.mc)) for dep in p.deps]


def resolve_modrinth(p: Plugin, mc: str, loader: str) -> Plugin:
    try:
        versions = fetch_json(f"https://api.modrinth.com/v2/project/{p.slug}/version")
    except urllib.error.HTTPError as exc:
        raise NoCandidate(f"{p.name} ({p.slug}): Modrinth lookup failed with HTTP {exc.code}") from exc

    family = LOADER_FAMILIES.get(loader, {loader})
    matches = [v for v in versions if family & set(v["loaders"])]
    compatible = [v for v in matches if mc in v["game_versions"]]
    if not compatible:
        newest = max(matches or versions, key=lambda v: v["date_published"], default=None)
        if newest is None:
            raise NoCandidate(f"{p.name} ({p.slug}): upstream has no releases")
        supported = max(newest["game_versions"], key=_version_key)
        raise NoCandidate(f"{p.name} ({p.slug}): no {loader} build for MC {mc}; newest "
                          f"{newest['version_number']} supports {supported}")

    top = max(compatible, key=lambda v: v["date_published"])
    filing = next((f for f in top["files"] if f["primary"]), top["files"][0])
    with tempfile.TemporaryDirectory() as tmp:
        digest = sha256_of_url(filing["url"], Path(tmp) / filing["filename"])
    return dataclasses.replace(p, version=top["version_number"], url=filing["url"], sha256=digest)


def resolve_github(p: Plugin, loader: str) -> Plugin:
    """A GitHub release declares no MC version or loader, so the asset name is the evidence.
    Repos like OPanel ship one jar per loader+MC: pick this loader's newest MC build and flag
    it as stale when it is not the pinned one, rather than installing another loader's jar."""
    try:
        release = fetch_json(f"https://api.github.com/repos/{p.slug}/releases/latest")
    except urllib.error.HTTPError as exc:
        raise NoCandidate(f"{p.name} ({p.slug}): GitHub lookup failed with HTTP {exc.code}") from exc

    jars = [a for a in release["assets"] if a["name"].endswith(".jar")]
    tagged = [a for a in jars if loader in a["name"].lower()]
    pool = tagged or [a for a in jars if not any(l in a["name"].lower() for l in LOADERS)]
    if not pool:
        raise NoCandidate(f"{p.name} ({p.slug}): release {release['tag_name']} ships no .jar asset")

    pinned = p.mc[0] if p.mc else ""
    # shortcut: these names put the MC version before the build version; if a repo names its
    # assets differently, match the pinned mc in the tag/release name instead.
    best = max(pool, key=lambda a: _version_key(_mc_token(a["name"]) or "0"))
    asset = next((a for a in pool if pinned and pinned in a["name"]), best)
    resolved = dataclasses.replace(p, version=release["tag_name"], url=asset["browser_download_url"])
    if pinned and pinned not in f"{asset['name']} {release['tag_name']} {release['name']}":
        resolved.stale = True
        resolved.note = (f"upstream ships no {pinned} {loader} build; newest is {asset['name']} "
                         f"(MC {_mc_token(asset['name']) or 'unknown'})")
    with tempfile.TemporaryDirectory() as tmp:
        resolved.sha256 = sha256_of_url(resolved.url, Path(tmp) / asset["name"])
    return resolved


def _mc_token(name: str) -> str | None:
    match = re.search(r"\d+\.\d+(?:\.\d+)?", name)
    return match.group(0) if match else None


def resolve_manual(p: Plugin) -> Plugin:
    """A human recorded the artifact: re-verify the committed sha256, never move the version."""
    if not (p.url and p.sha256):
        raise NoCandidate(f"{p.name} ({p.slug}): a manual entry needs a committed url and sha256")
    with tempfile.TemporaryDirectory() as tmp:
        digest = sha256_of_url(p.url, Path(tmp) / p.install_as)
    if digest != p.sha256:
        raise NoCandidate(f"{p.name}: sha256 mismatch — {p.url} is {digest}, manifest says {p.sha256}")
    return p


def resolve_plugin(p: Plugin, loader: str) -> Plugin:
    if p.source == "manual":
        return resolve_manual(p)
    if not p.mc:
        raise NoCandidate(f"{p.name} ({p.slug}): no MC version pinned")
    if p.source == "hangar":
        return resolve_hangar(p, p.mc[0])
    if p.source == "modrinth":
        return resolve_modrinth(p, p.mc[0], loader)
    if p.source == "github":
        return resolve_github(p, loader)
    raise NoCandidate(f"{p.name}: unknown source {p.source!r}")


def resolve_manifest(manifest: list[dict], image: dict, problems: list[str] | None = None) -> list[dict]:
    """Resolve `image`'s plugin list; `manifest` is the manifest it came from. An entry whose
    upstream has nothing compatible is returned exactly as committed and its reason is
    appended to `problems`. Required upstream dependencies become new entries."""
    loader = LOADERS[image["name"]]
    problems = [] if problems is None else problems
    pending = list(image.get("plugins", []))
    declared = {seed["install_as"]: seed["slug"] for seed in pending}
    out: list[dict] = []
    index = 0
    while index < len(pending):
        seed = pending[index]
        index += 1
        plugin = Plugin(**{key: value for key, value in seed.items()
                           if key in Plugin.__dataclass_fields__})
        try:
            resolved = resolve_plugin(plugin, loader)
        except NoCandidate as exc:
            problems.append(f"{image['name']}/{seed['name']}: {exc}")
            out.append(dict(seed))
            continue

        entry = dict(seed)
        entry["version"] = resolved.version
        entry["url"] = resolved.url
        entry["sha256"] = resolved.sha256
        entry["deps"] = resolved.deps
        if resolved.note:
            entry["note"] = resolved.note
        if resolved.stale:
            entry["stale"] = True
        out.append(entry)

        for dep in (expand_hangar_deps(resolved) if resolved.source == "hangar" else []):
            required = next((d["required"] for d in resolved.deps if d["slug"] == dep.slug), False)
            # Optional upstream dependencies are reported in `deps`, never installed.
            if required and dep.slug not in declared.values():
                declared[dep.install_as] = dep.slug
                pending.append({"name": dep.name, "slug": dep.slug, "source": dep.source,
                                "mc": list(seed.get("mc", [])), "install_as": dep.install_as})
    return out


def bump_revision(version: str) -> str:
    match = re.match(r"^(.*)-p(\d+)$", version)
    return f"{match.group(1)}-p{int(match.group(2)) + 1}" if match else f"{version}-p1"


def mc_verdict(entry: dict) -> str:
    """Whether the resolved artifact installs on the pinned MC. A GitHub release carries no MC
    metadata, so there the answer is the asset/note the resolver had to read; a manual entry is
    a human's promise and is not verified against the MC at all."""
    mc = "/".join(entry.get("mc") or []) or "no MC"
    if entry.get("source") == "manual":
        return f"{mc} manual (unverified)"
    return f"{mc} stale" if entry.get("stale") else f"{mc} compatible"


def plan(manifest: list[dict], only: str | None = None
         ) -> tuple[list[dict], list[str], list[tuple[str, str, str]], list[str]]:
    """Resolve the manifest in memory: fill the artifact fields, and move an image's -pN when a
    resolved plugin's artifact identity (version, url or sha256) moved or the plugin set grew.
    Returns (changes, problems, revised, stale); nothing is written."""
    changes: list[dict] = []
    problems: list[str] = []
    revised: list[tuple[str, str, str]] = []
    stale: list[str] = []
    for image in manifest:
        if (only and image["name"] != only) or not image.get("plugins"):
            continue
        before = {plugin["install_as"]: plugin for plugin in image["plugins"]}
        image["plugins"] = resolve_manifest(manifest, image, problems)
        added = {entry["install_as"] for entry in image["plugins"]} - set(before)
        moved = False
        for entry in image["plugins"]:
            # A stale pin resolves to what it already says, so it is in no change row: the body
            # has to report it separately or a reviewer never learns the pin is behind.
            if entry.get("stale"):
                stale.append(f"{image['name']}/{entry['name']}: pinned {entry.get('version')} — "
                             f"{entry.get('note') or 'upstream has no build for the pinned MC'}")
            old = before.get(entry["install_as"], {})
            if old == entry:
                continue
            changes.append({"image": image["name"], "name": entry["name"], "source": entry["source"],
                            "old": old.get("version"), "new": entry.get("version"),
                            "url": entry.get("url"), "sha256": entry.get("sha256"),
                            "note": entry.get("note", ""), "mc": mc_verdict(entry),
                            "added_deps": [dep["name"] for dep in entry.get("deps") or []
                                           if dep.get("required") and dep_install_as(dep) in added]})
            # A first resolution only fills empty fields. After that, any move in artifact
            # identity — version, url or sha256 — is a new revision, because an image tag is
            # defined by the bytes it installs and is never re-pushed with different content.
            if old.get("version") and (old.get("version"), old.get("url"), old.get("sha256")) != (
                    entry.get("version"), entry.get("url"), entry.get("sha256")):
                moved = True
        if moved or len(image["plugins"]) != len(before):
            old_version = image["version"]
            image["version"] = bump_revision(old_version)
            revised.append((image["name"], old_version, image["version"]))
    return changes, problems, revised, stale


def pr_body(changes: list[dict], problems: list[str], revised: list[tuple[str, str, str]],
            stale: list[str]) -> str:
    """The bump PR's body: what moved, on which MC, what it drags in — then what the resolver
    refused to move (a stale pin, or an upstream it could not use)."""
    lines = ["Resolve plugin versions, urls and sha256s in `containers/images.json`.", ""]
    for image, old_version, new_version in revised:
        lines.append(f"- `{image}`: `{old_version}` -> `{new_version}` (plugin artifacts changed)")
    if revised:
        lines.append("")
    if changes:
        lines += ["| image | plugin | source | version | mc | deps added | sha256 |",
                  "|---|---|---|---|---|---|---|"]
        for change in changes:
            if change["old"]:
                version = f"{change['old']} -> {change['new']}"
            else:
                version = f"(new) {change['new']}"
            if change["note"]:
                version += f" — {change['note']}"
            deps = ", ".join(f"+{name}" for name in change["added_deps"])
            lines.append(f"| {change['image']} | {change['name']} | {change['source']} | {version} "
                         f"| {change['mc']} | {deps} | {change['sha256']} |")
        lines += ["", "Artifacts:"]
        for change in changes:
            lines.append(f"- {change['image']}/{change['name']}: {change['url']}")
    if stale:
        lines += ["", "## Staleness — upstream has nothing newer that installs on the pinned MC",
                  "The pin stays where it is: this PR does not move it."]
        lines += [f"- {entry}" for entry in stale]
    if problems:
        lines += ["", "## Left alone (no compatible release, or a manual artifact that no longer matches)"]
        lines += [f"- {problem}" for problem in problems]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve containers/images.json plugin pins.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="resolve and report only; exit 2 when anything is actionable")
    mode.add_argument("--dry-run", action="store_true", help="print the PR body, write nothing")
    mode.add_argument("--bump", action="store_true", help="rewrite containers/images.json in place")
    parser.add_argument("--only", metavar="IMAGE", help="resolve one image")
    args = parser.parse_args(argv)

    manifest = json.loads(MANIFEST.read_text())
    changes, problems, revised, stale = plan(manifest, args.only)
    print(pr_body(changes, problems, revised, stale), end="")
    if args.bump:
        if changes:
            MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
            print(f"wrote {MANIFEST}: {len(changes)} plugin(s) resolved")
        else:
            print(f"{MANIFEST} is already resolved; nothing written")
        return 2 if problems else 0
    return 2 if changes or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
