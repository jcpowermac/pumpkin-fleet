#!/usr/bin/env python3
"""Gate the change to containers/images.json before it can move a published tag.

`head` is the manifest CI is about to build, `base` the manifest it already built —
containers/images.json at the commit being replaced. A version tag is never re-pushed
(containers/README.md rule 1), so whatever the build consumes has to come with a new
version: the image `args`, and the plugin triples `url install_as sha256` that CI hands
to the plugins stage as PLUGIN_TRIPLETS. An unchanged manifest is deliberately clean —
comparing a manifest with itself is how a re-run and an annotation-only edit both pass.

changed() is the build matrix: the head entries whose identity or version actually
moved, plus images with no base entry at all. An untouched image is skipped rather than
rebuilt, because rebuilding it publishes a new digest under a tag ghcr.io already has.

  ci_gate.py --base <path> --head <path> [--matrix]   exit 1 on any GateError
  --matrix prints the JSON array of images to build (one line, for the Actions matrix)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# One spelling of the plugin-revision convention, shared with the resolver that writes it.
from resolve_plugins import bump_revision

HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"-p\d+$")


@dataclass
class GateError:
    image: str
    message: str


def _plugins(image: dict) -> list[dict]:
    """A plugin is a dict. A bare string is the pre-resolver shape (images.json on main
    before Task 2), which carries no artifact fields — the base side of a gate compares it.
    """
    return [p if isinstance(p, dict) else {"name": p} for p in image.get("plugins") or []]


def build_identity(image: dict) -> tuple:
    """The bytes the build produces: server args plus the PLUGIN_TRIPLETS it installs.

    A plugin's name/slug/source/mc/deps/note are resolver input and annotation; only the
    triple reaches the image, so editing them must not read as a new artifact.
    """
    triples = tuple(
        sorted((p.get("url") or "", p.get("install_as") or "", p.get("sha256") or "")
               for p in _plugins(image))
    )
    return tuple(sorted((image.get("args") or {}).items())), triples


def gate(base: list[dict], head: list[dict]) -> list[GateError]:
    built = {i["name"]: i for i in base}
    errors: list[GateError] = []
    for image in head:
        name, version = image["name"], image.get("version") or ""
        plugins = _plugins(image)

        for plugin in plugins:
            sha = plugin.get("sha256") or ""
            if not HEX64.match(sha):
                errors.append(GateError(
                    name,
                    f"plugin {plugin.get('name')} pins sha256 {sha!r}: expected 64 hex, "
                    "the digest ADD --checksum verifies (run scripts/resolve_plugins.py --bump)",
                ))
            if not plugin.get("url"):
                errors.append(GateError(
                    name,
                    f"plugin {plugin.get('name')} has no url: PLUGIN_TRIPLETS cannot be "
                    "derived from it, so CI cannot ship it",
                ))

        if plugins and not REVISION.search(version):
            errors.append(GateError(
                name,
                f"{name}:{version} ships {len(plugins)} plugin(s) with no -pN revision in "
                "the version; the plugin set is part of what the tag names",
            ))

        previous = built.get(name)
        if previous is not None and build_identity(previous) != build_identity(image):
            if previous.get("version") == version:
                errors.append(GateError(
                    name,
                    f"{name}:{version} changes args or plugins with an unchanged version, "
                    f"and that tag is already published — it has to become {bump_revision(version)}",
                ))
    return errors


def changed(base: list[dict], head: list[dict]) -> list[dict]:
    built = {i["name"]: i for i in base}
    out = []
    for image in head:
        previous = built.get(image["name"])
        if previous is None or (previous.get("version"), build_identity(previous)) != (
            image.get("version"),
            build_identity(image),
        ):
            out.append(image)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate containers/images.json changes.")
    parser.add_argument("--base", type=Path, required=True, help="manifest CI already published")
    parser.add_argument("--head", type=Path, required=True, help="manifest CI is about to build")
    parser.add_argument("--matrix", action="store_true", help="print the images to build as JSON")
    args = parser.parse_args(argv)

    base = json.loads(args.base.read_text())
    head = json.loads(args.head.read_text())
    errors = gate(base, head)
    for error in errors:
        print(f"{error.image}: {error.message}", file=sys.stderr)
    if errors:
        return 1
    if args.matrix:
        print(json.dumps(changed(base, head), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
