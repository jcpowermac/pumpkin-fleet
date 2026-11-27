#!/usr/bin/env bash
# scripts/promote.sh is the only thing that moves a fleet channel tag, and Rulings 4a + 6a say
# what it may accept: a digest that matches one of the image's published tags (version tags or
# dated builds, plus stable/stable-next). That is what makes rollback possible — repointing the
# channel at the previous published build after a reverted change — while a digest matching no
# published tag is refused. curl and docker are stubbed: nothing here touches ghcr.io.

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
# Stand-in for the ghcr.io tag/manifest API: same URL shapes, canned digests. Two packages,
# because the two channels have different tag shapes:
#   .../pumpkin-fleet/papermc  version tags plus stable/stable-next (Ruling 4a's rollback)
#   .../pumpkin-os             BlueBuild's dated builds plus latest, stable one build behind
url=""
head=no
for a in "$@"; do
  case "$a" in http*) url="$a" ;; -*I) head=yes ;; esac
done
case "$url" in *pumpkin-os*) pkg=os ;; *) pkg=fleet ;; esac
digest_of() {
  if [ "$pkg" = os ]; then
    # B is what the newest canary build publishes; A is the previous build, which :stable
    # still points at until a human promotes - so A is the rollback digest.
    case "$1" in
      latest|20261126-45) echo 2222222222222222222222222222222222222222222222222222222222222222 ;;
      20261125-45|stable) echo 1111111111111111111111111111111111111111111111111111111111111111 ;;
    esac
    return
  fi
  case "$1" in
    26.3-258-p1) echo 1111111111111111111111111111111111111111111111111111111111111111 ;;
    26.3-258-p2|stable|stable-next) echo 2222222222222222222222222222222222222222222222222222222222222222 ;;
  esac
}
case "$url" in
  */token*)
    if [ "${STUB_MODE:-ok}" = token-fail ]; then
      # what real `curl -fsS` prints on a 401, so the diagnostic path is exercised honestly
      echo 'curl: (22) The requested URL returned error: 401' >&2
      exit 22
    fi
    echo '{"token":"faketoken"}' ;;
  */tags/list)
    if [ "${STUB_MODE:-ok}" = tags-fail ]; then
      echo 'curl: (22) The requested URL returned error: 500' >&2
      exit 22
    fi
    if [ "$pkg" = os ]; then
      # 20261123-45 is listed but serves no manifest, so the loop has to survive it here too.
      echo '{"name":"jcpowermac/pumpkin-os","tags":["latest","20261126-45","20261125-45","20261123-45","stable"]}'
    else
      echo '{"name":"jcpowermac/pumpkin-fleet/papermc","tags":["26.3-258-p1","broken-tag","26.3-258-p2","stable","stable-next"]}'
    fi ;;
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

run() { # run <image> <digest> [stub-mode] [target-tag] ; echoes the promote output, sets $code
  : > "$work/docker.log"
  out=$(PATH="$bin:$PATH" DOCKER_LOG="$work/docker.log" STUB_MODE="${3:-ok}" TARGET_TAG="${4:-}" \
        PREFIX=ghcr.io/jcpowermac/pumpkin-fleet IMAGE="$1" DIGEST="$2" GITHUB_TOKEN=fake \
        bash "$PROMOTE" 2>&1)
  code=$?
  log=$(cat "$work/docker.log")
}

run_os() { # run_os <digest> ; the os.yml promote-os command line: PREFIX/IMAGE split out of
  # pins.env's OS_IMAGE, TARGET_TAG="$OS_TAG". Nothing narrows the accepted digests.
  : > "$work/docker.log"
  out=$(PATH="$bin:$PATH" DOCKER_LOG="$work/docker.log" \
        PREFIX=ghcr.io/jcpowermac IMAGE=pumpkin-os TARGET_TAG=stable DIGEST="$1" GITHUB_TOKEN=fake \
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

# A failed token exchange has to say so: the pipeline must not red with a bare curl error.
run papermc "$A" token-fail
[ "$code" -ne 0 ] || fail "a failed token exchange was reported as success"
case "$out" in *"token exchange failed for jcpowermac/pumpkin-fleet/papermc"*) ;;
  (*) fail "token failure was not named: $out" ;;
esac
case "$out" in *"curl:"*) ;; (*) fail "no curl diagnostics survived: $out" ;;
esac
[ -s "$work/docker.log" ] && fail "a failed token exchange still reached the retag: $log"

# Same for a tags/list failure: "matches no published tag" must not be what a transport
# failure gets reported as.
run papermc "$A" tags-fail
[ "$code" -ne 0 ] || fail "a tags/list failure was reported as success"
case "$out" in *"could not list the published tags of jcpowermac/pumpkin-fleet/papermc"*) ;;
  (*) fail "tags/list failure was not named: $out" ;;
esac
[ -s "$work/docker.log" ] && fail "a failed tags/list still reached the retag: $log"

# The OS channel (Ruling 6a): the same rule, and pumpkin-os's published tags are BlueBuild's
# dated builds, so moving :stable back to a previous build is a rollback too.
run_os "$A"   # digest of the dated build 20261125-45, which is what :stable carries today
[ "$code" -eq 0 ] || fail "an older published OS build was refused: $out"
case "$out" in *20261125-45*) ;; (*) fail "OS promote did not name the published build: $out" ;; esac
case "$log" in *"--tag ghcr.io/jcpowermac/pumpkin-os:stable"*) ;;
  (*) fail "OS promote did not retag the OS channel: $log" ;;
esac
case "$log" in *"pumpkin-os@sha256:$A"*) ;; (*) fail "OS promote retagged the wrong digest: $log" ;; esac

run_os "$B"   # digest :latest carries: the forward promote
[ "$code" -eq 0 ] || fail "the canary digest was refused: $out"

run_os "$C"   # published under no tag of either package
[ "$code" -ne 0 ] || fail "an unpublished digest was accepted for the OS channel: $out"
[ -s "$work/docker.log" ] && fail "an unpublished OS digest still retagged stable: $log"
case "$out" in *refusing*) ;; (*) fail "OS refusal did not say why: $out" ;; esac

# TARGET_TAG is the channel pins.env points the nodes at: promoting must move that tag, not a
# hardcoded :stable, or changing OS_TAG would leave the nodes following a tag nothing moves.
run papermc "$B" ok stable-next
[ "$code" -eq 0 ] || fail "promote to a named target tag exited $code: $out"
case "$log" in *"--tag ghcr.io/jcpowermac/pumpkin-fleet/papermc:stable-next"*) ;;
  (*) fail "promote moved a hardcoded tag instead of the target: $log" ;;
esac
run papermc "$A" ok 'stable; curl evil'
[ "$code" -ne 0 ] || fail "a malformed TARGET_TAG was accepted: $out"
[ -s "$work/docker.log" ] && fail "a malformed TARGET_TAG reached the retag: $log"

echo "OK: promote accepts any published digest (including a rollback) and refuses the rest"
