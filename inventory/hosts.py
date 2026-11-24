# pyinfra v3 inventory — pumpkin fleet on pve3.
# Module-level list vars are groups; items are "hostname" or ("hostname", {data}).
# Uncomment a host when the task that creates it is done.

pve = ["pve"]

game_nodes = [
    ("lobby", {"ssh_hostname": "10.10.10.54", "ssh_user": "core"}),
    # ("plugins", {"ssh_hostname": "10.10.10.55", "ssh_user": "core"}),
    # ("vanilla", {"ssh_hostname": "10.10.10.56", "ssh_user": "core"}),
    # ("neoforge", {"ssh_hostname": "10.10.10.57", "ssh_user": "core"}),
]

ucore = [
    ("ucore-golden", {"ssh_hostname": "10.10.10.99", "ssh_user": "core"}),
]

infra = [
    # ("velocity", {"ssh_hostname": "10.10.10.52"}),
    # ("traefik", {"ssh_hostname": "10.10.10.10"}),
    # ("authentik", {"ssh_hostname": "10.10.10.53"}),
]
