# LDAP / authentik hand-off

Written 2026-11-23 for continuation by another model. Self-contained: everything you
need to reproduce the failure and the state it's in. No secrets in this file — only
paths (all `0600`, on the host they live on).

**Goal:** a Minecraft-fleet admin (`jcallen`) logs into Cockpit on a game node with their
authentik account.

**Where it stands: the LDAP bind works.** `ldapsearch` with the test user returns
`result: 0 Success`; a wrong password now correctly returns `Invalid credentials (49)`. Root
cause and fix are in **§10**. Nothing downstream (sssd, cockpit) is configured yet —
§8 is the next phase.

---

## 1. Access

```sh
ssh root@172.31.12.99                 # pve3 Proxmox host
pct exec 150 -- bash                  # authentik container (Debian 13, unprivileged)
```

| what | where |
|---|---|
| authentik container | CT 150, hostname `authentik`, `10.10.10.53/24`, gw `.1`, bridge `vmbr1`, 4c/8G, rootfs `local-zfs:16G` |
| authentik version | 2026.8.6, installed by community-scripts `ct/authentik.sh` (builds from source) |
| web UI | `https://10.10.10.53:9443` (reachable from the operator laptop; self-signed cert) |
| admin login | user `akadmin`, password in **`/root/authentik-admin-password`** (inside CT 150) |
| API token (superuser) | **`/root/authentik-api-token`** (inside CT 150) |
| test user password | **`/root/ldap-user-password`** (inside CT 150, format: `jcallen <password>`) |
| outpost token | **`/root/.outpost-token`** (inside CT 150) |
| game node used for testing | `lobby` = VM 201, `10.10.10.54`, user `core` (sudo, SSH key) |

Helper scripts already written (all inside CT 150, also in the repo under
`build/`): `setup-ldap.py` (provider/app/outpost/user), `setup-ldap-flow.py`
(bind flow), `chk*.py` (diagnostics).

```sh
# every API call needs BOTH headers; without Accept you get DRF's HTML page
TOK=$(pct exec 150 -- cat /root/authentik-api-token)
curl -s -H "Authorization: Bearer $TOK" -H 'Accept: application/json' \
  http://127.0.0.1:9000/api/v3/...      # from inside CT 150
```

`ldapsearch` lives in the CT (installed via `apt-get install ldap-utils`).

---

## 2. What exists now

| object | value |
|---|---|
| LDAP provider | `fleet-ldap` (pk **1**), `base_dn=dc=ldap,dc=goauthentik,dc=io`, `bind_mode=direct`, `search_mode=direct` |
| application | `Fleet LDAP`, slug `fleet-ldap` |
| outpost | `fleet-ldap-outpost`, pk `843d9efa-8726-45c0-938e-975c0624c9a4`, type `ldap`, providers `[1]` |
| outpost unit | `authentik-ldap.service`, `EnvironmentFile=/etc/default/authentik_ldap` (**underscore**) |
| listening | LDAP `:3389`, LDAPS `:6636` (TLSv1.3, self-signed "authentik default certificate") |
| bind flow | slug **`ldap-bind`** |
| user | `jcallen`, active, group `fleet-admins`, password verified with `u.check_password()` → `True` |

Note the TLS port is **6636**, not 636.

`/etc/default/authentik_ldap`:
```
AUTHENTIK_HOST="https://127.0.0.1:9443"
AUTHENTIK_INSECURE="true"
AUTHENTIK_TOKEN=<outpost token>
```

---

## 3. The failing thing

```sh
pct exec 150 -- bash -s <<'CT'
read -r U P < /root/ldap-user-password
LDAPTLS_REQCERT=never ldapsearch -x -H ldaps://127.0.0.1:6636 \
  -D "cn=$U,ou=users,dc=ldap,dc=goauthentik,dc=io" -w "$P" \
  -b "ou=users,dc=ldap,dc=goauthentik,dc=io" "(cn=$U)" dn cn mail
CT
```

returns `ldap_bind: Operations error (1)`.

`journalctl -u authentik-ldap -o cat | tail` shows, in order:

```
{"bindDN":"cn=jcallen,ou=users,dc=ldap,dc=goauthentik,dc=io","event":"Bind request",...}
{"bindDN":"cn=jcallen,ou=users,dc=ldap,dc=goauthentik,dc=io","event":"User has access",...}
{"bindDN":"cn=jcallen,...","error":"403 Forbidden  (Authentication credentials were not provided.)","event":"failed to get user info","level":"warning",...}
{"bindDN":"cn=jcallen,...","event":"Bind request",...}
```

**So: the flow authenticates the user ("User has access"), and then the outpost's own
follow-up "get user info" API call is rejected with DRF's `NotAuthenticated`.**

That is the single blocker. `ldapsearch` then reports `Operations error` because the
search cannot be served without the user's attributes.

---

## 4. How it got here — five bugs already fixed (do not re-debug these)

Everything below is verified fixed. If something regresses, these are the causes.

1. **Bind failed `Invalid credentials (49)` + outpost log `Flow does not apply to current
   user.`** The provider was pointing at `default-authentication-flow`, which carries OOBE
   policy bindings and cannot drive a machine bind.

2. **Field name is `authentication`, not `authentication_requirement`.**
   `flows/instances` has NO `authentication_requirement` field. A PATCH with that name is
   silently ignored and reads back `null`. Confirm the real schema:
   ```sh
   curl -s -H "Authorization: Bearer $TOK" -H 'Accept: application/json' \
     http://127.0.0.1:9000/api/v3/schema/ > /tmp/sch
   # Flow.authentication -> AuthenticationEnum: none|require_authenticated|require_unauthenticated|...
   ```

3. **`policy_engine_mode: any` with zero policies evaluates to FALSE** (`any` needs at
   least one passing binding). A flow with no policies must be `all`.

4. **The failure was actually the *authorization* flow.**
   `default-provider-authorization-implicit-consent` ships with
   `authentication: require_authenticated` — impossible during a bind, where the request
   is anonymous. It was changed to `authentication: none`. Verify with:
   ```sh
   journalctl -u authentik-server | grep -i 'not applicable'
   # earlier it named: "flow_slug": "default-provider-authorization-implicit-consent"
   ```

5. **The bind flow had no login stage, so no session was created.** A bind flow needs
   identification → password → **user_login**. It now has all three.

Stage-binding endpoint is **`/api/v3/flows/bindings/`** (NOT `/flows/stage-bindings/` —
that 404s). POST body:

```json
{"target":"<flow pk>","stage":"<stage pk>","order":30,
 "evaluate_on_plan":true,"re_evaluate_policies":false}
```

`evaluate_on_plan` **must be true** or you get
`Either evaluation on plan or evaluation on run must be enabled`.

---

## 5. Current `ldap-bind` flow

```
order 10  default-authentication-identification  (bb559acf-…)
order 20  default-authentication-password        (f52cde46-…)
order 30  default-authentication-login           (18aafef7-…)
authentication: none | policy_engine_mode: all | designation: authentication
denied_action: message_continue
```

Note: `default-provider-authorization-implicit-consent` is **empty** (no stages) and now
logs `Flow is empty` — that is expected and tolerated (the bind proceeds past it).

---

## 6. Ruled out (don't spend time here)

- **Not the password.** `User.objects.get(username="jcallen").check_password(pw)` → `True`,
  `is_active=True`, `has_usable_password=True`, group `fleet-admins`.
- **Not the DN format.** The outpost parses `cn=jcallen,ou=users,…` correctly — it appears
  verbatim in the log as `bindDN`.
- **Not TLS.** `openssl s_client -connect 127.0.0.1:6636` → `CONNECTION ESTABLISHED`,
  `TLSv1.3`; the LDAPS listener starts fine.
- **Not `AUTHENTIK_HOST` being plain HTTP.** It was already `https://127.0.0.1:9443`.
- **Not `bind_mode`.** Tried `cached`/`cached` — same failure. Reverted to
  `direct`/`direct`, which is the documented setting for this.
- **Not the metrics port clash.** `:9300 address already in use` in the log is cosmetic
  (the outpost also binds a unix metrics socket).

---

## 7. Best next step (executed — see §10)

**Find out what the outpost actually sends on that "get user info" call.** Do it from the
wire, not by inference — that single observation splits the problem in two:

```sh
# in CT 150, watch the outbound API calls while triggering a bind
pct exec 150 -- bash -c 'timeout 20 tcpdump -s0 -A -i lo tcp port 9443' &
# then run the ldapsearch from §3
```

Look at whether the failing request carries **any** `Authorization:` header, and which
path it hits (`/api/v3/core/users/me/` vs `/api/v3/core/users/<pk>/`). Then:

- **No header at all** → the outpost isn't attaching the token/session to that client.
  Compare against a call that *does* work (the provider fetch on startup). Consider
  whether the session cookie from the flow is `Secure` and being dropped, or whether the
  outpost expects `AUTHENTIK_TOKEN` to carry extra permissions.
- **Header present but a session token** → the session from the `user_login` stage isn't
  valid/persisted. Check `authentik_core_session` rows and the flow's
  `SESSION_KEY_PLAN`/session creation in `authentik/stages/user_login/`.
- Also worth pricing in: read `/opt/authentik` (full Python source is on disk, installed
  from source) — `grep -rn "failed to get user info"` will not hit Python (that string is
  in the Go outpost binary), but the *server-side* view of the failing request is in
  `journalctl -u authentik-server` with `?query` params if you raise log level.

Alternative if the outpost turns out to be genuinely broken in 2026.8.6: pin an older
authentik tag, or drive sssd from a manual LDAP bind account instead of `direct` mode.

---

## 8. What comes after LDAP works (done — see §11)

1. ✅ **sssd on the node** — `/etc/sssd/sssd.conf` written on all four nodes, sssd
   enabled. The three non-obvious settings are in §11 (`ldap_user_name = cn`,
   `ldap_schema = rfc2307bis`, SELinux port label for 6636).
2. ✅ `authselect select sssd` — turned out to already be the default profile on
   FCOS/uCore; the template re-asserts it.
3. ✅ `getent passwd jcallen` resolves on all four nodes (and PAM login verified).
4. ⬜ Cockpit login (browser). Cockpit is the uCore **container** on **:9090**.
   Its privileged mode logs in by SSHing to the host and using host users/passwords —
   and uCore already ships `/etc/ssh/sshd_config.d/90-enable-localhost-passwords.conf`
   (`Match Address 127.0.0.1,::1 / PasswordAuthentication yes`) plus the `Include` line.
   Verify with `sudo sshd -T -C addr=127.0.0.1,user=core` → `yes`, and
   `-C addr=10.10.10.1,user=core` → `no`. **Do not add another drop-in; it is redundant.**

---

## 9. Environment notes that will otherwise waste your time

- **Any `grep` on `/etc/ssh/sshd_config` or `/etc/sssd/**` must use `sudo`.** Running as
  `core` fails with `Permission denied`; because the output is empty it reads exactly like
  "the line is absent". This cost hours earlier in the project.
- `rpm-ostree rebase` **no-ops on an unchanged ref** (`Old and new refs are equal`). To
  pick up a rebuild of the same tag, rebase by `@sha256:<digest>`.
- Nested quoting through `ssh … 'pct exec … "…"'` mangles easily. Prefer
  `pct exec 150 -- bash -s <<'CT' … CT` and write Python to a file with `pct push`.
- The authentik `manage.py` shell prints
  `229 objects imported automatically` on stderr; filter it out when parsing.
- ~~`/` on the authentik web UI redirects to `/setup` …~~ **Resolved (2026-11-23):**
  the OOBE wizard had never been completed, so `Setup` flag was `False` and `/setup`
  ran the `initial-setup` flow — whose OOBE policy `default-oobe-password-usable`
  fails for anonymous requests (`policy_engine_mode: any` + one failing policy =
  non-applicable) → 500 `FlowNonApplicableException`. Fixed by completing OOBE the way
  the wizard's `PostSetupStageView` does: `Setup.set(True)`, disable OOBE
  `BlueprintInstance`s, set `initial-setup` to `REQUIRE_SUPERUSER`, and set the tenant
  `base_url` to `https://10.10.10.53:9443`. `/` now lands on the login page (200).

---

## 10. Resolution (2026-11-23, continued session)

**Root cause — the bind flow that actually runs is the provider's `authorization_flow`,
not its `authentication_flow`.**

The Go LDAP outpost does not read `authentication_flow` at all. `refresh.go` builds the
provider instance with `authenticationFlowSlug: provider.BindFlowSlug`, where the
`bind_flow_slug` comes from the outpost config endpoint
`/api/v3/outposts/ldap/`. On the server side
(`authentik/providers/ldap/api.py`, `LDAPOutpostConfigSerializer`):

```python
bind_flow_slug = CharField(source="authorization_flow.slug")
```

So every LDAP bind executed the provider's **authorization flow**, which was still the
stock **empty** `default-provider-authorization-implicit-consent` (it had only been fixed
to `authentication: none`, which is what let it "pass"). An empty flow returns a redirect
challenge immediately → `passed=true`, **no `user_login` stage ever ran, no
`authentik_session` cookie was ever set**. `check_access` then passed for the anonymous
request (empty flow = passes for anyone), and the outpost's follow-up
`GET /api/v3/core/users/me/` went out with **no credentials at all** — confirmed server
side: `auth_via: "unauthenticated", "user": "", "status": 403,
user_agent: "goauthentik.io/outpost/2026.8.6"`. (The Go client carries the flow's session
cookie in an `http.CookieJar`; there was also no Bearer header —
`TokenAuthentication` only reads `Authorization: Bearer`, and the
`X-authentik-outpost-token` header only feeds the remote-IP override middleware.)

This also explains why the `ldap-bind` flow (identification → password → login,
`authentication: none`, `policy_engine_mode: all`) never appeared in the server request
log during a bind.

**Fix (one API PATCH):**

```sh
TOK=$(pct exec 150 -- cat /root/authentik-api-token)
curl -s -X PATCH -H "Authorization: Bearer $TOK" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{"authorization_flow": "0347f4f1-d5e9-414f-b6bc-2c61df0139a4"}' \
  http://127.0.0.1:9000/api/v3/providers/ldap/1/   # ldap-bind flow pk
```
(run inside CT 150), then `systemctl restart authentik-ldap`.

**Verified after the fix:**
- `ldapsearch` (§3) → `result: 0 Success`, returns `cn=jcallen,ou=users,…` + `mail`.
- Same bind with a **wrong password** → `ldap_bind: Invalid credentials (49)` —
  credentials are genuinely checked now (before, the empty flow would have passed
  anything, so don't trust pre-fix negative tests).

**State changes vs. §2/§5:** provider `fleet-ldap` now has
`authentication_flow = ldap-bind` **and** `authorization_flow = ldap-bind` (same flow,
pk `0347f4f1-d5e9-414f-b6bc-2c61df0139a4`).
`default-provider-authorization-implicit-consent` is no longer referenced by the provider
at all. The §5 note about it logging `Flow is empty` is stale.

**Gotchas hit in this session:**
- `tcpdump` was not installed in CT 150 (`apt-get install -y tcpdump`). Loopback traffic
  to 9443 is TLS, so wire capture shows ciphertext — the **server-side** request log
  (`journalctl -u authentik-server`, `authentik.asgi` lines carry `event`/`status`/
  `auth_via`/`user_agent`) was the right tool; no log-level change needed.
- The provider list endpoint is `/api/v3/providers/ldap/` but **does not expose
  `bind_flow``; the flow wiring only shows up at `/api/v3/outposts/ldap/`
  (`bind_flow_slug`) and as `authentication_flow`/`authorization_flow` on the provider.
  `/api/v3/flows/` is 404 — it's `/api/v3/flows/instances/`.

## 11. sssd / game-node phase (2026-11-24)

Done on all four nodes (201–204): `getent passwd jcallen`, `getent group fleet-admins`
(with member list), and PAM login (`sudo -Su jcallen true` with the LDAP password) all
pass. Cockpit login is the remaining browser-side step (§8.4).

**The config** (identical on every node; now baked into
`templates/fleet.ign.butane` so new nodes get it at first boot, and
`scripts/fleet/20-create-nodes.sh` fetches the bind credential from CT 150's
`/root/ldap-user-password` at render time):

```ini
[domain/ldap.goauthentik.io]
id_provider = ldap
auth_provider = ldap
chpass_provider = ldap
ldap_uri = ldaps://10.10.10.53:6636
ldap_user_search_base = ou=users,dc=ldap,dc=goauthentik,dc=io
ldap_group_search_base = ou=groups,dc=ldap,dc=goauthentik,dc=io
ldap_user_name = cn            # (1)
ldap_schema = rfc2307bis       # (2)
ldap_default_bind_dn = cn=jcallen,ou=users,dc=ldap,dc=goauthentik,dc=io
ldap_default_authtok = <jcallen's password>
ldap_tls_reqcert = never
enumerate = true               # (3)
default_shell = /bin/bash
cache_credentials = true
```

Plus two node steps that are **not** in the conf (template runs them post-rebase):
`semanage port -a -t ldap_port_t -p tcp 6636` (4) and
`systemctl enable --now sssd` (authselect is already on the sssd profile).

**The gotchas — each one cost a full debug round, don't re-debug them:**
1. **`ldap_user_name = cn`.** authentik's LDAP user entries put the *username* in `cn`;
   the `uid` attribute is a **UUID**. sssd's default (`uid`) finds nothing. Symptom:
   everything else looks right, `getent passwd` just misses.
2. **`ldap_schema = rfc2307bis`.** authentik stores group members as full DNs in the
   group's `member` attribute (RFC 2307bis). sssd's default schema (`rfc2307`) expects
   usernames in `memberUid`; given DNs it can't canonicalize them to users and caches
   them as **ghost members with the raw DN as the name**. Symptom: `getent group`
   → `error writing group entry: Invalid argument`, `id jcallen` missing the group,
   sssd log `member #0 (cn=…): not found in sysdb / Adding a ghost entry`. Once a ghost
   is cached it survives `sss_cache -E`-looking behavior — do a hard
   `systemctl stop sssd && sss_cache -E && systemctl start sssd` to get clean entries.
3. **`enumerate = true`** keeps the user cached so group-member resolution never races
   the first user lookup on a cold cache (a fresh node querying a group before the user
   will otherwise mint a ghost).
4. **SELinux port label.** 6636 is not labeled `ldap_port_t` by default; `sssd_be`
   dies on `connect() = -1 EACCES` with nothing useful in the logs. `semanage port -a`
   is the fix. (strace from a podman container with
   `--pid=host --network=host` against the live sssd_be was how this was confirmed —
   the sssd logs say `connecting to LDAP server` and then… nothing.)

**Debug tooling that worked:** `sssd -D 9` (temp `debug_level = 9` in the domain
section) + `grep -aE 'search|filter|member' /var/log/sssd/sssd_ldap.*.log`; and
`python3 -c 'import grp; …'` on the node — it exercises NSS without glibc's `getent`
choking on malformed ghost entries, so you can see *what* a group entry actually
contains.

**API-token gotcha (CT 150):** the `fleet-api` token in `/root/authentik-api-token`
**expires after 24h by default** — and authentik's `expire_action` *rotates the key in
place* (row stays, key changes), so every API call afterwards 403s with
`Token invalid/expired` while the token still "exists". Fixed by
`Token.objects.get(identifier="fleet-api").update(expires=None)` + new key. If the API
starts 403ing again: check `Token` rows in `manage.py shell` (key rotation, not
missing rows). Note `manage.py shell` runs in the **default tenant's schema**
(`public` here); the token table elsewhere is empty.

**Environment notes from this session:**
- The API path for property mappings is `/api/v3/propertymappings/all/`
  (no underscores) — `/api/v3/core/property_mappings/` 404s.
- `pct exec 150` + `manage.py shell` prints a wall of JSON log lines; filter with
  `grep -aE '^MARKER'`.
- The node fleet image already ships sssd + oddjob-mkhomedir (no install step needed);
  `policycoreutils` (semanage) and `authselect` are also present on uCore.
