"""verify — did the plugins this node's image declares actually load?

The laptop-side half of the plugin guard. It re-runs the node's own check first, because
a stale /run/fleet-verify.status is worse than none: it says `ok` about a container that
was replaced an hour ago. Then it prints what a human needs to judge the node — what
image is actually running, whether the unit is up, the verdict, the declared jars next to
the volume's contents, and the server's own answer to a status ping — and fails the run
when any node's verdict is not `ok`. That failure is the gate after a plugin bump.

Usage: uv run pyinfra inventory/hosts.py --group game_nodes tasks/verify.py
"""
from pyinfra import host
from pyinfra.operations import server

node = host.name
data_dir = f"/srv/{node}/data"

# The image name is not repeated here: gameserver.py writes one expected-jar file per
# image, so the glob is the node's own statement of which image it was deployed with.
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
echo "in {data_dir}: $(ls {data_dir} | tr '\\n' ' ')"
timeout 10 /usr/bin/python3 /usr/local/lib/fleet/mc-ping.py 127.0.0.1 25565 || echo "mc-ping: no answer on 25565"
""",
)

# Started here, not read blind: this is the only thing that makes the verdict describe the
# container that is running now. `fleet-verify.service` is a oneshot that always exits 0,
# so the statement that decides this run is the `[ ... = ok ]` on the verdict text.
server.shell(
    name=f"{node}: plugin verdict (fails the run unless ok)",
    commands=f"""\
systemctl start fleet-verify.service
status=$(cat /run/fleet-verify.status 2>/dev/null || echo "missing /run/fleet-verify.status")
echo "fleet-verify.status: $status"
[ "$status" = ok ]
""",
    _sudo=True,
)
