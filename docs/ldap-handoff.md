# LDAP / authentik hand-off

Written 2026-11-23 for continuation by another model. Self-contained: everything you
need to reproduce the failure and the state it's in. No secrets in this file — only
paths (all `0600`, on the host they live on).

**Goal:** a Minecraft-fleet admin (`jcallen`) logs into Cockpit on a game node with their
authentik account.

**Where it stands:** authentication works, attribute lookup does not. `ldapsearch`
returns `Operations error (1)`; the outpost log shows `failed to get user info`.
Nothing downstream (sssd, cockpit) has been configured yet, on purpose.

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

## 7. Best next step

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

## 8. What comes after LDAP works (not started, in order)

1. **sssd on the node.** `lobby` (10.10.10.54) already runs the fleet image and has
   `sssd`, `sssd-ldap`, `sssd-tools`, `oddjob-mkhomedir` installed (baked by BlueBuild,
   see `recipes/fleet.yml`). **There is no `/etc/sssd/sssd.conf` yet — deliberately.**
   It needs roughly: `id_provider=ldap`, `ldap_uri=ldaps://10.10.10.53:6636`,
   `ldap_tls_reqcert=never`, `ldap_search_base=dc=ldap,dc=goauthentik,dc=io`,
   `ldap_user_search_base=ou=users,dc=ldap,dc=goauthentik,dc=io`,
   `ldap_default_bind_dn=cn=<svc>,ou=users,…`, `ldap_default_authtok=<pw>`,
   `default_shell=/bin/bash` (so cockpit can open a session), and start `sssd`.
2. `authselect select sssd` on the node (current profile is `local`) to wire
   `/etc/nsswitch.conf` and `/etc/pam.d`.
3. `getent passwd jcallen` must resolve **before** touching cockpit.
4. Then Cockpit login. Relevant: cockpit here is the uCore **container**. Its privileged
   mode logs in by SSHing to the host and using host users/passwords — and uCore already
   ships `/etc/ssh/sshd_config.d/90-enable-localhost-passwords.conf`
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
- `/` on the authentik web UI redirects to `/setup` (the first-run wizard) even though
  `akadmin` has a working password and the API accepts the token. Log in directly at
  `https://10.10.10.53:9443/if/flow/default-authentication-flow/`. **Unresolved why** —
  possibly relevant to the brand's `flow_authentication`; worth a look if you run out of
  ideas on the main blocker.
