#!/usr/bin/env bash
# Move <image>:<channel tag> to a digest CI published for that image — registry-side, no pull.

# Rulings 4a + 6a: the accepted digests are the ones ghcr.io already serves for this image, i.e.
# the Docker-Content-Digest of one of its published tags — version tags for the server images,
# BlueBuild's dated builds for pumpkin-os, plus stable and stable-next. Accepting an *older*
# published tag's digest is the rollback path both channels need (spec §1: revert the change,
# repoint the channel at the previous digest). A digest matching none of them is refused — that
# is the whole guard, and it is what keeps a channel pointing only at something published.
#
# Retagging by manifest copy (docker buildx imagetools create) rather than docker
# pull/tag/push: the daemon would re-push the single-platform manifest for the OCI index
# ghcr stored, so the channel tag would land on a different digest than the one just verified.
#
# env: PREFIX  ghcr.io/<owner>[/<repo>], as the workflow's prefix step lowercases it
#      IMAGE   images.json image name, or the OS image name (pumpkin-os)
#      DIGEST  sha256:<64 hex> or bare 64 hex
#      GITHUB_TOKEN  optional: ghcr serves a public package anonymously
#      TARGET_TAG  optional: the channel tag to move, default stable. The nodes follow the tag
#                  pins.env names, so the retag has to follow it too: a hardcoded `:stable`
#                  would leave a node on a renamed channel tag that nothing ever moves.
set -euo pipefail

image="${IMAGE:-}"
digest="${DIGEST:-}"
prefix="${PREFIX:-}"
target_tag="${TARGET_TAG:-stable}"
digest="${digest#sha256:}"

if ! [[ "$image" =~ ^[a-z0-9-]+$ ]]; then
  echo "IMAGE must be an images.json image name, got '$image'" >&2
  exit 1
fi
if ! [[ "$target_tag" =~ ^[a-z0-9-]+$ ]]; then
  echo "TARGET_TAG must be a channel tag, got '$target_tag'" >&2
  exit 1
fi
if [[ ! "$digest" =~ ^[0-9a-f]{64}$ || -z "$prefix" ]]; then
  echo "DIGEST must be a 64-hex sha256, got '${DIGEST:-}'" >&2
  exit 1
fi

repo="${prefix#ghcr.io/}"
api="https://ghcr.io/v2/$repo/$image"
accept="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v1+json"
auth=()
[[ -n "${GITHUB_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")

# `|| { … }` on each call: under set -e a bare `curl: (22)` is the only thing the job log
# would show, and that is not actionable.
token=$(curl -fsS ${auth[@]+"${auth[@]}"} "https://ghcr.io/token?service=ghcr.io&scope=repository:$repo/$image:pull" | jq -r .token) \
  || { echo "ghcr token exchange failed for $repo/$image" >&2; exit 1; }
if [[ -z "$token" || "$token" == "null" ]]; then
  echo "ghcr returned no pull token for $repo/$image" >&2
  exit 1
fi

tags=$(curl -fsS -H "Authorization: Bearer $token" "$api/tags/list" | jq -r '.tags[]?') \
  || { echo "could not list the published tags of $repo/$image" >&2; exit 1; }

matched=""
for tag in $tags; do
  headers=$(curl -fsSI -H "Authorization: Bearer $token" -H "Accept: $accept" "$api/manifests/$tag" | tr -d '\r') \
    || { echo "could not read the digest of $repo/$image:$tag" >&2; continue; }
  if [[ "$(grep -im1 '^docker-content-digest:' <<<"$headers" | cut -d' ' -f2)" == "sha256:$digest" ]]; then
    matched="$tag"
    break
  fi
done

if [[ -z "$matched" ]]; then
  echo "refusing: sha256:$digest matches no published tag of $repo/$image — moving $target_tag needs a digest CI built" >&2
  exit 1
fi
echo "sha256:$digest is published as $repo/$image:$matched; moving $repo/$image:$target_tag to it"

# Needs the docker login the workflow job already did (packages: write).
docker buildx imagetools create --tag "$prefix/$image:$target_tag" "$prefix/$image@sha256:$digest"
