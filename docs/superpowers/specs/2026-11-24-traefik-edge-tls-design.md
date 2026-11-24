# Fleet edge: traefik VM, one Let's Encrypt wildcard, authentik gate

Date: 2026-11-24
Status: draft for approval
Supersedes: LXC 106 (community-scripts traefik), plan.md T4 "inspect existing
ACME setup" (there is none worth keeping)

## Intent

One box terminates TLS for every administrative service in the fleet, using a
**single publicly trusted Let's Encrypt wildcard**, and enforces **authentik**
authentication on every route. Users — on the LAN or anywhere else — reach
services over HTTPS only; there is no VPN and no self-signed certificate in the
path.

## Goals

- Valid (publicly trusted) TLS for all fleet admin UIs, internal clients included.
- One identity gate: authentik forward-auth on every route, MFA required.
- One place to add/remove a route; one cert to renew.
- Reproducible: the VM is built from the same golden template as the game nodes.
- No inbound service reachable except through traefik on 443.

## Non-goals

- No private CA; no root certificate to install on client devices.
- No per-node certificate issuance or key distribution.
- No VPN (explicit user decision).
- OPanel OIDC wiring (plan.md T7) — the routes are created here, the app later.
- Minecraft/Velocity auth (raw TCP, Velocity's own Mojang auth) — unchanged.

## Anti-goals

- authentik's own ports (9000/9443/6636) reachable from the WAN.
- Cockpit (or any UI) exposed without the authentik gate in front.
- A wildcard private key copied onto every node.
- `<node>.schcal.org` names appearing individually in Certificate Transparency logs.

## Constraints

- pve3 (172.31.12.99). Traefik at `10.10.10.10/24` (`IP_TRAEFIK`).
- Omada forwards: 443 → `10.10.10.10`, 24587 → Velocity. Nothing else.
- Zone `schcal.org` is registered at Squarespace (NS = `ns-cloud-*.googledomains.com`);
  **no official DNS API**. Comcast IPv4 `98.250.15.60` is stable.
- Fleet VM pattern: uCore, ignition-rendered from `templates/fleet.ign.butane`,
  rootless-unfriendly podman containers under systemd, pinned images in
  `containers/images.json`.

## Approaches considered

| Approach | Verdict |
|---|---|
| **traefik terminates + built-in ACME, one wildcard** | **Chosen.** One component, one cert, renewal built in, routes are the gate. |
| certmate as issuer, traefik serves | Rejected for now: adds a container, a volume, a deploy hook and a second ACME client (only one client may renew a cert) to solve a problem one wildcard doesn't have. Revisit when a second machine needs a cert traefik can't front, or there is more than one public domain. |
| Private CA (certmate ACME server) for internal names | Rejected: user wants LE-valid certs; a private CA means installing a root on every client. |
| Per-node certs + split-horizon DNS, direct `:9090` | Rejected: N certs, N keys, N renewals, names in CT logs. |
| **HTTP-01** | Rejected: cannot issue a wildcard; needs port 80 exposed. |
| **DNS-01 via Squarespace internal API + custom script** | Rejected: traefik's built-in ACME has no custom-script hook, so it forces a separate issuer; unofficial session-cookie endpoints. |
| **DNS-01 via `_acme-challenge` CNAME delegation into DuckDNS** | **Chosen mechanism.** Keeps `*.schcal.org` names; DuckDNS is a native lego/traefik provider; lego follows CNAMEs by default. One record set once in Squarespace. |
| DNS-01 with the zone moved to Cloudflare | Documented fallback if delegation misbehaves. |
| DNS-01 with a DuckDNS-only fleet zone | Documented fallback if the user prefers no schcal.org records in public DNS. |

## Design

### 1. The machine

- Delete LXC 106; create **VM 106** (VMID reused) with the **same import path as the
  game nodes** — `scripts/fleet/20-create-nodes.sh` imports a fresh qcow2 and
  renders per-node ignition; it does **not** clone 190, because on a cloned disk
  coreos-gpt-setup rewrites the root UUID mid-boot and Ignition runs against a
  half-mounted /sysroot. The edge box follows the same path with
  `SPIKE_HOSTNAME=traefik SPIKE_IP4=10.10.10.10/24`.
- Image: whatever `OS_IMAGE` pins (`ghcr.io/jcpowermac/pumpkin-os`), i.e. the same
  rebase the nodes run — one OS to track, not two. 2 vCPU, 2 GB RAM, 16 GB disk.
- sssd/jcallen login comes from the same ignition template. Cockpit on `.10` is
  **not** exposed through traefik (a UI that grants root on the proxy box); it is
  LAN-only like the nodes' SSH.
- Traefik's image tag is pinned in `pins.env` like other upstream versions;
  `containers/images.json` stays for house-built images with build args only.

### 2. traefik

- Podman container, image pinned in `containers/images.json`, `--network host`
  (needs to see LAN addresses and bind 443 directly), systemd unit, `--restart`.
- Volumes: `/etc/traefik/traefik.yml` (static), `/etc/traefik/dynamic/*.yml`
  (file provider, `watch: true`), `/etc/traefik/acme.json` (0600).
- Static config essentials:
  - `entryPoints.websecure :443`; `web :80` exists only to redirect, and **80 is
    never forwarded from the WAN**.
  - `certificatesResolvers.le.acme`: email, `storage: /etc/traefik/acme.json`,
    `dnsChallenge.provider: duckdns`, `delayBeforeCheck: 30`, env `DUCKDNS_TOKEN`.
  - `providers.file.directory: /etc/traefik/dynamic`, `watch: true`.
- The single certificate is declared once:
  `tls: {domains: [{main: schcal.org, sans: ["*.schcal.org"]}], certResolver: le}`.
  Every router uses `tls: {}`, so this cert is the default for all of them.

### 3. DNS

- **Squarespace (one-time, one record):**
  `_acme-challenge.schcal.org  CNAME  schcal-acme.<fleet>.duckdns.org`
  — lego follows the CNAME and writes the TXT through the DuckDNS provider.
  This record is the only Squarespace touch and never changes.
- **Public A records** (all → `98.250.15.60`): `auth`, `lobby`, `plugins`,
  `vanilla`, `neoforge`. They are public services now; the gate is authentik,
  not obscurity — but because the cert is a wildcard, **no individual name
  appears in CT logs**.
- **LAN clients** work whichever of these holds (no design dependency):
  (a) Omada `Network → LAN → LAN DNS` maps the names → `10.10.10.10`;
  (b) NAT hairpin/loopback on the gateway; (c) plain public resolution, which
  also works. Verify one during rollout; none of them changes anything else.
- **DuckDNS name keepalive:** a daily systemd timer runs
  `/update?domains=X&token=Y&ip=` (empty `ip` refreshes without changing the
  record). Cheap insurance against subdomain reclamation, which would only bite
  at the second renewal ~60 days in — and would take the whole fleet's TLS with it.
- Secrets: no secret lives in the repo. The DuckDNS token goes to
  `/root/traefik-duckdns-token` in CT 150 (0600), same place and same 0600 pattern
  as `/root/ldap-user-password`; the deploy script fetches it at render time,
  exactly as `20-create-nodes.sh` already does for the sssd bind credential. The
  LE account email is not a secret and lives in `pins.env`.

### 4. authentik gate

- **One** authentik proxy provider in **forward-auth (domain level)** mode with a
  wildcard external host — the pinned authentik supports domain-level forward
  auth, so one provider + one application covers every gated hostname instead of
  one pair per host.
- `forwardAuth` middleware: address
  `http://10.10.10.53:9000/outpost.goauthentik.io/auth/traefik`,
  `trustForwardHeader: true`, passing `X-authentik-username`, `-groups`,
  `-email`, `-name`, `-uid`, `-jwt`.
- A router for `/outpost.goauthentik.io/*` → authentik, on every gated host.
- **Embedded outpost `authentik_host` must change** from `https://127.0.0.1:9443`
  to `https://auth.schcal.org`: it is the URL browsers are redirected to for
  login. The *LDAP* outpost's `AUTHENTIK_HOST` is unrelated (outpost→core
  traffic) and stays on localhost.
- Routes: `auth.schcal.org` → `10.10.10.53:9000` (**ungated** — it is the IdP);
  `lobby|plugins|vanilla|neoforge.schcal.org` → `10.10.10.5x:9090` **gated**,
  websocket-enabled; OPanel routes added later from the same template.
- **Gate rule:** forward-auth is for apps that *cannot* speak OIDC (Cockpit).
  Apps that can (OPanel, and anything else later) authenticate with **native
  OIDC** against authentik instead, so their users log in once rather than twice.
- Policy: group `fleet-admins` (the existing LDAP group, currently just
  `jcallen`) for the cockpit routes; default-deny. Player-facing panels are an
  OPanel/OIDC decision (plan.md `mc-admins`), not an edge decision.

### 4a. The cockpit hop (node-side change — approved or not, see Open items)

Cockpit on the nodes is `quay.io/cockpit/ws` with its own self-signed TLS in
`/etc/cockpit/ws-certs.d/` and **no `cockpit.conf`**. Reverse-proxying it needs
both ends adjusted:

- **cockpit:** `/etc/cockpit/cockpit.conf` with
  `[WebService] Origins = https://<node>.schcal.org, wss://<node>.schcal.org` and
  `ProtocolHeader = X-Forwarded-Proto` — without `Origins` cockpit rejects the
  proxied Host/Origin, and without `ProtocolHeader` it thinks the request is
  plain HTTP. `AllowUnencrypted` is only needed if we drop its TLS.
- **traefik:** a `serversTransport` with `insecureSkipVerify: true` for the
  node hop (cockpit keeps its self-signed cert; only the LAN hop sees it). The
  browser always sees the LE wildcard.
- **Rejected alternative:** copying the wildcard key onto each node's
  `ws-certs.d/` — removes the self-signed hop but puts the fleet's one private
  key on four more machines.
- Enable **websockets** on these routers and raise read timeouts; cockpit's
  terminal and shell channels are long-lived.
- **Where `cockpit.conf` must land depends on the container** (the uCore
  cockpit/ws unit runs `/container/label-run` with `/:/host` mounted); the
  builder confirms whether the image reads container-local or host
  `/etc/cockpit`, then edits the ignition template so a rebuild keeps it.

### 5. Because authentik is now internet-facing

Zero trust, no VPN, HTTP(S) only — the IdP is public by design, so it gets
treated as a public service:

- MFA (TOTP/WebAuthn) enforced by policy; no password-only path.
- `rateLimit` middleware on `auth.schcal.org` (and on the outpost path).
- Security headers + HSTS on all routes; no HTTP served from the WAN.
- authentik's 9000/9443/6636 are never port-forwarded, only reachable from
  traefik and the LAN.
- Patching: authentik updates are now security updates.

### 6. Rollout

1. Delete LXC 106; create VM 106 from the golden template; verify sssd login and
   cockpit on `.10`.
2. traefik container + static config + empty `acme.json` (0600); start.
3. DuckDNS name, Squarespace CNAME, public A records, LAN DNS entries.
4. Request the wildcard; verify with `openssl s_client` and a browser from
   outside the LAN; confirm in crt.sh that only `*.schcal.org` is logged.
5. authentik proxy provider + outpost + `forwardAuth`; gate the four cockpit
   routes; confirm 302 → login → 200.
6. MFA policy, rate limits, headers.
7. Omada: 443 → `10.10.10.10` (already planned), remove any other forward to a
   fleet node except 24587 → Velocity.
8. Commit the config (`configs/traefik/`), pins, and a pyinfra task to deploy it.

### 7. Verification (must all pass)

- `curl -sI https://auth.schcal.org` → 200 from the internet, valid cert chain.
- `curl -sI https://lobby.schcal.org` → 302 to authentik; after login, 200.
- `openssl s_client -connect lobby.schcal.org:443` → `*.schcal.org`, LE issuer.
- From a LAN client with LAN DNS: same URL, same cert, traffic to `10.10.10.10`.
- `nmcli`/`ss` on `.10`: only 443 and 80 listening publicly; nothing on `.53`.
- Renewal dry-run: staging resolver issue succeeds; `acme.json` reloads without
  a traefik restart (file provider watch).

## Decisions made by the assistant (correctable)

- VMID 106 reused rather than 205.
- traefik in podman with `--network host` rather than macvlan.
- `_acme-challenge` CNAME delegation rather than moving the zone to Cloudflare
  (fallback documented; nothing else in the design changes if we switch).
- Cockpit on the proxy VM stays LAN-only; the four node cockpits are gated and
  public.
- No `www`→apex or other redirects; no observability stack on this box yet.
- Secrets follow the existing CT 150 root-only pattern (no new secrets scheme).
- Authentik hardening (MFA policy, rate-limit values, HSTS) is in scope but its
  exact shape is left to the builder.

## Open items for the user (blocking)

1. **Cockpit exposure** — gating cockpit publicly means a `cockpit.conf` change
   on all four game VMs plus the ignition template (§4a). Approve, or keep
   cockpit LAN-only and expose only auth (+OPanel later)?
2. **DuckDNS** — do you already have an account/name to use (and should the
   `_acme-challenge` CNAME point at it)? If not, one must be created; the token
   then lives in CT 150.
3. **Omada** — does the controller have `Network → LAN → LAN DNS` (and does NAT
   hairpin work)? Not blocking: any of the three LAN paths works, but it decides
   whether we add DNS entries at all.
4. **Gate rule** — confirm forward-auth only for non-OIDC apps (cockpit), with
   OPanel using native OIDC (§4). Speak now if you want every route edge-gated.

## Left to the builder

- Exact traefik image tag and podman unit shape; the DuckDNS name to register.
- The four cockpit routers' websocket options and timeouts.
- Where `cockpit.conf` must live for the containerised cockpit (§4a).
- Whether to gate Proxmox's own UI (`:8006`) the same way — deferred; it needs
  websocket and long-lived console sessions.
- Renewal alerting (a cron checking `acme.json` expiry → ntfy) — nice to have.
- Backup of `acme.json` and the DuckDNS token (they are the only state).
