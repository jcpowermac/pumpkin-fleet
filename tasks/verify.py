"""verify — did the plugins this node's image declares actually load?

The laptop-side half of the plugin guard. It re-runs the node's own check first, because
a stale /run/fleet-verify.status is worse than none: it says `ok` about a container that
was replaced an hour ago. Then it prints what a human needs to judge the node — what
image is actually running, whether the unit is up, the verdict, the declared jars next to
the volume's contents, and the server's own answer to a status ping — and fails the run
when any node's verdict is not `ok`, or when the node's expected-jar file no longer says
what `containers/images.json` declares.

Usage: uv run pyinfra inventory/hosts.py --group game_nodes tasks/verify.py
"""
import json
from pathlib import Path

from pyinfra import host
from pyinfra.operations import server

REPO = Path(__file__).resolve().parent.parent
node = host.name
data_dir = f"/srv/{node}/data"

# The node's expected-jar file is deploy-time state: `podman-auto-update.timer` can move
# the image long after the last deploy, and then the file describes a plugin set the image
# no longer ships. So the laptop carries what a deploy today would write, per image, and
# the node's file is compared against it instead of being trusted.
# Rendered as `tr '\n' ' '` output of that file, hence the trailing space.
DECLARED = {
    image["name"]: "".join(f"{name} " for name in sorted(p["install_as"] for p in image.get("plugins", [])))
    for image in json.loads((REPO / "containers" / "images.json").read_text())
}
WANT_CASES = "".join(f'    {name}) want="{jars}" ;;\n' for name, jars in DECLARED.items())

# The image name is not passed in: gameserver.py writes one expected-jar file per image,
# so the glob is the node's own statement of which image it was deployed with.
server.shell(
    name=f"{node}: running image, unit, plugins, mc-ping",
    commands=f"""\
echo "== {node} =="
if podman container exists {node}; then
  podman inspect --format 'image: {{{{.ImageName}}}} {{{{.ImageDigest}}}}' {node}
else
  echo "image: no {node} container"
fi
echo "unit: $(systemctl --user is-active {node}.service)"
echo "expected jars $(ls /etc/fleet/expected-plugins.*.txt): $(cat /etc/fleet/expected-plugins.*.txt | tr '\\n' ' ')"
echo "jars in {data_dir}: $(find {data_dir}/plugins {data_dir}/mods -maxdepth 1 -type f -name '*.jar' -printf '%f ' 2>/dev/null)"
timeout 10 /usr/bin/python3 /usr/local/lib/fleet/mc-ping.py 127.0.0.1 25565 || {{ echo "mc-ping: no answer on 25565 — the server is not answering"; exit 1; }}
""",
)

# Started here, not read blind: this is the only thing that makes the verdict describe the
# container that is running now. The file is removed first so a check that fails to start
# cannot leave an earlier `ok` behind to be read as this run's verdict.
server.shell(
    name=f"{node}: plugin verdict (fails the run unless ok and in step with the manifest)",
    commands=f"""\
rm -f /run/fleet-verify.status
systemctl start fleet-verify.service || exit 1
status=$(cat /run/fleet-verify.status 2>/dev/null || echo "missing /run/fleet-verify.status")
echo "fleet-verify.status: $status"
[ "$status" = ok ] || exit 1
for f in /etc/fleet/expected-plugins.*.txt; do
  [ -e "$f" ] || {{ echo "drift: no expected-plugins file in /etc/fleet"; exit 1; }}
  image=${{f##*/expected-plugins.}}
  image=${{image%.txt}}
  case "$image" in
{WANT_CASES}    *) echo "drift: $f is not for any image in containers/images.json"; exit 1 ;;
  esac
  have=$(tr '\\n' ' ' < "$f")
  if [ "$have" != "$want" ]; then
    echo "drift: $f says [$have] but containers/images.json declares [$want]"
    exit 1
  fi
done
""",
    _sudo=True,
)
