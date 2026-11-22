"""ping — connectivity gate for every inventory host."""
from pyinfra import host

for h in host:
    h.ssh_ping()
