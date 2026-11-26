"""containers/images.json is the plugin manifest every later task reads.

Contract being protected (docs/plan.md, fleet-plugin-gitops): an image is
`name`/`version`/`args`/`plugins`; a plugin seed is `name`/`slug`/`source`/`mc`/
`install_as`, and the resolver (Task 2) is what adds `version`/`url`/`sha256`.
"""
import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGES = {i["name"]: i for i in json.loads((REPO / "containers" / "images.json").read_text())}
SOURCES = {"hangar", "modrinth", "github", "manual"}
# Exact seed values from the plan: bumping an image's plugin list bumps its version.
VERSIONS = {
    "papermc": "26.3-258-p1",
    "fabric": "26.3-0.20.1-p1",
    "velocity": "4.3.1-45",
    "neoforge": "26.3.0.115",
}
# Task 2 fills these in; their absence here is what makes the manifest reviewable.
UNRESOLVED = ("version", "url", "sha256")


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


def test_unresolved_fields_absent():
    for image_name, plugin in _plugins():
        for field in UNRESOLVED:
            assert field not in plugin, (image_name, plugin["name"], field)
