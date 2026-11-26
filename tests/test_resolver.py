"""Resolver tests — every upstream answer comes from tests/fixtures/*.json, recorded
live with curl on 2026-11-26 and trimmed to the fields the parser reads.

The two seams a test replaces are `resolve_plugins.fetch_json` (API JSON) and
`resolve_plugins.sha256_of_url` (artifact download); nothing here touches the network.
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parent.parent
# scripts/ holds plain scripts, not a package; import it the way CI invokes it.
sys.path.insert(0, str(REPO / "scripts"))
import resolve_plugins as rp  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    """Hangar spacing (20 req / 5 s) is exercised by the live run, not by fixture tests."""
    monkeypatch.setattr(rp, "HANGAR_SLEEP", 0.0)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test reaches the internet. file:// stays open because sha256_of_url is tested
    against a local jar, and an artifact download is a seam every other test replaces."""
    real_urlopen = urllib.request.urlopen

    def guarded(request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        if not url.startswith("file://"):
            raise AssertionError(f"test tried to fetch {url}")
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


def _fetch(responses):
    """Stand-in for fetch_json: first (URL substring, body) pair that matches wins."""

    def fetch(url):
        for needle, body in responses.items():
            if needle in url:
                return body
        raise AssertionError(f"test has no recorded response for {url}")

    return fetch


def _via_seed():
    return rp.Plugin(name="ViaVersion", slug="viaversion", source="hangar",
                     install_as="ViaVersion.jar", mc=["26.3"])


def _hangar_fetch():
    return _fetch({
        "projects/viaversion/versions": _fixture("hangar_viaversion.json"),
        "projects/12": _fixture("hangar_dep_projects.json")["12"],
        "projects/112": _fixture("hangar_dep_projects.json")["112"],
    })


# The fixture is the real Hangar answer for project=viaversion, platform=PAPER,
# platformVersion=26.3 without the channel filter: two Snapshots newer than one Release.
def test_hangar_picks_release_channel_and_compatible_build(monkeypatch):
    fx = _fixture("hangar_viaversion.json")
    monkeypatch.setattr(rp, "fetch_json", _hangar_fetch())
    releases = [v for v in fx["result"] if v["channel"]["name"] == "Release"]
    snapshots = [v for v in fx["result"] if v["channel"]["name"] != "Release"]
    assert len(releases) == 1 and len(snapshots) == 2
    # Sorting on date alone would pick a Snapshot: the Release is the oldest of the three.
    assert max(v["createdAt"] for v in snapshots) > releases[0]["createdAt"]

    p = rp.resolve_hangar(_via_seed(), "26.3")
    assert p.version == "5.12.3" == releases[0]["name"]
    assert p.sha256 == releases[0]["downloads"]["PAPER"]["fileInfo"]["sha256Hash"]
    assert p.url == releases[0]["downloads"]["PAPER"]["downloadUrl"]
    assert urlparse(p.url).hostname == "hangarcdn.papermc.io"
    # Seed fields survive resolution: a bump rewrites, it does not re-declare.
    assert (p.name, p.slug, p.source, p.install_as, p.mc) == (
        "ViaVersion", "viaversion", "hangar", "ViaVersion.jar", ["26.3"])


def test_hangar_reports_no_compatible_build(monkeypatch):
    # Verified live: Hangar answers with an empty result for a platformVersion the
    # project has no build for, so the resolver must report why instead of crashing.
    empty = {"pagination": {"count": 0}, "result": []}
    monkeypatch.setattr(rp, "fetch_json", _fetch({
        "platformVersion=26.3": empty,
        "projects/viaversion/versions": _fixture("hangar_viaversion.json"),
    }))
    with pytest.raises(rp.NoCandidate) as exc:
        rp.resolve_hangar(_via_seed(), "26.3")
    reason = str(exc.value)
    assert "26.3" in reason and "5.12.3" in reason


def test_hangar_dependency_closure(monkeypatch):
    monkeypatch.setattr(rp, "fetch_json", _hangar_fetch())
    p = rp.resolve_hangar(_via_seed(), "26.3")
    deps = rp.expand_hangar_deps(p)
    assert {d.slug for d in deps} == {"viabackwards", "viarewind"}
    assert all(d.source == "hangar" and d.mc == ["26.3"] for d in deps)
    # Hangar marks ViaVersion's deps optional; reported in `deps`, never installed.
    assert {d["slug"]: d["required"] for d in p.deps} == {"viabackwards": False, "viarewind": False}

    seed = {"name": "ViaVersion", "slug": "viaversion", "source": "hangar",
            "mc": ["26.3"], "install_as": "ViaVersion.jar"}
    image = {"name": "papermc", "version": "26.3-258-p1", "plugins": [dict(seed)]}
    plugins = rp.resolve_manifest([image], image)
    assert plugins[0]["deps"] == p.deps
    assert [q["install_as"] for q in plugins] == ["ViaVersion.jar"]  # optional deps not added


# Hangar answers pluginDependencies with projectIds; the slug must come from the project
# lookup so a dependency is the canonical project, never a search hit.
def test_hangar_dependency_slugs_come_from_the_project_lookup(monkeypatch):
    monkeypatch.setattr(rp, "fetch_json", _hangar_fetch())
    p = rp.resolve_hangar(_via_seed(), "26.3")
    assert {d["name"] for d in p.deps} == {"ViaBackwards", "ViaRewind"}
    assert {d["slug"] for d in p.deps} == {"viabackwards", "viarewind"}


def test_dependency_order_is_stable_across_runs(monkeypatch):
    """Hangar answers pluginDependencies in an unstable order (seen live: ViaRewind before
    ViaBackwards on one call, the other way on the next). An unsorted list makes an unchanged
    plugin set look like a change, so deps are recorded sorted by slug."""
    body = _synthetic_hangar(False)
    body["result"][0]["pluginDependencies"]["PAPER"] = [
        {"name": "ViaRewind", "projectId": 112, "required": False},
        {"name": "ViaBackwards", "projectId": 12, "required": False},
    ]
    monkeypatch.setattr(rp, "fetch_json", _fetch({
        "versions": body,
        "projects/12": _fixture("hangar_dep_projects.json")["12"],
        "projects/112": _fixture("hangar_dep_projects.json")["112"],
    }))
    seed = rp.Plugin(name="Foo", slug="foo", source="hangar", install_as="Foo.jar", mc=["26.3"])
    p = rp.resolve_hangar(seed, "26.3")
    assert [dep["slug"] for dep in p.deps] == ["viabackwards", "viarewind"]


def _synthetic_hangar(dep_required):
    """Minimal Hangar versions body with one required/optional dependency."""
    return {"pagination": {"count": 1}, "result": [{
        "name": "1.0", "createdAt": "2026-11-01T00:00:00Z", "channel": {"name": "Release"},
        "downloads": {"PAPER": {
            "downloadUrl": "https://hangarcdn.papermc.io/plugins/Foo/Foo/versions/1.0/PAPER/Foo-1.0.jar",
            "fileInfo": {"name": "Foo-1.0.jar", "sha256Hash": "a" * 64}}},
        "pluginDependencies": {"PAPER": [
            {"name": "ViaBackwards", "projectId": 12, "required": dep_required}]}}]}


def test_required_dependency_becomes_a_manifest_entry(monkeypatch):
    monkeypatch.setattr(rp, "fetch_json", _fetch({
        "versions": _synthetic_hangar(True),
        "projects/12": _fixture("hangar_dep_projects.json")["12"],
    }))
    seed = {"name": "Foo", "slug": "foo", "source": "hangar", "mc": ["26.3"], "install_as": "Foo.jar"}
    image = {"name": "papermc", "version": "26.3-258-p1", "plugins": [dict(seed)]}
    plugins = rp.resolve_manifest([image], image)
    assert [q["install_as"] for q in plugins] == ["Foo.jar", "ViaBackwards.jar"]
    dep = plugins[1]
    assert (dep["slug"], dep["source"], dep["version"], dep["mc"]) == ("viabackwards", "hangar", "1.0", ["26.3"])
    # A second required dep already declared is not added twice, and the closure terminates.
    again = rp.resolve_manifest([image], image)
    assert [q["install_as"] for q in again] == ["Foo.jar", "ViaBackwards.jar"]


def test_a_declared_dependency_is_never_added_again(monkeypatch):
    """The fleet declares ViaBackwards by hand (containers/images.json), and Hangar reports
    ViaVersion as a *required* dependency of ViaBackwards: the closure must not re-add a plugin
    the manifest already names."""
    monkeypatch.setattr(rp, "fetch_json", _fetch({
        "projects/viaversion/versions": _fixture("hangar_viaversion.json"),
        "projects/viabackwards/versions": _synthetic_hangar(False),
        "projects/12": _fixture("hangar_dep_projects.json")["12"],
        "projects/112": _fixture("hangar_dep_projects.json")["112"],
    }))
    via = {"name": "ViaVersion", "slug": "viaversion", "source": "hangar", "mc": ["26.3"],
           "install_as": "ViaVersion.jar"}
    backwards = {"name": "ViaBackwards", "slug": "viabackwards", "source": "hangar",
                 "mc": ["26.3"], "install_as": "ViaBackwards.jar"}
    image = {"name": "papermc", "version": "26.3-258-p2", "plugins": [dict(via), dict(backwards)]}
    plugins = rp.resolve_manifest([image], image)
    assert [q["install_as"] for q in plugins] == ["ViaVersion.jar", "ViaBackwards.jar"]
    assert plugins[1]["version"] == "1.0", "resolved from its own slug, not via ViaVersion's deps"
    assert {d["slug"] for d in plugins[0]["deps"]} == {"viabackwards", "viarewind"}


def test_modrinth_filters_by_loader_and_game_version(monkeypatch):
    fx = _fixture("modrinth_luckperms.json")
    monkeypatch.setattr(rp, "fetch_json", _fetch({"project/luckperms/version": fx}))
    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: "e" * 64)
    seed = rp.Plugin(name="LuckPerms", slug="luckperms", source="modrinth",
                     install_as="LuckPerms.jar", mc=["26.3"])

    paper = rp.resolve_modrinth(seed, "26.3", "paper")
    assert paper.version == "v5.5.71-bukkit"
    assert paper.url.endswith("/LuckPerms-Bukkit-5.5.71.jar")
    assert urlparse(paper.url).hostname == "cdn.modrinth.com"
    # The two newest builds in the fixture declare 26.3 but are NeoForge artifacts,
    # and the velocity build is the wrong loader too: all three must lose to 5.5.71-bukkit.
    assert [v["version_number"] for v in fx[:4]] == [
        "v5.5.87-neoforge", "v5.5.85-neoforge", "v5.5.85-fabric", "v5.5.71-velocity"]
    # The next bukkit-family build (5.5.53) stops at 26.2, so it is not a candidate.
    assert fx[9]["version_number"] == "v5.5.53-bukkit" and "26.3" not in fx[9]["game_versions"]

    fabric = rp.resolve_modrinth(seed, "26.3", "fabric")
    assert fabric.version == "v5.5.85-fabric"
    assert fabric.url.endswith("/LuckPerms-Fabric-5.5.85.jar")


def test_modrinth_computes_sha256_by_download(monkeypatch):
    fx = _fixture("modrinth_luckperms.json")
    monkeypatch.setattr(rp, "fetch_json", _fetch({"project/luckperms/version": fx}))
    downloaded = []
    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: downloaded.append(url) or ("f" * 64))
    seed = rp.Plugin(name="LuckPerms", slug="luckperms", source="modrinth",
                     install_as="LuckPerms.jar", mc=["26.3"])
    p = rp.resolve_modrinth(seed, "26.3", "paper")
    # Modrinth publishes sha1/sha512 only and ADD --checksum wants sha256, so it is computed.
    assert p.sha256 == "f" * 64
    assert downloaded == [p.url]
    upstream = next(f["hashes"] for v in fx for f in v["files"] if f["primary"])
    assert p.sha256 not in upstream.values()


def test_sha256_of_url_downloads_to_dest_and_hashes(tmp_path):
    jar = tmp_path / "LuckPerms-Bukkit-5.5.71.jar"
    jar.write_bytes(b"plugin bytes")
    dest = tmp_path / "downloaded.jar"
    assert rp.sha256_of_url(jar.as_uri(), dest) == hashlib.sha256(b"plugin bytes").hexdigest()
    assert dest.read_bytes() == b"plugin bytes"


def test_modrinth_rejects_incompatible_upstream(monkeypatch):
    """EssentialsX: the newest release supports 26.1.2, the fleet pins Paper 26.3."""
    fx = _fixture("modrinth_essentialsx.json")
    monkeypatch.setattr(rp, "fetch_json", _fetch({"project/essentialsx/version": fx}))
    seed = {"name": "EssentialsX", "slug": "essentialsx", "source": "modrinth",
            "mc": ["26.3"], "install_as": "EssentialsX.jar"}
    plugin = rp.Plugin(**seed)
    with pytest.raises(rp.NoCandidate) as exc:
        rp.resolve_modrinth(plugin, "26.3", "paper")
    reason = str(exc.value)
    assert "2.22.0" in reason and "26.1.2" in reason and "26.3" in reason
    assert fx[0]["version_number"] == "2.22.0" and "26.3" not in fx[0]["game_versions"]

    image = {"name": "papermc", "version": "26.3-258-p1", "plugins": [dict(seed)]}
    problems = []
    plugins = rp.resolve_manifest([image], image, problems)
    assert plugins == [seed], "an incompatible plugin must leave the manifest entry untouched"
    assert problems and "EssentialsX" in problems[0]


def test_manual_never_upgrades_but_reverifies(monkeypatch):
    seed = {"name": "Vault", "slug": "MilkBowl/Vault", "source": "manual", "mc": ["26.3"],
            "install_as": "Vault.jar", "version": "1.7.3",
            "url": "https://github.com/MilkBowl/Vault/releases/download/1.7.3/Vault.jar",
            "sha256": "ab" * 32}
    image = {"name": "papermc", "version": "26.3-258-p1", "plugins": [dict(seed)]}
    # A resolved entry always carries its dependency list, even when upstream declares none.
    resolved = dict(seed, deps=[])
    asked = []
    monkeypatch.setattr(rp, "fetch_json", lambda url: asked.append(url) or {})
    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: seed["sha256"])
    assert rp.resolve_manifest([image], image) == [resolved]
    assert asked == [], "a manual entry must not ask upstream for a newer version"

    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: "cd" * 32)
    problems = []
    plugins = rp.resolve_manifest([image], image, problems)
    assert plugins == [seed], "a mismatch is reported, never rewritten: the entry stays as committed"
    assert problems and "mismatch" in problems[0]


def test_github_prefers_the_asset_for_the_loader_and_pinned_mc(monkeypatch):
    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: "e" * 64)
    monkeypatch.setattr(rp, "fetch_json", _fetch({"repos/FabricMC/fabric": _fixture("github_fabric.json")}))
    seed = rp.Plugin(name="fabric-api", slug="FabricMC/fabric", source="github",
                     install_as="fabric-api.jar", mc=["26.3"])
    p = rp.resolve_github(seed, "fabric")
    assert p.version == "0.167.0+26.3"
    assert p.url.endswith("fabric-api-0.167.0%2B26.3.jar")
    assert urlparse(p.url).hostname == "github.com"
    assert not p.stale

    # OPanel ships one jar per loader+MC and has never shipped a fabric 26.3 build:
    # the fleet runs the newest fabric build (26.1) and says so, it does not pick a
    # forge/neoforge jar or fail the whole image.
    monkeypatch.setattr(rp, "fetch_json", _fetch({"repos/opanel-mc/opanel": _fixture("github_opanel.json")}))
    mod = rp.Plugin(name="opanel", slug="opanel-mc/opanel", source="github",
                    install_as="opanel-fabric.jar", mc=["26.3"])
    o = rp.resolve_github(mod, "fabric")
    assert o.url.endswith("/opanel-fabric-26.1-build-2.2.11.jar")
    assert o.stale and "26.1" in o.note and "26.3" in o.note


def _manifest_with_via(version, sha="b" * 64):
    return [{"name": "papermc", "version": version, "plugins": [
        {"name": "ViaVersion", "slug": "viaversion", "source": "hangar", "mc": ["26.3"],
         "install_as": "ViaVersion.jar", "version": "5.12.2",
         "url": "https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.12.2/PAPER/ViaVersion-5.12.2.jar",
         "sha256": sha, "deps": []}]}]


def _papermc_only(monkeypatch, tmp_path):
    path = tmp_path / "images.json"
    monkeypatch.setattr(rp, "MANIFEST", path)
    monkeypatch.setattr(rp, "fetch_json", _hangar_fetch())
    return path


def test_bump_rewrites_p_revision(monkeypatch, tmp_path):
    path = _papermc_only(monkeypatch, tmp_path)
    seed_manifest = _manifest_with_via("26.3-258-p1")
    path.write_text(json.dumps(seed_manifest, indent=2) + "\n")
    assert rp.main(["--bump"]) == 0
    out = json.loads(path.read_text())
    assert out[0]["version"] == "26.3-258-p2", "a changed plugin version must move the plugin revision"
    assert out[0]["plugins"][0]["version"] == "5.12.3"
    assert out[0]["plugins"][0]["sha256"] == (
        "8dec6b62c7ec4f355e59175fc8eaeb825719ae8d308723617097dcaab7073531")
    assert list(out[0]["plugins"][0]) == list(seed_manifest[0]["plugins"][0]), "key order preserved"
    assert path.read_text().endswith("\n")


def test_bump_is_a_noop_when_nothing_changed(monkeypatch, tmp_path):
    path = _papermc_only(monkeypatch, tmp_path)
    resolved = [{"name": "papermc", "version": "26.3-258-p1", "plugins": [
        {"name": "ViaVersion", "slug": "viaversion", "source": "hangar", "mc": ["26.3"],
         "install_as": "ViaVersion.jar", "version": "5.12.3",
         "url": "https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.12.3/PAPER/ViaVersion-5.12.3.jar",
         "sha256": "8dec6b62c7ec4f355e59175fc8eaeb825719ae8d308723617097dcaab7073531",
         "deps": [{"slug": "viabackwards", "name": "ViaBackwards", "required": False},
                  {"slug": "viarewind", "name": "ViaRewind", "required": False}]}]}]
    before = json.dumps(resolved, indent=2) + "\n"
    path.write_text(before)
    assert rp.main(["--bump"]) == 0
    assert path.read_text() == before, "an up-to-date manifest must not be rewritten or re-versioned"


def test_only_restricts_the_resolution(monkeypatch, tmp_path):
    path = tmp_path / "images.json"
    monkeypatch.setattr(rp, "MANIFEST", path)
    asked = []
    hangar = _hangar_fetch()

    def fetch(url):
        asked.append(url)
        return hangar(url)

    monkeypatch.setattr(rp, "fetch_json", fetch)
    manifest = [{"name": "papermc", "version": "26.3-258-p1", "plugins": [
        {"name": "ViaVersion", "slug": "viaversion", "source": "hangar", "mc": ["26.3"],
         "install_as": "ViaVersion.jar"}]},
        {"name": "fabric", "version": "26.3-0.20.1-p1", "plugins": [
            {"name": "LuckPerms", "slug": "luckperms", "source": "modrinth", "mc": ["26.3"],
             "install_as": "LuckPerms.jar"}]}]
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    assert rp.main(["--check", "--only", "papermc"]) == 2
    assert any("projects/viaversion" in url for url in asked)
    assert not any("luckperms" in url for url in asked), "--only must not resolve the other image"


def test_a_reuploaded_asset_with_the_same_tag_bumps_the_revision(monkeypatch, tmp_path):
    """GitHub lets a release asset be deleted and re-uploaded under the same tag, so
    `tag_name`, `browser_download_url` and the resolved version are all identical while the bytes
    differ. That is a new plugin revision: writing the new sha256 without moving -pN would have
    Task 3 re-push an image tag that already exists, with different content."""
    path = tmp_path / "images.json"
    monkeypatch.setattr(rp, "MANIFEST", path)
    release = _fixture("github_fabric.json")
    monkeypatch.setattr(rp, "fetch_json", _fetch({"repos/FabricMC/fabric": release}))
    monkeypatch.setattr(rp, "sha256_of_url", lambda url, dest: "b" * 64)  # the re-uploaded asset
    committed = [{"name": "fabric", "version": "26.3-0.20.1-p1", "plugins": [
        {"name": "fabric-api", "slug": "FabricMC/fabric", "source": "github", "mc": ["26.3"],
         "install_as": "fabric-api.jar", "version": "0.167.0+26.3",
         "url": release["assets"][0]["browser_download_url"], "sha256": "a" * 64, "deps": []}]}]
    path.write_text(json.dumps(committed, indent=2) + "\n")
    assert rp.main(["--bump"]) == 0
    out = json.loads(path.read_text())
    assert out[0]["plugins"][0]["version"] == "0.167.0+26.3", "upstream did not publish a new version"
    assert out[0]["plugins"][0]["url"] == committed[0]["plugins"][0]["url"]
    assert out[0]["plugins"][0]["sha256"] == "b" * 64
    assert out[0]["version"] == "26.3-0.20.1-p2", "different bytes must mean a new plugin revision"


def test_dry_run_body_names_plugin_version_sha_and_source(monkeypatch, tmp_path, capsys):
    path = _papermc_only(monkeypatch, tmp_path)
    before = json.dumps(_manifest_with_via("26.3-258-p1"), indent=2) + "\n"
    path.write_text(before)
    assert rp.main(["--dry-run"]) == 2, "an available bump is actionable, so --check/--dry-run exit 2"
    body = capsys.readouterr().out
    assert "5.12.3" in body and "8dec6b62" in body and "hangar" in body
    assert "ViaVersion" in body
    assert path.read_text() == before, "--dry-run writes nothing"
    assert rp.main(["--check"]) == 2
    assert path.read_text() == before, "--check leaves the manifest alone"


def test_check_exits_2_for_an_incompatible_upstream(monkeypatch, tmp_path, capsys):
    path = tmp_path / "images.json"
    monkeypatch.setattr(rp, "MANIFEST", path)
    monkeypatch.setattr(rp, "fetch_json", _fetch({"project/essentialsx/version": _fixture("modrinth_essentialsx.json")}))
    seed = {"name": "EssentialsX", "slug": "essentialsx", "source": "modrinth",
            "mc": ["26.3"], "install_as": "EssentialsX.jar"}
    before = json.dumps([{"name": "papermc", "version": "26.3-258-p1", "plugins": [dict(seed)]}], indent=2) + "\n"
    path.write_text(before)
    assert rp.main(["--check"]) == 2
    assert "2.22.0" in capsys.readouterr().out
    assert path.read_text() == before
