"""containers/images.json is the plugin manifest every later task reads.

Contract being protected (docs/plan.md, fleet-plugin-gitops): an image is
`name`/`version`/`args`/`plugins`; a plugin declares `name`/`slug`/`source`/`mc`/
`install_as` and carries the `version`/`url`/`sha256` that scripts/resolve_plugins.py
resolved from that declaration. A hand-edited artifact field here is a pin the build
will not agree with.
"""
import ast
import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
IMAGES = {i["name"]: i for i in json.loads((REPO / "containers" / "images.json").read_text())}
SOURCES = {"hangar", "modrinth", "github", "manual"}
# Exact seed values from the plan: bumping an image's plugin list bumps its version.
VERSIONS = {
    "papermc": "26.3-258-p2",
    "fabric": "26.3-0.20.1-p1",
    "velocity": "4.3.1-45",
    "neoforge": "26.3.0.115",
}
# What the resolver (Task 2) writes into every declared plugin.
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_HOSTS = {"hangar.papermc.io", "hangarcdn.papermc.io", "cdn.modrinth.com", "github.com"}


def _workloads():
    """tasks/gameserver.py WORKLOADS as data: importing it would run the pyinfra task."""
    tree = ast.parse((REPO / "tasks" / "gameserver.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "WORKLOADS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("WORKLOADS not found in tasks/gameserver.py")


def _plugins():
    for image in IMAGES.values():
        for plugin in image.get("plugins", []):
            yield image["name"], plugin


def test_no_pumpkin_anywhere():
    assert "pumpkin" not in IMAGES
    assert "pumpkin" not in {w[0] for w in _workloads().values()}
    # Every node's image must exist in the manifest, or the deploy picks a dead pin.
    assert {w[0] for w in _workloads().values()} <= set(IMAGES)


def test_plugin_seed_shape():
    seen = 0
    for image_name, plugin in _plugins():
        seen += 1
        assert set(("name", "slug", "source", "mc", "install_as")) <= set(plugin), (image_name, plugin)
        assert plugin["source"] in SOURCES, (image_name, plugin)
        assert plugin["install_as"].endswith(".jar"), (image_name, plugin)
        assert isinstance(plugin["mc"], list) and all(isinstance(m, str) for m in plugin["mc"])
    assert seen, "no plugins declared at all"


def test_install_as_unique_per_image():
    for image_name, image in IMAGES.items():
        names = [p["install_as"] for p in image.get("plugins", [])]
        assert len(names) == len(set(names)), (image_name, names)


def test_plugin_images_carry_plugin_revision():
    assert set(IMAGES) == set(VERSIONS)
    for name, version in VERSIONS.items():
        assert IMAGES[name]["version"] == version, name


def test_resolved_fields_present():
    for image_name, plugin in _plugins():
        assert HEX64.match(plugin["sha256"]), (image_name, plugin["name"])
        host = urlparse(plugin["url"]).hostname
        assert host in ARTIFACT_HOSTS, (image_name, plugin["name"], host)
        assert plugin["version"], (image_name, plugin["name"])


def test_only_fabric_opens_the_opanel_panel():
    # The 3000/tcp firewall rule follows the image: OPanel is a fabric mod here, and
    # papermc must not pick it up by accident.
    declared = {name: {p["name"] for p in image.get("plugins", [])} for name, image in IMAGES.items()}
    assert "opanel" in declared["fabric"]
    assert "opanel" not in declared["papermc"]


def test_excluded_plugins_stay_excluded():
    excluded = {name: {e["name"] for e in image.get("excluded", [])} for name, image in IMAGES.items()}
    assert excluded["papermc"] == {"EssentialsX", "Vault"}
    for name, image in IMAGES.items():
        assert not excluded[name] & {p["name"] for p in image.get("plugins", [])}, name


def test_plugin_declaring_images_bake_them():
    # CI passes PLUGIN_TRIPLETS to every image, so the manifest and the Dockerfile have
    # to agree: an image with declared plugins but no plugins stage would ignore the arg
    # and ship a server with none of them, and an image without plugins must not carry
    # the stage, because the stage refuses an empty arg.
    for name, image in IMAGES.items():
        dockerfile = (REPO / "containers" / name / "Dockerfile").read_text()
        if image.get("plugins"):
            assert "AS plugins" in dockerfile and "PLUGIN_TRIPLETS" in dockerfile, name
        else:
            assert "PLUGIN_TRIPLETS" not in dockerfile, name


def test_papermc_plugin_set_is_exact():
    # ViaBackwards and ViaRewind are declared here rather than conjured by the resolver:
    # Hangar calls them optional dependencies of ViaVersion, which describes ViaVersion's
    # packaging, not what this fleet wants loaded (older clients need ViaBackwards).
    assert {p["name"] for p in IMAGES["papermc"]["plugins"]} == {
        "LuckPerms", "ViaVersion", "ViaBackwards", "ViaRewind"}
    assert {p["install_as"] for p in IMAGES["papermc"]["plugins"]} == {
        "LuckPerms.jar", "ViaVersion.jar", "ViaBackwards.jar", "ViaRewind.jar"}
