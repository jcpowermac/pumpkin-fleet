# Fleet Plugin GitOps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "merge a bump PR → the nodes run those plugins" true for the pumpkin-fleet Paper/Fabric images, with a canary node you can join with a Minecraft client, automatic OS updates, and a rebuild path that cannot destroy a world.

**Architecture:** Plugin jars are declared in `containers/images.json` and baked into the image by CI, which publishes an immutable version tag plus a `stable-next` canary tag; node 202 follows `stable-next`, a human joins it with a client, and a `workflow_dispatch` promote moves `stable`. Nodes run containers as quadlet units with `AutoUpdate=registry`, so a promoted channel converges on a timer without a human running pyinfra. World data lives on a host-owned ZFS volume attached as a raw path, which survives `qm destroy --purge`.

**Tech Stack:** Python 3.11 (`scripts/`, pytest), POSIX shell (container entrypoints, `scripts/fleet/`), Dockerfile + GitHub Actions (BuildKit, `build-args`), quadlet/podman 6.1.3 systemd user units, pyinfra 2 (`tasks/`), BlueBuild + rpm-ostree, Proxmox VE 9.2 (`qm`, ZFS `pool`).

**Spec:** `docs/superpowers/specs/2026-11-26-fleet-plugin-gitops-design.md` — read it with this plan; every decision below cites a section of it.

## Global Constraints

- Registry and tags: `ghcr.io/jcpowermac/pumpkin-fleet/<image>:<version>`; **no `latest` tag for server images**, and a version tag is **never re-pushed**.
- The only moving tags are `<image>:stable` (fleet) and `<image>:stable-next` (canary node 202 `plugins`), both advanced only by CI.
- An image whose `plugins` list changed must change `version`: `26.3-258` → `26.3-258-p1` → `-p2`. CI rejects a plugin change with an unchanged version.
- Plugin identity is a `sha256` (64 hex) plus a canonical `install_as` jar name. `ADD --checksum` supports sha256 only.
- Sources: `hangar` (native sha256, server-side `platformVersion` filter, 20 req/5 s), `modrinth` (sha1+sha512 only → sha256 computed once by download, 300 req/min, identified `User-Agent`), `github` (sha256 computed), `manual` (human-pinned sha256, never auto-upgraded).
- No GitHub Actions runner may reach `10.10.10.0/24`; nothing in CI SSHes to a node.
- Server MC version pinned at **26.3** (protocol 777). Paper `26.3-258`, Fabric loader `0.20.1`, NeoForge `26.3.0.115`.
- Images keep base `eclipse-temurin:25-jre-ubi10-minimal` (glibc) and run as uid/gid `1000` with `/data` as the volume; container flags follow `containers/README.md` "Running one".
- "The volume always wins" stays the rule; the only exception is the set of names the fleet itself ships, recorded in the stamp `/data/plugins/.fleet-plugins` (spec §5). Within that set the image is authoritative: absent is copied, mismatched is replaced, unbundled is deleted. A name the fleet never shipped is never touched.
- OS channel `ghcr.io/jcpowermac/pumpkin-os:stable`; BlueBuild publishes only `:latest`, so `latest` is the canary OS channel and `stable` moves by an explicit promote (spec §4).
- Legacy LXCs 100–107 are **untouchable** (no reads, copies, or stops). LXC 150 (authentik) belongs to this fleet and stays in scope.
- No node deploy runs before Task 7, and from Task 7 until Task 10 deploys are limited to the canary node **202 `plugins`** — Tasks 1–6 leave the version tags naming images CI has not built yet, and `:stable` does not exist until a promote.

## Review Focus

Each line is a failure mode the spec implies but no task's happy path exercises. Each gets its test in the task named.

1. **Same plugin, two names on an existing volume** — a data dir already holding `LuckPerms-Bukkit-5.5.71.jar` next to a new canonical `LuckPerms.jar` must end up with exactly one copy. (Task 3)
1. **A bumped plugin must actually reach an existing volume** — a volume holding an older `LuckPerms.jar` converges to the build the new image ships. (Task 3)
2. **A jar that downloads fine but fails to load** (unmet dependency, wrong MC build) must be reported by the node, not hidden by a container that is "running". (Task 8)
3. **Reboot window landing while players are online** must skip the reboot and say so. (Task 7)
4. **Promoting a `stable` that does not match the built digest** must abort, not move `stable` to something unbuilt. (Task 4)
5. **Upstream has no release compatible with 26.3** (the EssentialsX case) must leave the manifest untouched and say why. (Task 2)
6. **A node whose data volume is missing or unmounted** must fail the deploy loudly, not write worlds into the root filesystem. (Task 9)

---

### Task 1: Depumpkin the fleet and declare plugins in images.json

**Files:**
- Modify: `containers/images.json` (drop the `pumpkin` entry; `plugins` becomes a list on `papermc`, `fabric`, `velocity`, `neoforge`)
- Delete: `containers/pumpkin/Dockerfile`, `containers/pumpkin/entrypoint.sh`
- Modify: `tasks/gameserver.py` (`WORKLOADS` lobby → `papermc`; delete the non-bundled OPanel install block, currently `tasks/gameserver.py:112-142`)
- Modify: `containers/papermc/Dockerfile`, `containers/fabric/Dockerfile`, `containers/velocity/Dockerfile`, `containers/neoforge/Dockerfile` (comments that cite the deleted pumpkin image)
- Modify: `README.md` (pumpkin mentions)
- Create: `tests/test_manifest.py`
- Modify: `pyproject.toml` (add `[dependency-groups]` with `dev = ["pytest>=8"]`)

**Interfaces:**
- Consumes: nothing.
- Produces: manifest shape every later task reads. Image object: `name`, `version`, `args`, `plugins` (list). Plugin seed entry (this task's shape): `name`, `slug`, `source` ∈ `hangar|modrinth|github|manual`, `mc` (list of MC version strings), `install_as` (canonical jar name). Task 2 fills `version`, `url`, `sha256`, `deps`, `note`.

Seed manifest values (exact): `papermc` version `26.3-258-p1` with plugins `LuckPerms` (slug `luckperms`, source `modrinth`, `install_as: LuckPerms.jar`) and `ViaVersion` (slug `viaversion`, source `hangar`, `install_as: ViaVersion.jar`). `fabric` version `26.3-0.20.1-p1` with plugins `fabric-api` (slug `FabricMC/fabric-api`, source `github`, `install_as: fabric-api.jar`), `opanel` (slug `opanel-mc/opanel`, source `github`, `install_as: opanel-fabric.jar`), `LuckPerms` (slug `luckperms`, source `modrinth`, `install_as: LuckPerms.jar`). `neoforge` and `velocity` get `"plugins": []`. No `EssentialsX` (no 26.3-compatible release) and no `Vault` (no scriptable source) — both named in a `"excluded"` note on the `papermc` entry so a reader knows they were considered. **Task 1 changes only `images.json` for fabric:** `containers/fabric/Dockerfile` keeps its `FABRIC_API` / `OPANEL_VERSION` / `OPANEL_ASSET` args and its two `ADD` lines until Task 3 replaces them with the plugin stage, so the fabric image is never built missing the mods its loader refuses to start without.

- [ ] **Step 1: Write the failing test** in `tests/test_manifest.py`

```python
def test_no_pumpkin_anywhere():            # images.json has no image named pumpkin; WORKLOADS has no pumpkin value
def test_plugin_seed_shape():              # every plugin: slug, source in {hangar,modrinth,github,manual}, install_as ends .jar, mc list
def test_install_as_unique_per_image():    # no duplicate install_as inside one image's plugins
def test_plugin_images_carry_plugin_revision():  # papermc/fabric versions end with "-p1"; velocity/neoforge unchanged
def test_unresolved_fields_absent():       # seed entries have no version/url/sha256 keys yet (Task 2 fills them)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_manifest.py -q`
Expected: FAIL — 4 of 5 fail against the current manifest (pumpkin present, plugins are objects not lists, no `-p1`).

- [ ] **Step 3: Rewrite `containers/images.json`** to the shape and values above, and delete `containers/pumpkin/`.

- [ ] **Step 4: Update `tasks/gameserver.py`** — `"lobby": ("papermc", "4g", "2G")` in `WORKLOADS`, delete the OPanel install block; leave the `_desired` pin expression alone (Task 7 deletes that whole mechanism) and keep the firewalld rule for 3000/tcp keyed on any plugin whose `name == "opanel"` (OPanel's web panel listens on 3000). With `lobby` on `papermc`, the 3000 rule must key on the manifest of the node's image, not on a leftover OPanel entry.

- [ ] **Step 5: Reword the base-image comments and `README.md`** so they justify glibc/UBI without citing the deleted image. No base change.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS (5 passed).

- [ ] **Step 7: Commit**

```bash
git add containers/ tasks/gameserver.py tests/ pyproject.toml README.md
git commit -m "feat: declare plugins in images.json; drop the pumpkin image"
```

---

### Task 2: Plugin resolver — sources, hashes, dependency closure

**Files:**
- Create: `scripts/resolve_plugins.py`
- Create: `tests/test_resolver.py`, `tests/fixtures/hangar_viaversion.json`, `tests/fixtures/hangar_essentialsx.json`, `tests/fixtures/modrinth_luckperms.json`, `tests/fixtures/github_fabric.json` (recorded real API responses)
- Modify: `containers/images.json` (filled by running the resolver)

**Interfaces:**
- Consumes: Task 1's plugin seed entries.
- Produces: `Plugin` dataclass with `name, slug, source, version, url, sha256, install_as, mc, deps, note, stale`; `fetch_json(url: str) -> Any`; `sha256_of_url(url: str, dest: Path) -> str`; `resolve_hangar(p: Plugin, mc: str) -> Plugin`; `resolve_modrinth(p: Plugin, mc: str, loader: str) -> Plugin`; `resolve_github(p: Plugin) -> Plugin`; `resolve_manual(p: Plugin) -> Plugin`; `expand_hangar_deps(p: Plugin) -> list[Plugin]`; `resolve_manifest(manifest: list[dict], image: dict) -> list[dict]`.
- CLI: `--check` (exit 2 when any plugin has a newer compatible release, manifest unchanged), `--bump` (rewrites `containers/images.json` in place, bumping the image's `-pN` revision when a plugin artifact identity — `version`, `url` or `sha256` — changed), `--only <image>`, `--dry-run` (print the PR body to stdout, write nothing).

Endpoints (exact): Hangar `GET https://hangar.papermc.io/api/v1/projects/{slug}/versions?platform=PAPER&platformVersion={mc}&channel=Release&limit=10`, sha256 from `downloads[<platform>].fileInfo.sha256Hash`, dependencies from `pluginDependencies[].projectId` → `GET /api/v1/projects/{id}`; Modrinth `GET https://api.modrinth.com/v2/project/{slug}/version`, filtered client-side by `game_versions` overlap and `loaders`, header `User-Agent: pumpkin-fleet-bumpbot/1.0 (+https://github.com/jcpowermac/pumpkin-fleet)`; GitHub `GET https://api.github.com/repos/{slug}/releases/latest` with `Accept: application/vnd.github+json`.

- [ ] **Step 1: Write the failing tests** in `tests/test_resolver.py`, each fed by a fixture rather than the network

```python
def test_hangar_picks_release_channel_and_compatible_build()   # fixture: 3 versions, one channel=Release for 26.3
def test_hangar_rejects_incompatible_upstream()                # EssentialsX fixture for 26.3 -> raises NoCandidate, manifest untouched
def test_hangar_dependency_closure()                           # ViaVersion -> deps slugs {viabackwards, viarewind}
def test_modrinth_filters_by_loader_and_game_version()         # fixture mixes 1.20.x and 26.3 builds, fabric and bukkit loaders
def test_modrinth_computes_sha256_by_download()                # fixture carries sha1/sha512 only; injected fetch returns bytes
def test_manual_never_upgrades_but_reverifies()
def test_bump_rewrites_p_revision()                            # 26.3-258-p1 -> 26.3-258-p2 when a plugin version changed
def test_bump_is_a_noop_when_nothing_changed()
def test_dry_run_body_names_plugin_version_sha_and_source()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_resolver.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'resolve_plugins'`.

- [ ] **Step 3: Implement `scripts/resolve_plugins.py`** with the signatures above. Approach: `fetch_json` is the single HTTP seam (tests inject it), `sha256_of_url` streams to a temp file and hashes it, `NoCandidate(Exception)` carries the upstream's newest version and the MC version it supports so `--check` can print a useful reason. Sleep 260 ms between Hangar calls (20 req/5 s).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -q`
Expected: PASS.

- [ ] **Step 5: Resolve the real manifest against the live APIs**

Run: `uv run python scripts/resolve_plugins.py --bump && uv run pytest tests/test_manifest.py -q`
Expected: `papermc` and `fabric` plugins gain `version`, `url`, `sha256`, `deps`; version stays `-p1` (first resolution); manifest tests still PASS (extend `test_unresolved_fields_absent` into `test_resolved_fields_present`: sha256 is 64 hex, url host ∈ `{hangar.papermc.io, cdn.modrinth.com, github.com}` — Hangar's download host may be `hangarcdn.papermc.io`, accept both).

- [ ] **Step 6: Commit**

```bash
git add scripts/resolve_plugins.py tests/ containers/images.json
git commit -m "feat: resolve plugin versions, sha256s and dependency closures"
```

---

### Task 3: Bake plugins into the image; seed and converge them in the entrypoint

**Files:**
- Create: `containers/lib/plugin-seed.sh` (shared by both entrypoints)
- Modify: `containers/papermc/Dockerfile`, `containers/papermc/entrypoint.sh`
- Modify: `containers/fabric/Dockerfile`, `containers/fabric/entrypoint.sh`
- Modify: `.github/workflows/containers.yml` (pass `PLUGIN_TRIPLETS` for every image)
- Create: `tests/test_plugin_seed.sh`

**Interfaces:**
- Consumes: Task 1/2 plugin entries (`url`, `install_as`, `sha256`).
- Produces: build arg `PLUGIN_TRIPLETS` — one space-separated list of `url install_as sha256` triples; image dirs `/opt/plugins` (papermc) and `/opt/bundled-mods` (fabric); `/opt/lib/plugin-seed.sh` exposing `seed_plugins <bundled_dir> <data_dir> <stamp_name>`; data-dir stamp `<data_dir>/.fleet-plugins`, one canonical jar name per line.
- `seed_plugins` behaviour, in order: (1) delete a data-dir jar whose sha256 equals a bundled jar's sha256 but whose name differs; (2) copy a bundled jar when its canonical name is absent from the data dir; (3) replace a data-dir jar whose name the image bundles but whose sha256 differs from the bundled jar; (4) delete a name recorded in the old stamp that the image no longer bundles; (5) touch nothing the stamp never recorded; (6) rewrite the stamp from the bundled set.

- [ ] **Step 1: Write the failing test** `tests/test_plugin_seed.sh`

```bash
# mktemp dir; fake bundled jars + fake data jars; source containers/lib/plugin-seed.sh
# assert, with messages:
#   first_seed_copies_all_canonical_and_writes_stamp
#   image_wins_for_a_bundled_name: a data jar with a bundled name but different bytes IS replaced
#   removal: stamp had Vault.jar, bundle lacks it -> deleted; untracked Extra.jar -> untouched
#   dedupe: data/LuckPerms-Bukkit-5.5.71.jar identical to bundled LuckPerms.jar -> removed, canonical kept
#   stamp_after: equals the sorted bundled canonical names
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tests/test_plugin_seed.sh`
Expected: FAIL — `containers/lib/plugin-seed.sh: No such file or directory`.

- [ ] **Step 3: Implement `containers/lib/plugin-seed.sh`** as a POSIX `sh` function file (no bash-only constructs; entrypoints run `#!/bin/sh`). `sha256sum` is available in the UBI base.

- [ ] **Step 4: Add the plugins build stage to `containers/papermc/Dockerfile`** and the same shape to fabric. Body, since build args do not determine it:

```dockerfile
FROM docker.io/library/fedora:42 AS plugins
ARG PLUGIN_TRIPLETS
RUN set -eu; mkdir -p /opt/plugins; cd /opt/plugins; \
    set -- ${PLUGIN_TRIPLETS}; \
    while [ "$#" -ge 3 ]; do \
      curl -fsSL "$1" -o "$2"; echo "$3  $2" | sha256sum -c; shift 3; \
    done
```

then in the final stage `COPY --from=plugins /opt/plugins /opt/plugins`, `COPY lib/plugin-seed.sh /opt/lib/plugin-seed.sh`, `RUN chown -R 1000:1000 /opt/plugins /opt/lib` (fabric: `/opt/bundled-mods`, and delete the two `ADD` lines plus `FABRIC_API` / `OPANEL_VERSION` / `OPANEL_ASSET` args). `shortcut: URLs must contain no whitespace (all four CDNs satisfy); upgrade to a TSV build arg if a source needs a signed query string.`

- [ ] **Step 4b: Pass the build arg from CI in the same task** — extend the existing `args` step in `.github/workflows/containers.yml` with `PLUGIN_TRIPLETS=$(jq -r '.[] | select(.name=="<image>") | .plugins[]? | "\(.url) \(.install_as) \(.sha256)"' containers/images.json | paste -sd' ' -)`, and make the plugins stage **fail when an image declares plugins but the arg is empty** (a silent empty `/opt/plugins` would ship a server with no plugins while the manifest claims them). `velocity` and `neoforge` legitimately pass an empty arg.

- [ ] **Step 5: Seed in the entrypoints** — `papermc/entrypoint.sh` calls `seed_plugins /opt/plugins /data/plugins fleet-plugins` before `exec java`; `fabric/entrypoint.sh` replaces its inline bundled-mod loop with `seed_plugins /opt/bundled-mods /data/mods fleet-mods`.

- [ ] **Step 6: Run the shell test and the seed tests again**

Run: `bash tests/test_plugin_seed.sh && uv run pytest tests/test_manifest.py -q`
Expected: both PASS.

- [ ] **Step 7: Build both images locally if a builder exists** (`command -v podman || command -v docker`): build `papermc` with `PLUGIN_TRIPLETS` from the manifest via `jq -r '.[] | select(.name=="papermc") | .plugins[] | "\(.url) \(.install_as) \(.sha256)"' | paste -sd' ' -`, then list `/opt/plugins`. Repeat with one sha256 deliberately corrupted.
Expected: jar list shows exactly the canonical names; the corrupted build **fails** at `sha256sum -c`. If no builder is installed locally, state that Task 4's CI run carries this proof and record it in the commit message.

- [ ] **Step 8: Commit** `git commit -m "feat: bake plugin jars into images with a sha256 gate"`.

---

### Task 4: CI gate, channel tags, and the promote step

**Files:**
- Create: `scripts/ci_gate.py`, `tests/test_ci_gate.py`
- Modify: `.github/workflows/containers.yml`

**Interfaces:**
- Consumes: Task 1/2 manifest.
- Produces: `GateError(image: str, message: str)`; `gate(base: list[dict], head: list[dict]) -> list[GateError]`; CLI `scripts/ci_gate.py --base <path> --head <path>` (exit 1 on any error). Rules: plugin list changed with an unchanged `version`; a `version` equal to the base's (tag already published); a plugin with a non-64-hex `sha256` or a missing `url`; an image with plugins and no `-pN` suffix; a plugin list changed without the CI-visible `PLUGIN_TRIPLETS` being derivable (missing `url`).

- [ ] **Step 1: Write the failing tests** in `tests/test_ci_gate.py`

```python
def test_plugin_change_without_version_bump_is_rejected()
def test_same_version_twice_is_rejected()            # Review Focus 4's CI half: a tag never re-pushed
def test_unresolved_plugin_entry_is_rejected()       # missing sha256 / url
def test_clean_manifest_has_no_errors()
def test_p_revision_bump_with_plugin_change_is_allowed()
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/test_ci_gate.py -q`, expected `ModuleNotFoundError`.

- [ ] **Step 3: Implement `scripts/ci_gate.py`** with the signatures above.

- [ ] **Step 4: Run tests to verify they pass** — expected PASS.

- [ ] **Step 5: Wire the workflow** — in `.github/workflows/containers.yml`: `actions/checkout@v4` with `fetch-depth: 2` and a gate step writing `git show HEAD^:containers/images.json > /tmp/base.json` then `uv run python scripts/ci_gate.py --base /tmp/base.json --head containers/images.json` (skip the gate on `workflow_dispatch`); add `:${matrix.image.version}` **and** `:stable-next` to `tags` (Task 3 already added the `PLUGIN_TRIPLETS` build arg). Add a `promote` job, `if: github.event_name == 'workflow_dispatch'`, inputs `image` and `digest`: log in, `docker pull <prefix>/<image>:stable-next`, refuse unless `docker inspect --format '{{index .RepoDigests 0}}'` ends with the input digest, then tag and push `:stable`.

- [ ] **Step 6: Verify locally before pushing**

Run: `uv run python scripts/ci_gate.py --base <(git show HEAD:containers/images.json) --head containers/images.json; echo $?`
Expected: `0` for the committed manifest; `1` with a `GateError` line when you hand-edit a plugin version without touching `version`.

- [ ] **Step 7: Verify in CI** — push a branch that bumps one plugin to a newer version and confirm: gate passes, the build publishes `<image>:<version>` and `<image>:stable-next`, then dispatch `promote` with the built digest and confirm ghcr.io shows `stable` at that digest. Negative tests: a deliberate same-version push goes red at the gate, and a `promote` dispatch with a stale or mistyped digest fails the digest check rather than moving `stable` (Review Focus 4).

- [ ] **Step 8: Commit** `git commit -m "feat: gate plugin manifest changes; stable and stable-next channels"`.

---

### Task 5: Bump bot that opens PRs and never merges

**Files:**
- Create: `.github/workflows/bump-plugins.yml`
- Modify: `scripts/resolve_plugins.py` (only if `--bump` cannot already emit the PR body; Task 2's `--dry-run` covers it)

**Interfaces:**
- Consumes: Task 2 CLI (`--bump`, `--dry-run`, `--only`), Task 4 gate (the opened PR must pass it).
- Produces: a scheduled job (cron `0 6 * * 3`, plus `workflow_dispatch`) on `ubuntu-latest` with `permissions: {contents: write, pull-requests: write}` that: checks out a `bump/plugins-<run_number>` branch, runs `--bump`, and when `git diff --exit-code containers/images.json` reports changes, commits, pushes, and opens a PR whose body is `--dry-run` output (per plugin: name, old→new version, sha256, source, MC compat verdict, dependency additions) plus a **staleness section** for plugins where `--check` found a newer release the resolver refused to take.

- [ ] **Step 1: Add the workflow file** with the steps above. Guard: skip opening a second PR when an open PR already carries the same resolved `sha256` set (`gh pr list --search "author:github-actions[bot]" --json title,body` and compare).

- [ ] **Step 2: Verify the body content locally**

Run: `uv run python scripts/resolve_plugins.py --dry-run`
Expected: a Markdown body listing every plugin with version, sha256 and source; nothing written to `containers/images.json` (`git status --porcelain containers/images.json` empty).

- [ ] **Step 3: Verify the real run** — `gh workflow run bump-plugins.yml`, then inspect the opened PR (or the empty run if nothing is due) and confirm the gate passes on that PR.

- [ ] **Step 4: Commit** `git commit -m "feat: weekly plugin bump PRs"`.

---

### Task 6: The OS channel — pumpkin-os:stable, canary on latest

**Files:**
- Modify: `configs/pins.env`
- Modify: `.github/workflows/os.yml`
- Modify: `recipes/fleet.yml` (add `python3`)
- Modify: `templates/fleet.ign.butane`, `scripts/fleet/20-create-nodes.sh`

**Interfaces:**
- Produces: `pins.env` keys `OS_IMAGE=ghcr.io/jcpowermac/pumpkin-os`, `OS_TAG=stable`, `OS_TAG_CANARY=latest`, `CANARY_NODE=plugins`, `FLEET_CHANNEL=stable`, `FLEET_CHANNEL_CANARY=stable-next`, `REBOOT_ONCALENDAR=Sun *-*-* 05:00`, `MC_DATA_DISK_GB=40`; and `20-create-nodes.sh --render-only`, which renders every node's butane/Ignition into `build/nodes/` and stops before any SSH to pve3.

- [ ] **Step 1: Add `--render-only` to `scripts/fleet/20-create-nodes.sh`** (skip the credential read and all pve3 calls) and make the rebase target per node: `UCORE_TAG="$OS_TAG"`, except the canary node (`$name == "$CANARY_NODE"`) which gets `$OS_TAG_CANARY`; `UCORE_IMAGE="$OS_IMAGE"`.

- [ ] **Step 2: Verify the rendering offline**

Run: `bash scripts/fleet/20-create-nodes.sh --render-only && grep -h "rpm-ostree rebase" build/nodes/*.butane`
Expected: four lines; three target `ghcr.io/jcpowermac/pumpkin-os:stable`, `plugins.butane` targets `…pumpkin-os:latest`.

- [ ] **Step 3: Add `python3` to `recipes/fleet.yml`**'s dnf packages — Task 7 and Task 8 run node-side Python scripts, and FCOS does not guarantee `python3` in the base.

- [ ] **Step 4: Add a `promote-os` job** to `.github/workflows/os.yml` (`workflow_dispatch`, input `digest`): pull `pumpkin-os:latest`, refuse unless its digest equals the input, tag and push `pumpkin-os:stable`.

- [ ] **Step 5: Verify the channel exists after a CI run**

Run: `gh run list --workflow os --limit 3` then `skopeo inspect docker://ghcr.io/jcpowermac/pumpkin-os:stable --format '{{.Digest}}'` (or `docker buildx imagetools inspect`)
Expected: `stable` resolves to a digest that some `latest` build had; nodes following `stable` are not exposed to an unreviewed daily build.

- [ ] **Step 6: Commit** `git commit -m "feat: OS channel pumpkin-os:stable with a canary on latest"`.

---

### Task 7: Quadlet units, auto-update, and a player-aware reboot window

**Files:**
- Create: `scripts/render_quadlet.py`, `tests/test_render_quadlet.py`
- Create: `scripts/fleet/reboot-gate.py`, `tests/test_reboot_gate.py`
- Modify: `tasks/gameserver.py` (replace `podman run` with quadlet units + timers)

**Interfaces:**
- Consumes: Task 1 `WORKLOADS`, Task 6 pins (`FLEET_CHANNEL*`, `REBOOT_ONCALENDAR`).
- Produces: `render_container_unit(node: str, image: str, channel: str, memory: str, java_memory: str, data_dir: str) -> str`; `render_reboot_units(node: str, oncalendar: str) -> tuple[str, str]` (timer + oneshot service); `should_reboot(ping: dict | None) -> bool`; `reboot-gate.py --dry-run` prints the decision and exits 0. Node paths: `~/.config/containers/systemd/<node>.container`, `/usr/local/lib/fleet/reboot-gate.py`, `/usr/local/lib/fleet/mc-ping.py`, `/etc/systemd/system/fleet-reboot.{timer,service}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_render_quadlet.py`

```python
def test_unit_carries_image_channel_and_volume()   # Image=ghcr.io/…/<image>:stable, Volume=/srv/<node>/data:/data:Z
def test_unit_canary_uses_stable_next()
def test_unit_opts_into_registry_auto_update()     # AutoUpdate=registry, Restart=always, StopTimeout… , Memory=4g
def test_unit_never_names_latest()                 # no ":latest" anywhere in a server unit
def test_reboot_timer_is_randomised()              # RandomizedDelaySec present
```

and in `tests/test_reboot_gate.py`

```python
def test_online_players_block_the_reboot()          # {"players":{"online":3,...}} -> False   # Review Focus 3
def test_empty_server_allows_the_reboot()
def test_unreachable_server_allows_the_reboot()     # None -> True, with a logged reason
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/test_render_quadlet.py tests/test_reboot_gate.py -q`, expected import errors.

- [ ] **Step 3: Implement both modules.** `render_container_unit` emits `[Unit]/[Container]/[Service]` using only documented quadlet keys: `Image=`, `ContainerName=`, `Network=host`, `Memory=`, `Environment=EULA=true`, `Environment=JAVA_MEMORY=…`, `Volume=`, `AutoUpdate=registry`, `PodmanArgs=--stop-timeout 60`, `[Service] Restart=always` + `TimeoutStopSec=90`. `reboot-gate.py` runs `mc-ping.py 127.0.0.1 25565`, decides via `should_reboot`, and without `--dry-run` calls `systemctl reboot`.

- [ ] **Step 4: Run tests to verify they pass** — expected PASS.

- [ ] **Step 5: Rewrite the deploy half of `tasks/gameserver.py`** — keep linger, data dir, `podman unshare chown`, polkit/sudoers and firewalld steps as they are (spec §7's `/etc` finding says they persist; delete the `Does not survive an OSTree rebase` comment and reference spike-notes §16.4 with the verified result instead); push the quadlet file (same base64-to-`tee` pattern used for the polkit rule, no `_sudo` — it is a user file), then `systemctl --user daemon-reload`, `systemctl --user start <node>.service` (never `enable` — a quadlet unit is generator-owned and transient, so boot persistence is the unit's `[Install] WantedBy=default.target`); enable the user `podman-auto-update.timer`; push `reboot-gate.py` + `mc-ping.py` and install the two system units with `systemctl daemon-reload && systemctl enable --now fleet-reboot.timer`. Delete the `pin file` + `podman run` block and the port-wait loop's dependence on it (keep a port-wait verification).

- [ ] **Step 6: Deploy to the canary node and verify**

Run: `uv run pyinfra inventory/hosts.py --limit plugins tasks/gameserver.py`
Expected: `systemctl --user status plugins.service` active; `podman ps --filter name=^plugins$` shows the container; `systemctl --user is-enabled podman-auto-update.timer` → `enabled`; `python3 /usr/local/lib/fleet/reboot-gate.py --dry-run` prints `skip: <n> players online` or `reboot allowed`; `firewall-cmd --list-ports` shows `25565/tcp`.

- [ ] **Step 7: Prove auto-update on the canary only** — advance `stable-next` to a new digest via Task 4's build, run `podman auto-update --dry-run`, confirm it names `registry://…plugins` with the new digest, then run it for real and confirm `podman ps --format '{{.Image}}'` reports the new digest and the server answers. Record in `docs/spike-notes.md` what podman actually does when the new container fails to start.

- [ ] **Step 8: Commit** `git commit -m "feat: quadlet units with registry auto-update and a gated reboot window"`.

---

### Task 8: Prove the plugins loaded, on the node and from the laptop

**Files:**
- Create: `scripts/fleet/verify-plugins.py`, `tests/test_verify_plugins.py`, `tests/fixtures/paper_log_clean.txt`, `tests/fixtures/paper_log_plugin_failure.txt`
- Create: `tasks/verify.py`
- Modify: `tasks/gameserver.py` (write the expected-jar list)

**Interfaces:**
- Consumes: Task 1/2 manifest, Task 7's data dirs.
- Produces: `expected_jars(manifest_path: Path, image_name: str) -> set[str]`; `missing_jars(expected: set[str], listing: list[str]) -> set[str]`; `load_failures(log_text: str) -> list[str]` over these signatures: `Failed to load plugin`, `Could not load plugin`, `requires a newer version`, `is not compatible`, `NoClassDefFoundError`, `Unable to hook into`; `verdict(expected, listing, log) -> dict` with keys `missing`, `failures`, `ok`.
- Node paths: `/etc/fleet/expected-plugins.<image>.txt` (canonical names, one per line, written by `gameserver.py`), `/usr/local/lib/fleet/verify-plugins.py`, run by `fleet-verify.service` (oneshot, `After=<node>.service`) writing `/run/fleet-verify.status` (`ok` or the failing lines).

`shortcut: verdict keys off failure signatures plus jar presence, not Paper's per-plugin "Loading …" lines, whose exact wording is unpinned; Task 10 Step 4 tightens the patterns against a real log.`

- [ ] **Step 1: Write the failing tests** in `tests/test_verify_plugins.py`

```python
def test_missing_jar_is_reported()                   # expected {LuckPerms.jar, ViaVersion.jar}, listing missing one
def test_clean_log_has_no_failures()
def test_plugin_load_failure_is_flagged()            # Review Focus 2: fixture log with "Failed to load plugin ViaBackwards"
def test_dependency_failure_is_flagged()             # "Mod/plugin requires a newer version"
def test_verdict_ok_when_present_and_clean()
```

- [ ] **Step 2: Run tests to verify they fail** — expected import error.

- [ ] **Step 3: Implement `scripts/fleet/verify-plugins.py`** with the signatures above; CLI `--expected <path> --data-dir <path> --image <name> --log-from <cmd>` where `--log-from` defaults to `podman logs <node>`.

- [ ] **Step 4: Run tests to verify they pass** — expected PASS.

- [ ] **Step 5: Write `tasks/verify.py`** (laptop side, pyinfra): per node print the running image digest, `systemctl --user is-active <node>.service`, `/run/fleet-verify.status`, `cat /etc/fleet/expected-plugins.*.txt` against the data-dir listing, and the mc-ping answer; exit non-zero if any node's status is not `ok`.

- [ ] **Step 6: Deploy and run against the canary**

Run: `uv run pyinfra inventory/hosts.py --limit plugins tasks/gameserver.py && uv run pyinfra inventory/hosts.py --limit plugins tasks/verify.py`
Expected: verify prints the digest, `ok`, and the two canonical jar names; exit 0.

- [ ] **Step 7: Commit** `git commit -m "feat: verify declared plugins loaded, node-side and from the laptop"`.

---

### Task 9: PVE rebuild plumbing — pinned FCOS, data disks, no spike-era assumptions

**Files:**
- Modify: `scripts/spike/10-download-fcos.sh`
- Create: `scripts/fleet/15-ensure-data-disks.sh`, `tests/test_pve_scripts.py`
- Modify: `scripts/fleet/20-create-nodes.sh`
- Modify: `tasks/gameserver.py` (mount the data volume)

**Interfaces:**
- Consumes: Task 6 pins (`FCOS_BUILD`, `MC_DATA_DISK_GB`, VMIDs/names).
- Produces: `scripts/spike/10-download-fcos.sh --print-url` (prints the qcow2 URL+sha256 it would fetch, fetches nothing); pinned fetch reads `https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/$FCOS_BUILD/x86_64/meta.json` and only falls back to the live `streams/stable.json` when `FCOS_BUILD=auto`, and it no longer rewrites `pins.env` unless `auto`. `scripts/fleet/15-ensure-data-disks.sh [--dry-run]` creating `pool/mcdata/<node>` zvols and attaching them: `qm set <vmid> --scsi1 /dev/zvol/pool/mcdata/<node>`. `mount_unit(node: str, device: str) -> str` and `needs_format(blkid_output: str) -> bool` in `tasks/gameserver.py` or a `scripts/fleet/mount.py` helper.

- [ ] **Step 1: Write the failing tests** in `tests/test_pve_scripts.py`

```python
def test_pinned_fcos_build_is_used_verbatim()      # --print-url with FCOS_BUILD=45.20261030.3.1 contains that build id
def test_auto_resolves_live_stream()               # FCOS_BUILD=auto hits raw.githubusercontent.com/coreos/fedora-coreos-streams
def test_data_disk_attach_is_a_host_zvol()         # dry-run output contains "--scsi1 /dev/zvol/pool/mcdata/lobby" and NOT "pool:vm-201-disk-1"
def test_mount_unit_paths()                        # Where=/srv/<node>/data, no fstab
def test_blank_device_formats_and_used_device_does_not()   # needs_format("") is True, needs_format("UUID=… TYPE=xfs") is False  # Review Focus 6
```

- [ ] **Step 2: Run tests to verify they fail** — expected import/exit failures.

- [ ] **Step 3: Implement** `--print-url` in `scripts/spike/10-download-fcos.sh` (pinned `meta.json` path when `FCOS_BUILD != auto`) and `scripts/fleet/15-ensure-data-disks.sh` (`zfs create -V $MC_DATA_DISK_GB pool/mcdata/<node>` when absent, `qm set --scsi1` when the VM has no `scsi1`).

- [ ] **Step 4: Run tests to verify they pass** — expected PASS.

- [ ] **Step 5: Update `scripts/fleet/20-create-nodes.sh`** — `ssh "$PVE_HOST" mkdir -p /var/coreos/snippets`; the LXC 150 credential read must exit with a named error ("authentik LXC 150 unreadable — sssd bind credential unavailable") instead of proceeding; call `15-ensure-data-disks.sh` after `qm start`'s config steps so `scsi1` is attached on every create; keep the destroy step as-is and note inline that the host-owned zvol survives `qm destroy --purge` (verified with a throwaway VMID: created `pool/test-mcdata`, attached as `/dev/zvol/…`, destroyed with `--purge`, zvol still present).

- [ ] **Step 6: Mount the data volume in `tasks/gameserver.py`** — write `/etc/systemd/system/srv-<node>-data.mount` (`What=<FLEET_DATA_DEV>`, `Where=/srv/<node>/data`, `Type=xfs`), format only when `needs_format(blkid)` is true, daemon-reload, `systemctl start`, and fail the deploy when `/srv/<node>/data` is not a mountpoint afterwards. `FLEET_DATA_DEV` goes in `pins.env`; Task 10 Step 2 records the real `/dev/disk/by-id` path from a node and pins it.

- [ ] **Step 7: Migrate existing test data without losing it** — for a node whose `scsi1` is new and whose old `/srv/<node>/data` is non-empty: mount the zvol at `/srv/<node>/data.tmp`, `cp -a`, move the old tree aside to `/srv/<node>/data.pre-zvol`, then mount at the real path. Verify: `findmnt /srv/<node>/data` shows the zvol and the server still answers.

- [ ] **Step 8: Commit** `git commit -m "feat: pinned FCOS import and host-owned Minecraft data disks"`.

---

### Task 10: Rebuild the fleet and walk the loop once, end to end

**Files:**
- Modify: `docs/plan.md` (check Task 6/7 items this plan supersedes, mark Task 8 descoped banner), `docs/spike-notes.md` (new sections: quadlet on FCOS user session, podman auto-update real behaviour, `/dev/disk/by-id` naming, quadlet keys that did not work)

**Interfaces:**
- Consumes: everything above.
- Produces: four nodes on `stable`, one canary on `stable-next`, one promoted plugin set, and a written record of what the loop actually did.

- [ ] **Step 1: Fetch and pin** — `bash scripts/spike/10-download-fcos.sh` with `FCOS_BUILD` at a real build, confirm `--print-url` and the qcow2 on pve3 match.
- [ ] **Step 2: Move the worlds onto the zvol, then rebuild the VMs — in this order** (it is the order `20-create-nodes.sh` now enforces; a rebuild that starts with the create destroys the worlds still on `scsi0` — 201 and 203 today). Per node whose world is still on `scsi0`:
  1. `bash scripts/fleet/15-ensure-data-disks.sh` — creates `pool/mcdata/<node>` and attaches `scsi1` to the node that is **still running**.
  2. `uv run pyinfra inventory/hosts.py --limit <node> tasks/gameserver.py` — writes `/etc/systemd/system/srv-<node>-data.mount`, formats the blank zvol, and then **refuses** the unmigrated `/srv/<node>/data`. That refusal is the expected outcome of this step; the unit it wrote is what the next step reads.
  3. On the node, as root, server stopped (`systemctl --user stop <node>.service` as `core`): `bash scripts/fleet/25-migrate-data.sh <node>` — copies onto the zvol, verifies, moves the original to `/srv/<node>/data.pre-zvol` (never deleted), and mounts the zvol. It refuses a running container, an unformatted zvol and a non-empty one.
  4. `uv run pyinfra inventory/hosts.py --limit <node> tasks/gameserver.py` again — the volume is mounted, so the deploy proceeds (ownership, then the container).

  Then `bash scripts/fleet/20-create-nodes.sh` (it refuses the whole run — read-only, before any stop or destroy — if any node's `scsi1` is not `/dev/zvol/pool/mcdata/<node>`), wait for the rebase (`bash scripts/spike/40-wait-rebase.sh` per node), `ssh core@<ip> ls /dev/disk/by-id` on 202, pin `FLEET_DATA_DEV` in `pins.env`, and re-run the deploy for all four nodes (step 5's order, in `task-9-report.md`).
- [ ] **Step 3: Deploy all four** — `uv run pyinfra inventory/hosts.py --group game_nodes tasks/gameserver.py`. Expected: four `--user` quadlet services active; `rpm-ostree status` on 202 shows `pumpkin-os:latest`, the others `pumpkin-os:stable`.
- [ ] **Step 4: Verify** — `uv run pyinfra inventory/hosts.py --group game_nodes tasks/verify.py`. Expected: exit 0, `ok` per node, canonical jars listed. Tighten `load_failures` signatures against the real Paper log here and commit the change.
- [ ] **Step 5: Join the canary with a Minecraft client** — direct connect to `10.10.10.55:25565`, confirm LuckPerms (`/luckpermstest`-equivalent: `/lp` loads) and ViaVersion's protocol list, then confirm the same on a `stable` node is on the previous set.
- [ ] **Step 6: Promote** — `gh workflow run promote -f image=papermc -f digest=<built digest>`, wait one `podman-auto-update.timer` tick (or run `podman auto-update` on the three fleet nodes), and confirm they converge on the new digest without a human touching pyinfra.
- [ ] **Step 7: Prove rollback** — dispatch `promote` with the previous digest, confirm all four converge back, and confirm a `rpm-ostree rollback` on one node still boots.
- [ ] **Step 8: Commit** `git commit -m "docs: fleet rebuilt on channels; record what the loop did"`.
