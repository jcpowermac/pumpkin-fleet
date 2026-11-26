#!/usr/bin/env bash
# scripts/promote.sh is the only thing that moves <image>:stable, and Ruling 4a says what
# it may accept: a digest that matches one of the image's published tags (its version tags
# plus stable/stable-next). That is what makes rollback possible — repointing stable at an
# older published version tag after a reverted PR — while a digest CI never published is
# refused. curl and docker are stubbed: nothing here touches ghcr.io.

set -u

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROMOTE=$REPO/scripts/promote.sh
if [ ! -r "$PROMOTE" ]; then
  echo "FAIL: $PROMOTE is not readable"
  exit 1
fi

fail() { echo "FAIL: $1"; exit 1; }

work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT

# papermc as ghcr.io has it: 26.3-258-p1 (A) superseded by 26.3-258-p2 (B), stable and
# stable-next both at B. C belongs to no tag at all.
A=1111111111111111111111111111111111111111111111111111111111111111
B=2222222222222222222222222222222222222222222222222222222222222222
C=3333333333333333333333333333333333333333333333333333333333333333

bin=$work/bin; mkdir -p "$bin"
cat > "$bin/curl" <<'EOF'
#!/usr/bin/env bash
# Stand-in for the ghcr.io tag/manifest API: same URL shapes, canned digests.
url=""
head=no
for a in "$@"; do
  case "$a" in http*) url="$a" ;; -*I) head=yes ;; esac
done
digest_of() {
  case "$1" in
    26.3-258-p1) echo 1111111111111111111111111111111111111111111111111111111111111111 ;;
    26.3-258-p2|stable|stable-next) echo 2222222222222222222222222222222222222222222222222222222222222222 ;;
  esac
}
case "$url" in
  */token*) echo '{"token":"faketoken"}' ;;
  */tags/list) echo '{"name":"jcpowermac/pumpkin-fleet/papermc","tags":["26.3-258-p1","broken-tag","26.3-258-p2","stable","stable-next"]}' ;;
  */manifests/*)
    tag=${url##*/manifests/}
    sha=$(digest_of "$tag")
    [ -n "$sha" ] || exit 22
    [ "$head" = yes ] && printf 'HTTP/2 200 \r\n'
    printf 'content-type: application/vnd.oci.image.index.v1+json\r\n'
    printf 'docker-content-digest: sha256:%s\r\n' "$sha"
    ;;
  *) exit 22 ;;
esac
EOF
cat > "$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
EOF
chmod +x "$bin/curl" "$bin/docker"

run() { # run <image> <digest> ; echoes the promote output, sets $code
  : > "$work/docker.log"
  out=$(PATH="$bin:$PATH" DOCKER_LOG="$work/docker.log" \
        PREFIX=ghcr.io/jcpowermac/pumpkin-fleet IMAGE="$1" DIGEST="$2" GITHUB_TOKEN=fake \
        bash "$PROMOTE" 2>&1)
  code=$?
  log=$(cat "$work/docker.log")
}

# Rollback: stable is at B, the reverted PR's digest is the older published 26.3-258-p1 (A).
run papermc "sha256:$A"
[ "$code" -eq 0 ] || fail "rollback promote exited $code: $out"
case "$out" in *26.3-258-p1*) ;; (*) fail "rollback did not name the published tag: $out" ;; esac
case "$log" in *"--tag ghcr.io/jcpowermac/pumpkin-fleet/papermc:stable"*) ;;
  (*) fail "rollback did not retag stable: $log" ;;
esac
case "$log" in *"papermc@sha256:$A"*) ;; (*) fail "rollback retagged the wrong digest: $log" ;; esac

# Forward: the digest of the stable-next that CI just built.
run papermc "$B"
[ "$code" -eq 0 ] || fail "forward promote exited $code: $out"
case "$log" in *"--tag ghcr.io/jcpowermac/pumpkin-fleet/papermc:stable"*) ;;
  (*) fail "forward promote did not retag stable: $log" ;;
esac

# Refusal: a digest that belongs to none of the image's published tags must not move stable.
# The list also holds a tag whose manifest HEAD fails, which the loop has to survive.
run papermc "$C"
[ "$code" -ne 0 ] || fail "an unpublished digest was accepted: $out"
[ -s "$work/docker.log" ] && fail "an unpublished digest still retagged stable: $log"
case "$out" in *refusing*) ;; (*) fail "refusal did not say why: $out" ;; esac

# Inputs are the trust boundary: no registry call for a malformed name or digest.
run 'papermc; curl evil' "$A"
[ "$code" -ne 0 ] || fail "a malformed image name was accepted"
[ -s "$work/docker.log" ] && fail "malformed image name reached the retag: $log"
run papermc "sha256:deadbeef"
[ "$code" -ne 0 ] || fail "a malformed digest was accepted"
run papermc ""
[ "$code" -ne 0 ] || fail "an empty digest was accepted"

echo "OK: promote accepts any published digest (including a rollback) and refuses the rest"
