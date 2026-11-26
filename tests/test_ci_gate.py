"""scripts/ci_gate.py is the CI half of "a version tag is never re-pushed".

It compares the manifest CI is about to build (`head`) with the manifest it already
built and published (`base`, i.e. containers/images.json at the commit being replaced).
Anything the build consumes — the image `args` and the plugin triples `url install_as
sha256` that become PLUGIN_TRIPLETS — moving under an unchanged `version` is rejected,
and the changed set decides the build matrix so an untouched image is never rebuilt
under its own published tag.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# scripts/ holds plain scripts, not a package; import it the way CI invokes it.
sys.path.insert(0, str(REPO / "scripts"))
import ci_gate  # noqa: E402

LUCKPERMS_URL = "https://cdn.modrinth.com/data/Vebnzrzj/versions/b0mk8uS6/LuckPerms-Bukkit-5.5.71.jar"
LUCKPERMS_NEW_URL = "https://cdn.modrinth.com/data/Vebnzrzj/versions/DzQPkkXY/LuckPerms-Fabric-5.5.85.jar"
VIA_URL = "https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.12.3/PAPER/ViaVersion-5.12.3.jar"
SHA_A = "49cecb66fa1fd22a133039a490e9c1e5095a238e7cd66eb9d2a16fe6c897550d"
SHA_B = "18598fcb95a83f6723d8bc68a905500ed5ecac92e9909ce1f25409cdebe2df49"
SHA_C = "8dec6b62c7ec4f355e59175fc8eaeb825719ae8d308723617097dcaab7073531"


def _plugins():
    return [
        {
            "name": "LuckPerms",
            "slug": "luckperms",
            "source": "modrinth",
            "install_as": "LuckPerms.jar",
            "version": "v5.5.71-bukkit",
            "url": LUCKPERMS_URL,
            "sha256": SHA_A,
        }
    ]


def _image(**over) -> dict:
    image = {
        "name": "papermc",
        "version": "26.3-258-p1",
        "args": {"PAPER_VERSION": "26.3-258", "PAPER_SHA256": "c5df5fb9f6155e48380c67c6e2b2dc25"},
        "plugins": _plugins(),
    }
    image.update(over)
    return image


def _velocity() -> dict:
    return {
        "name": "velocity",
        "version": "4.3.1-45",
        "args": {"VELOCITY_VERSION": "4.3.1-45", "VELOCITY_SHA256": "0844bcabdcdb4f5b875906fd4c3365bc"},
        "plugins": [],
    }


def _plugin(**over) -> dict:
    plugin = _plugins()[0]
    plugin.update(over)
    return [plugin]


def _errors(base, head):
    return ci_gate.gate(base, head)


def test_plugin_change_without_version_bump_is_rejected():
    # LuckPerms moved to a newer release: new url and sha256, image version left alone.
    head = [_image(plugins=_plugin(version="v5.5.85-bukkit", url=LUCKPERMS_NEW_URL, sha256=SHA_B))]
    errors = _errors([_image()], head)
    assert [e.image for e in errors] == ["papermc"]
    assert "26.3-258-p1" in errors[0].message


def test_same_version_twice_is_rejected():
    # Review Focus 4's CI half: papermc:26.3-258-p1 is published once, so the same
    # version with a different plugin set must not be built again under it.
    grown = _plugins() + [{"name": "ViaVersion", "install_as": "ViaVersion.jar", "url": VIA_URL,
                           "version": "5.12.3", "sha256": SHA_C}]
    errors = _errors([_image()], [_image(plugins=grown)])
    assert len(errors) == 1 and errors[0].image == "papermc"
    assert "-p" in errors[0].message  # tells the human to move the plugin revision


def test_plugin_artifact_identity_change_without_version_bump_is_rejected():
    # A hand edit of sha256 alone, and of url alone, are the same breach as a new release.
    sha_only = _errors([_image()], [_image(plugins=_plugin(sha256=SHA_C))])
    url_only = _errors([_image()], [_image(plugins=_plugin(url=VIA_URL))])
    assert [e.image for e in sha_only] == ["papermc"]
    assert [e.image for e in url_only] == ["papermc"]


def test_server_arg_change_without_version_bump_is_rejected():
    # args are part of the same identity: PAPER_VERSION is the jar the image installs.
    args = {"PAPER_VERSION": "26.3-259", "PAPER_SHA256": "c5df5fb9f6155e48380c67c6e2b2dc25"}
    errors = _errors([_image()], [_image(args=args)])
    assert [e.image for e in errors] == ["papermc"]


def test_unresolved_plugin_entry_is_rejected():
    missing_sha = _errors([_image()], [_image(plugins=_plugin(sha256=None))])
    bad_sha = _errors([_image()], [_image(plugins=_plugin(sha256="deadbeef"))])
    missing_url = _errors([_image()], [_image(plugins=_plugin(url=None))])
    assert [e.message for e in missing_sha if e.image == "papermc"][0].startswith("plugin LuckPerms pins sha256")
    assert [e.message for e in bad_sha if e.image == "papermc"][0].startswith("plugin LuckPerms pins sha256")
    assert any("PLUGIN_TRIPLETS" in e.message for e in missing_url)


def test_plugin_images_need_a_plugin_revision():
    # 26.3-258 with plugins would read as "the plain server build" to anyone auditing a tag.
    errors = _errors([_image()], [_image(version="26.3-258")])
    assert [e.image for e in errors] == ["papermc"]
    assert "-p" in errors[0].message
    # An image that ships no plugins keeps the bare server version.
    assert _errors([_velocity()], [_velocity()]) == []


def test_clean_manifest_has_no_errors():
    committed = json.loads((REPO / "containers" / "images.json").read_text())
    assert ci_gate.gate(committed, committed) == []


def test_p_revision_bump_with_plugin_change_is_allowed():
    head = _image(
        version="26.3-258-p2",
        plugins=_plugin(version="v5.5.85-bukkit", url=LUCKPERMS_NEW_URL, sha256=SHA_B),
    )
    assert _errors([_image()], [head]) == []


def test_unchanged_images_are_skipped_from_the_build_matrix():
    head = _image(
        version="26.3-258-p2",
        plugins=_plugin(version="v5.5.85-bukkit", url=LUCKPERMS_NEW_URL, sha256=SHA_B),
    )
    base = [_image(), _velocity()]
    built = ci_gate.changed(base, [head, _velocity()])
    assert [i["name"] for i in built] == ["papermc"]


def test_a_new_image_is_built_even_when_the_rest_is_untouched():
    fresh = {"name": "fabric", "version": "26.3-0.20.1-p1", "args": {}, "plugins": _plugin()}
    built = ci_gate.changed([_image(), _velocity()], [_image(), _velocity(), fresh])
    assert [i["name"] for i in built] == ["fabric"]


def test_annotation_only_edits_never_rebuild_a_published_tag():
    # note/deps/name text is resolver input, not something the build consumes.
    annotated = _plugins()
    annotated[0]["note"] = "considered a newer release"
    annotated[0]["deps"] = [{"slug": "vault", "name": "Vault", "required": False}]
    built = ci_gate.changed([_image()], [_image(plugins=annotated)])
    assert built == []
    assert ci_gate.gate([_image()], [_image(plugins=annotated)]) == []


def test_a_legacy_base_manifest_does_not_crash_the_gate():
    # images.json on main before Task 2 declared plugins as bare names, so the first gated
    # merge compares against that shape.
    legacy = [_image(version="26.3-258", plugins=["LuckPerms"]), _velocity()]
    head = [_image(), _velocity()]
    assert ci_gate.gate(legacy, head) == []
    assert [i["name"] for i in ci_gate.changed(legacy, head)] == ["papermc"]


def test_cli_exits_1_and_names_the_breach(tmp_path, capsys):
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    base.write_text(json.dumps([_image()]))
    head.write_text(json.dumps([_image(plugins=_plugin(sha256=SHA_C))]))
    assert ci_gate.main(["--base", str(base), "--head", str(head)]) == 1
    err = capsys.readouterr().err
    assert "papermc" in err and "26.3-258-p1" in err


def test_cli_matrix_prints_only_the_images_to_build(tmp_path, capsys):
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    base.write_text(json.dumps([_image(), _velocity()]))
    moved = _image(
        version="26.3-258-p2",
        plugins=_plugin(version="v5.5.85-bukkit", url=LUCKPERMS_NEW_URL, sha256=SHA_B),
    )
    head.write_text(json.dumps([moved, _velocity()]))
    assert ci_gate.main(["--base", str(base), "--head", str(head), "--matrix"]) == 0
    assert [i["name"] for i in json.loads(capsys.readouterr().out)] == ["papermc"]
