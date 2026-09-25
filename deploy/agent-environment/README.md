# ADES · Agent Development Environment Service

ADES is a per-VM management plane for agent runtime
components. It is separate from the `Web artifact publisher`: the service owns
health, component updates, manual controls, persistent update policies, and the
daily systemd timer; `ade publish-html` remains an optional output for a
sanitized, read-only status snapshot.

## Install on a Linux VM

Run as root from an ADE checkout:

```bash
sudo ./deploy/agent-environment/install.sh \
  --source-root /opt/ade \
  --public-host vm.example.com
```

The installer requires Go `1.27.1`, Node.js `22.12+`, and npm. It compiles the
Go ADE binary and runs `npm ci` to build the React dashboard before installing
or restarting the systemd service. Node.js is only needed during dashboard
builds; the installed API and runtime use the Go binary.

The installer creates a per-VM manifest at `/etc/ade/agent-environment.json`,
state under `/var/lib/ade/agent-environment`, a root-owned update helper, a
non-root dashboard service, and an HTTPS certificate for the supplied host.
Replace the generated self-signed certificate with a trusted certificate when
the VM is accessed through a browser password manager. The service listens on port `6790`
by default and the existing artifact publisher remains on HTTP port `80`.
`/usr/local/bin/ade` is a standalone Go binary that owns the ADE CLI, provider
lifecycle, host adapters, sync, scheduling, ADES HTTP API, password authorization,
and update runtime. systemd and the root-owned helpers call this binary
directly. The React dashboard remains TypeScript and is served by the Go
management API.

To deploy each local commit without VM polling from GitHub, install the local
post-commit worker described in [Local commit deployment](./LOCAL-DEPLOYMENT.md).

Bootstrap TLS certificates use RSA-2048 with SHA-256 for Chrome compatibility.
Using Ed25519 for the TLS certificate
can cause Chrome to fail with `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` before
certificate trust is checked. The installer preserves existing certificates.
Older deployments with an Ed25519 TLS certificate need a replacement certificate
and matching key, followed by a restart of `agent-environment.service`.
Keep a protected backup of the old pair until the HTTPS endpoint is verified.
A self-signed replacement still requires explicit client trust; changing the
key algorithm alone does not establish a trusted browser origin.

The installer disables the older `orca-headless-update.timer` to avoid two
independent Orca update paths. The replacement timer runs daily at `20:00 UTC`,
which is `04:00 UTC+8`, with no random delay and `Persistent=true`.
It installs the root-owned Orca and Codex update helpers as part of the same
recipe, so a VM does not depend on a helper copied from an older deployment.

## Component manifest

Every managed component is declared in the root-owned JSON manifest. Commands
are absolute argv arrays and never come from an HTTP request. A component may
declare a `systemd`, `command`, or `http` health probe, a version file or
command, an update command, a restart command, and dependencies.
Components that accept a fixed release can also declare `target_version_arg`.
The approved target version is then appended as an argv value to that allowlisted
command. The bundled Orca helper verifies the release manifest version, asset
size, and SHA-512 before stopping the service; Codex receives the exact
`--release` value through the official installer.

The example includes Orca, Codex, nginx, the artifact publisher, Codex proxy,
and Headroom proxy. Add other MCP or system services to the manifest with an
explicit allowlist and an update method.

## Standard HTTP and HTTPS entrypoints

Use [nginx.conf.example](./nginx.conf.example) to expose the management dashboard
through Nginx. Replace `__PUBLIC_HOST__` with the external VM address or configured
hostname, and ensure the certificate covers that address. If a publisher vhost
already owns port 80 for the same host, merge the root redirect and `/api/`
redirect into it instead of creating a duplicate vhost. Preserve its artifact,
readiness and any unrelated routes.

- `http://172.16.240.41:80/` redirects to `https://172.16.240.41/`.
- Nginx terminates TLS on port 443 and proxies the dashboard/API to port 6790.
- Published artifacts stay on `http://172.16.240.41:80/artifacts/`.
- The HTTPS management vhost rejects `/artifacts/`, keeping user HTML on a separate origin.

Set `public_origin` to `https://172.16.240.41` in the environment manifest.
For an HTTP backend use `tls: {}` and `allow_http: true`; the public TLS connection
terminates at Nginx. Add the direct port 6790 browser origin to `allowed_origins`
only when users will access that origin. A loopback-only backend can use
`listen.host: "127.0.0.1"`. Keep the HTTP port 80 artifact origin out of the
management allowlist. The proxy accepts 15 MiB JSON bodies, matching the upload API.

Back up the live Nginx and environment configuration, run `nginx -t`, restart
`agent-environment.service`, and reload Nginx. Existing self-signed certificates
require client trust; use a trusted certificate when available. A new browser
origin and the authentication change require a fresh login. Root-provisioned
password accounts and existing update policies are preserved. Reverting the
saved configurations and reloading/restarting the same services restores the
previous entrypoint.

The product name is ADES. The `agent-environment.service` unit, configuration
paths and API identifiers remain stable. The Go listener runs through
`ade environment serve`.

## Web publishing from the dashboard

The React management dashboard includes an authenticated web publishing panel. It shares
storage and publication rules with `ade publish-html`; Nginx continues to serve
published content on HTTP port 80. The dashboard and its session stay on port 6790.
Do not serve uploaded HTML on the management origin or add the artifact origin
to `allowed_origins`.

Enable publishing in the root-owned `/etc/ade/agent-environment.json`:

```json
{
  "artifacts": {
    "enabled": true,
    "artifact_root": "/var/lib/ade/web-artifacts",
    "base_url": "http://172.16.240.41:80"
  }
}
```

Existing manifests default to disabled. New installation templates enable the
panel. Configure Nginx using [the publisher setup](../ade-web-artifacts/README.md),
and give the management service user ownership of the artifact directory and
existing published files. The installer creates the default directory for new
installations; it preserves existing contents and ownership. The systemd unit
allows writes to `/var/lib/ade/web-artifacts`; a custom root also needs an explicit
`ReadWritePaths` override. Restart `agent-environment.service` after changing its
manifest or unit, and run `systemctl daemon-reload` after a unit change.

After authenticated login, viewers can list and open existing publications, including
those created through the CLI. Operators and admins can upload HTML files and
assets, or select a folder preserving relative asset paths. Select the HTML
entrypoint and a lowercase artifact name. Each upload accepts at most 200 files
and 10 MiB of decoded content. The UI requires confirmation before replacing an
existing name or deleting a publication. Replacement replaces the whole bundle;
files omitted from the new upload are removed. Deletion cannot be undone.
Published URLs are readable by anyone who can reach the publisher, without
dashboard login. The dashboard never renders uploaded HTML inside its own origin.

The session-authenticated API is:

- `GET /api/v1/artifacts`: publication list, entrypoint URLs, sizes and publisher readiness.
- `POST /api/v1/artifacts`: operator/admin upload, returning the publication receipt.
- `DELETE /api/v1/artifacts/<name>`: operator/admin deletion.

Upload JSON contains `name`, `entrypoint`, an optional boolean `overwrite`
(default `false`), and `files`: objects with a relative `path` and
`content_base64`. The encoded request limit is 15 MiB. Clients cannot choose a
server source path, storage root or public origin. Invalid bundles and blocked
publishers preserve the existing publication. List and delete remain available
when Nginx is unavailable.

## API and dashboard

The Go management service serves the dashboard on an HTTPS origin, either
directly at https://<vm-host>:6790/ or through the Nginx HTTPS entrypoint.
Password login is rejected over plain HTTP. When Nginx terminates TLS, it must
proxy from loopback and set X-Forwarded-Proto to https; the example configuration
already does this. Use a certificate trusted by the browser and 1Password.

When the dashboard has no ADES users, it offers one-time admin registration or
WebDAV backup import. The first registered account receives the admin role.
Registration closes after an account is registered or imported. Both paths
require the HTTPS dashboard.

The root-only CLI remains available for local recovery, password rotation, and
role changes. It prompts twice without echoing the password and writes a salted
scrypt hash to the root-owned private accounts.json file.

~~~
sudo /usr/local/bin/ade environment account set --config /etc/ade/agent-environment.json --username <username> --role admin
~~~

Run the same command to rotate a password or update an account role. Updating
an account invalidates its existing sessions and pending manual approvals.

The login and registration forms use username and password autocomplete fields
for browser password managers, including 1Password. Set the matching website
URL on the 1Password Login item to the HTTPS dashboard origin.

Admins can export the complete ADES manifest, account hashes, and effective
update policy from the dashboard's **環境設定備份** panel. ADES encrypts the
versioned bundle with scrypt and AES-256-GCM before uploading it to the supplied
HTTPS WebDAV file URL. The passphrase must contain at least 12 bytes. WebDAV
Basic Auth is optional; credentials are entered for each operation and never
saved by ADES. The dashboard asks before overwriting the destination.

WebDAV import is available only while no local accounts exist. The backup must
match the VM ID, `source_root`, and `state_root`; ADES validates it, preserves
file ownership and permissions, clears old sessions, and schedules a service
restart if the manifest changed. Backups exclude session state, run history,
WebDAV credentials, TLS certificate files, and TLS private keys. They restore
to the same VM only.

The public trending feed and three first-use endpoints do not need a bearer
session. Setup status reports whether an account exists; registration and
WebDAV import work only while the account store is empty. All management APIs
otherwise require an authenticated bearer session. Missing, invalid, expired,
and signed-out sessions receive HTTP 401 without environment details:

~~~
POST /api/v1/auth/login
GET  /api/v1/auth/setup-status
POST /api/v1/auth/register
POST /api/v1/auth/webdav-import
GET  /api/v1/auth/session
POST /api/v1/auth/logout
POST /api/v1/environment/backup
GET  /healthz
GET  /api/v1/health
GET  /api/v1/components
GET  /api/v1/runs
GET  /api/v1/schedule
GET  /api/v1/policy
~~~

Login accepts username and password in a JSON POST body. The server applies an
IP failure limit and returns the same invalid-credentials response for unknown
usernames and incorrect passwords. Sessions last twelve hours, survive service
restarts, and remain in sessionStorage for same-tab reloads. Reload validates
the token through GET /api/v1/auth/session. The server stores token hashes only;
there is no silent extension or refresh token.

The environment backup endpoint requires an admin session. The import endpoint
requires the backup passphrase and optional WebDAV credentials in its HTTPS
request body. ADES never logs those values.

Viewer sessions can read environment data. Operator and admin sessions can
update or restart components. Only admins can change the daily update policy:

~~~
POST /api/v1/update-runs
PUT  /api/v1/policy
~~~

POST /api/v1/update-runs can create a manual update or restart run. An update
request contains {"action":"update","component_ids":["codex"],"target_versions":{}}.
PUT /api/v1/policy with {"enabled":false} stops daily updates. The disabled
policy remains root-owned so an older shared policy cannot reactivate.

Operators and admins can update or restart an eligible component from its card,
or run the action across all eligible public components. The dashboard confirms
restarts because the component and its declared dependencies may briefly stop;
the runner restarts dependencies first. The server validates the complete
dependency chain against the manifest before queuing a run. While a run is
queued or active, update and restart controls stay disabled. Manual restart runs
do not change the daily update policy.

## Public snapshot

Set `snapshot.enabled` to `true` only when the VM should publish a sanitized
status snapshot through the separate artifact publisher. The update runner
calls `ade publish-html` and records `publish blocked` when the publisher is
unavailable. It never presents a guest path as a browser URL.

```json
{
  "snapshot": {
    "enabled": true,
    "name": "agent-environment-vm-01",
    "artifact_root": "/var/lib/ade/web-artifacts",
    "base_url": "http://172.16.240.41:80"
  }
}
```

Snapshots contain component labels, versions, health, and timestamps. They do
not contain credentials, wallet private data, commands, filesystem paths,
internal addresses, or raw logs.

## Policy and manual operations

The root-owned authorization directory is derived from the manifest path:
/etc/ade/agent-environment.auth for the default manifest. It must reside under
a directory writable only by the config owner. sessions.json is private to root
and stores bearer-token hashes and one-use manual approvals. accounts.json is
private to root and stores salted password hashes and credential versions.
policy.json is locally readable but writable only by root. The HTTP service
cannot create or modify these records directly.

The fixed `agent-environment-authorize` helper accepts bounded JSON on stdin;
bearer credentials never appear in command arguments. At execution, the updater
checks the approved run ID, manual trigger, action, components, target versions,
requester, live session, and current account role. It consumes each approval once.
Revocation or expiry blocks pending manual runs; an already executing run can
finish. Updates follow the root-owned manifest's dependency graph.

An admin can enable or replace a daily policy during a live session. Once enabled,
it has no expiry and continues through sign-out, session expiry, restarts, or
changes to the original author's account role. It stops only when an admin disables
or replaces it, or the current VM/component configuration no longer permits it.
The dashboard provides both enable and disable controls. A disabled policy remains
as a root-owned record so an old shared policy cannot reactivate it.

Existing individually signed policies in
`/var/lib/ade/agent-environment/policy.json` retain their original scope and expiry
until an admin replaces them. A forged session policy in that shared location
cannot authorize work. Manual operations do not depend on a daily policy.

The update trigger accepts only a run UUID and fixes the expected trigger to
`manual`; changing a writable run to `scheduled` cannot bypass approval. The
updater executes manifest-owned commands, follows dependencies, waits for health,
and records a completed, partial-failure, failed, or blocked result.

For an existing installation, rerun the installer from the updated trusted
checkout to install `agent-environment-authorize` and its sudoers rule, then
restart `agent-environment.service`. The installer preserves the manifest and
certificates. The helper path has a runtime default, so existing manifests do
not need a new field. Existing in-memory sessions are not migrated: log in once
after upgrading. Root helpers execute the configured source checkout; that
checkout supplies the dashboard assets and is trusted deployment code; root
helpers execute the installed Go binary.

## HTTP over company VPN or Tailscale

The manifest can enable an HTTP listener for the public dashboard feed:

~~~
{
  "listen": {"host": "0.0.0.0", "port": 6790},
  "public_origin": "http://172.16.240.41:6790",
  "allow_http": true,
  "tls": {},
  "allowed_origins": ["http://<tailscale-ip>:6790"]
}
~~~

Replace the Tailscale placeholder with this VM address, then restart
agent-environment.service. Password login is blocked on this HTTP origin. Use
the HTTPS Nginx entrypoint for authenticated management. Each extra browser
origin must be explicitly listed; do not use a wildcard.
