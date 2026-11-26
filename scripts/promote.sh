#!/usr/bin/env bash
# Move <image>:stable to a digest CI published for that image — registry-side, no pull.

# Ruling 4a: the accepted digests are the ones ghcr.io already serves for this image, i.e.
# the Docker-Content-Digest of one of its published tags (its version tags plus stable and
# stable-next). Accepting an *older* version tag's digest is the rollback path the design's
# rejection needs (spec §1: revert the PR, repoint stable at the previous digest). A digest
# matching none of them is refused — that is the whole guard, and it is what keeps stable
# pointing only at something the build job published.
#
# Retagging by manifest copy (docker buildx imagetools create) rather than docker
# pull/tag/push: the daemon would re-push the single-platform manifest for the OCI index
# ghcr stored, so stable would land on a different digest than the one just verified.
#
# env: PREFIX  ghcr.io/<owner>/<repo>, as the workflow's prefix step lowercases it
#      IMAGE   images.json image name
#      DIGEST  sha256:<64 hex> or bare 64 hex
#      GITHUB_TOKEN  optional: ghcr serves a public package anonymously
set -euo pipefail

image="${IMAGE:-}"
digest="${DIGEST:-}"
prefix="${PREFIX:-}"
digest="${digest#sha256:}"

if ! [[ "$image" =~ ^[a-z0-9-]+$ ]]; then
  echo "IMAGE must be an images.json image name, got '$image'" >&2
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

token=$(curl -fsS ${auth[@]+"${auth[@]}"} "https://ghcr.io/token?service=ghcr.io&scope=repository:$repo/$image:pull" | jq -r .token)
if [[ -z "$token" || "$token" == "null" ]]; then
  echo "could not get a pull token for $repo/$image" >&2
  exit 1
fi

matched=""
for tag in $(curl -fsS -H "Authorization: Bearer $token" "$api/tags/list" | jq -r '.tags[]?'); do
  headers=$(curl -fsSI -H "Authorization: Bearer $token" -H "Accept: $accept" "$api/manifests/$tag" | tr -d '\r') || continue
  if [[ "$(grep -im1 '^docker-content-digest:' <<<"$headers" | cut -d' ' -f2)" == "sha256:$digest" ]]; then
    matched="$tag"
    break
  fi
done

if [[ -z "$matched" ]]; then
  echo "refusing: sha256:$digest matches no published tag of $repo/$image — stable only moves to a digest CI built" >&2
  exit 1
fi
echo "sha256:$digest is published as $repo/$image:$matched; moving $repo/$image:stable to it"

# Needs the docker login the workflow job already did (packages: write).
docker buildx imagetools create --tag "$prefix/$image:stable" "$prefix/$image@sha256:$digest"
