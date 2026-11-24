# Traefik VM + fleet certificates (in progress)

Started 2026-11-24, after the sssd/cockpit phase. Working notes — this becomes the
design doc once the open questions are answered.

## Decisions made

- **The traefik machine is being recreated as a VM**, not the existing LXC 106.
  LXC 106 is a community-scripts traefik container (Debian unprivileged, 512MB,
  DHCP, `onboot=0`, stopped) — nothing to preserve, delete and rebuild.
- **Base: uCore VM from the golden template (190)** (recommended; the Debian-VM
  alternative was considered and set aside). Same ignition pipeline as the game
  nodes; a new render of `templates/fleet.ign.butane` with hostname `traefik`,
  IP `10.10.10.10/24` (`IP_TRAEFIK` in pins.env). Bonus: sssd + a cockpit on `.10`
  for the box itself.
- **Traefik runs as a podman container** (the fleet pattern used for cockpit on
  the game nodes; image pinned in `containers/images.json`), config + ACME store
  as volumes. Native-binary/systemd is the fallback if the container proves
  awkward.
- **Traefik remains the only public edge** (plan.md T4): Omada forwards 443 →
  `10.10.10.10` and 24587 → Velocity. Everything that needs a valid cert is
  served by traefik; internal sites are routes behind it (see Serving model).
- **One `*.schcal.org` wildcard, issued via DNS-01** (see Public cert shape).
  DNS-01 is mandatory for a wildcard; no inbound ports.

## Status: sections below are chronological. The live decisions are
"Public cert shape", "Serving model: edge", "Provider decision" and the
Critical path.

## Cert architecture (superseded first draft — kept for the trail)

- **Public names** → Let's Encrypt via DNS-01.
- **LAN-only names** (cockpits, direct authentik `.53:9443`) → plain LE can't
  issue for a name that isn't in public DNS; the answer we settled on is to
  **serve the public names on the LAN through traefik** (edge model), not to
  add a private CA.

## Certmate investigation (findings, 2026-11-24)

Repo/image: **fabriziosalmi/certmate** (Docker Hub `fabriziosalmi/certmate:latest`,
or a shipped `certmate.service` systemd unit). Python app (gunicorn), web UI +
REST API + CLI/Python SDK, bearer-token auth (`API_BEARER_TOKEN`). Listens 8000.

- **CA providers**: Let's Encrypt, LE staging (separate CA entry, for dry runs),
  Sectigo, … and **per-certificate CA choice**. EAB supported.
- **DNS-01** with 29 providers (Cloudflare, DuckDNS, No-IP, DynDNS, …) plus a
  **custom-script provider** (your script creates/removes the TXT record) for
  anything exotic. DNS-01 means **no inbound ports**: validation happens in the
  DNS zone, certmate only needs outbound HTTPS.
- **ACME server mode** ("private CA", off by default): CertMate's own root issues
  for internal names and IPv4 addresses; **standard ACME clients** (certbot, lego,
  acme.sh, cert-manager) enroll with a per-client **EAB key** against
  `https://<certmate>/acme/directory`. Default validity 45 days (1–365).
  Self-signed on certmate's front is fine for the clients (certbot
  `REQUESTS_CA_BUNDLE` / acme.sh `--ca-bundle`). Must be served over HTTPS
  (RFC 8555), `BEHIND_PROXY=true` under a TLS-terminating proxy.
  **Trust is per-client**: the private root must be installed in the trust store
  of whatever browser/machine connects to services using the issued certs
  (i.e. our laptops for cockpit).
  Note: the ACME-server root is a *different* root from certmate's client-cert
  (mTLS) root — don't conflate them.
- **Storage**: local by default — `certificates/<domain>/{privkey.pem,chain.pem,…}`
  (`CERTMATE_CERT_DIR` to move it); settings/metadata/backups on local disk.
  Backup = passphrase-protected export.
- **Deploy hooks**: shell commands (≤1024 chars) run **on the certmate host**
  after created/renewed/revoked, with `CERTMATE_DOMAIN`, `CERTMATE_KEY_PATH`,
  `CERTMATE_CHAIN_PATH` env vars; global + per-domain hooks, maintenance windows,
  history, test endpoint. Base image has sh/bash/curl/openssl only — **no ssh**
  (build a wrapper image, or share a volume).

### Candidate roles in the fleet (RESOLVED — see "Provider decision")

- (a) sole cert authority: LE DNS-01 delivering to traefik by shared volume.
- (b) internal CA only; traefik keeps its own built-in ACME.
- (c) not needed — traefik's own ACME issues and holds the cert.
  **← chosen, unless a second cert consumer appears.**

### Public cert shape (user decision, 2026-11-24)

**One Let's Encrypt wildcard cert**: `*.schcal.org` — and nothing else.
Velocity is raw TCP (24587) and never presents TLS, so the `a.velocity`
SAN is unnecessary (dropped). LE allows up to 100 names per cert if ever
needed; the wildcard covers exactly one label (`lobby.schcal.org` ✓,
`a.velocity.schcal.org` ✗) — keep future names flat.

- User wants **valid Let's Encrypt** certs internally, not a private CA:
  "internal" is achieved by serving the public subdomain names on the LAN
  (edge model below), not by a self-issued root. Private-CA mode = not
  needed unless a name exists nowhere in public DNS.
- **Wildcard forces DNS-01** (a TXT record in the wildcard's zone).
  Port-80 HTTP-01 cannot issue a wildcard. **Which zone the TXT goes in is the
  open provider decision below** — schcal.org/Squarespace is only one option.

### Zone state (schcal.org)

- Registered via **Squarespace Domains**; NS = `ns-cloud-*.googledomains.com`
  (Squarespace runs on Google). Dashboard-managed; **no official DNS API** —
  known automation is the dashboard's internal endpoints with a session
  cookie ("browser console bulk add" trick), unverified.
- Comcast IPv4 **98.250.15.60 is very stable** (user) — static A records,
  DDNS is insurance, not a requirement.
- Existing records (all A → 98.250.15.60): `www`, `mx`, `auth`, `lobby`,
  `a.velocity`. Apex has no A.

### CT-log trade-off (researched 2026-11-24 — mendhak, certhub, LE community)

LE-for-internal is a standard homelab/enterprise pattern; the central
trade-off is **Certificate Transparency**: every issued cert's names are
publicly searchable (crt.sh). Community mitigations, in order:
1. **Wildcard cert** (our choice) — hides specific service hostnames;
   mendhak: enumeration risk > compromised-wildcard risk for internal use.
2. **Flat subdomain structure** — wildcard is one label only.
3. **Dedicated separate domain** — primary domain never appears in CT at
   all; also enables CAA pinning (LE-only / wildcard-only). Lever, not need:
   schcal.org already resolves publicly (www/mx/a.velocity → 98.250.15.60).

Our residual CT exposure: one `*.schcal.org` entry each renewal (~60 days).

### Provider decision: pure traefik vs certmate (2026-11-24)

**traefik's own ACME can do this whole job.** Confirmed: built-in ACME
certificate resolver, `dnsChallenge.provider = "duckdns"` (env `DUCKDNS_TOKEN`),
wildcard via `domains: [{main: X.duckdns.org, sans: [*.X.duckdns.org]}]`, cert
stored in `acme.json`, renewed automatically, served live. For **one wildcard
with traefik as the only consumer**, certmate adds nothing on the critical path.

What certmate adds (real, but only pays off at scale):
- UI/API/CLI to see every cert's expiry, force renew, revoke; audit log.
- Alerts (email/Slack/Discord/ntfy/webhook) + weekly digest; deploy history.
- Backup/restore of the cert store; certificate discovery (finds certs you
didn't issue); revocation checking; domain health.
- **Multiple consumers / many domains / per-cert CA choice** — the case where a
  central issuer beats a single proxy. Private CA (ACME server) exists too.

What certmate costs: another container + volume + token-authed UI to secure,
a **second ACME client** (only one client may renew a given cert), and a
deploy-hook/shared-volume delivery step to traefik.

**Decision: pure traefik ACME now.** Revisit certmate when either (i) a second
machine needs a cert traefik can't front (e.g. authentik serving TLS itself on
`.53:9443`), or (ii) more than one public domain. Rule if it returns: one ACME
client per certificate, never both.

### Zero trust: authentik forward-auth at traefik (2026-11-24)

Enforcement lives at traefik, not per app: every UI route gets a `forwardAuth`
middleware → authentik's proxy outpost
(`http://<outpost>/outpost.goauthentik.io/auth/traefik`), plus a router for
`/outpost.goauthentik.io/*` → outpost. Documented authentik pattern (proxy
provider + forward auth). One gate, one cert, one audit trail.

**authentik exposure:** it needs (1) reachability from traefik to the outpost
(internal) and (2) browser reachability to `authentik_host` (the outpost
redirects users there) — **not** internet exposure. Two postures:
- **A. Everything private (chosen, pending user ok)**: admins on LAN or VPN
  (WireGuard/Tailscale); `auth.schcal.org` in LAN DNS only; authentik never on
  the WAN. External surface = Minecraft 24587 and nothing else.
- **B. Publish authentik**: only if remote admin without VPN is required; makes
  the IdP internet-facing → needs MFA, rate limits, IP/geo policy, fast patching.
  Flippable later; cert design unchanged either way.

Casualties: **cockpit double-prompts** (forwardAuth gate, then its own PAM login;
no clean SSO into cockpit-bridge). **Raw TCP** (Minecraft, SSH) cannot be
forward-authed — gated by network policy/VPN.

### Serving model: edge (recommended, pending user ok)

Everything through traefik on 443: `lobby.schcal.org` → `10.10.10.54:9090`
(cockpit, websocket pass-through), same for the other three nodes, auth →
authentik, OPanel routes later. Single wildcard cert = traefik's default
cert for all routes; new subdomain = new route, no new cert. Requires Omada
**NAT loopback / hairpin** for LAN clients hitting the WAN IP (verify on the
router; fallback: hosts override on the laptop). Alternative (split-horizon
DNS + node-side certs, direct `:9090` access) kept in reserve — more
plumbing, same wildcard cert.

### Exposure

None inbound beyond the plan: 443→traefik + 24587→velocity. DNS-01 =
outbound only. Port 80 never needs to open (wildcard rules out HTTP-01
anyway).

### Critical path

**Spike: how do we write the TXT?** Three paths:

1. **DuckDNS zone (recommended)** — free name (e.g. `pumpkin-xyz.duckdns.org`),
   **native provider in both traefik and certmate** (API is one curl:
   `/update?domains=X&token=Y&txt=<v>`). schcal.org stays out of CT entirely
   (= the "dedicated separate domain" recommendation for $0). Cost: names are
   `lobby.<name>.duckdns.org`. Reference: github.com/ietxaniz/https-setup-local-networks
   (same pattern: DuckDNS + DNS-01 + nginx edge).
2. **Squarespace internal API script** (custom-script provider) — keeps
   `*.schcal.org` names; session cookie + unofficial endpoints; the fragile one.
   Note: a custom script is only usable with certmate/certbot — traefik's
   built-in ACME has no custom-script hook, so this path forces a separate
   issuer.
3. **Move schcal.org NS to Cloudflare** (free) — keeps the names, one-time
   record migration, native provider in both.

### Still needed from the user

1. **Zone choice**: DuckDNS name vs `*.schcal.org` (Squarespace spike) vs NS→Cloudflare,
   OR traefik + `_acme-challenge` **CNAME delegation** into DuckDNS (lego follows
   CNAMEs by default) — keeps `*.schcal.org` names with a native provider.
2. **Internal access**: everything by name through traefik, or keep some direct
   port access (then those need the cert distributed)?
3. **authentik public or internal-only** (posture A vs B above).
4. VMID 106 vs 205.
