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

The installer requires Node.js `22.12+` and npm. It runs `npm ci` and builds
the React dashboard and NestJS service from `web/package-lock.json` before
installing or restarting the systemd service. The production process runs as
`orca` with Node's `--jitless` option to keep the unit's
`MemoryDenyWriteExecute=true` restriction. Install a supported Node.js LTS
release in a system path before running the installer.

The installer creates a per-VM manifest at `/etc/ade/agent-environment.json`,
state under `/var/lib/ade/agent-environment`, a root-owned update helper, a
non-root dashboard service, and an HTTPS certificate for the supplied host.
Replace the generated self-signed certificate with a trusted certificate when
the VM is accessed through a browser wallet. The service listens on port `6790`
by default and the existing artifact publisher remains on HTTP port `80`.
NestJS uses its Express adapter and serves the dashboard and API on port `6790`.
Controllers and providers organize the routes and management operations;
Express handles HTTP transport, middleware and static file delivery. The service handles
component health probes, update run state, trending retrieval and dashboard
artifact inventory, upload and deletion directly in TypeScript. The optional
`ade publish-html` command remains a Python publisher. Root-owned enrollment,
authorization, policy verification and update helpers keep their privilege
boundary. The Python `ade.cli environment` commands and Python HTTP JSON API
remain available as compatibility paths; the production NestJS service does
not start Python CLI child processes for those dashboard management
operations. The ADE Python CLI and runtime continue to own installation,
providers, host adapters, sync and scheduling.

Bootstrap TLS certificates use RSA-2048 with SHA-256 for Chrome compatibility.
Using Ed25519 for the TLS certificate
can cause Chrome to fail with `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` before
certificate trust is checked. The installer preserves existing certificates.
Older deployments with an Ed25519 TLS certificate need a replacement certificate
and matching key, followed by a restart of `agent-environment.service`.
Keep a protected backup of the old pair until the HTTPS endpoint is verified.
A self-signed replacement still requires explicit client trust; changing the
key algorithm alone does not establish a trusted browser wallet origin.

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
terminates at Nginx. Add the old port 6790 origin to `allowed_origins` only if that
entrypoint is intentionally retained. A loopback-only backend can instead use
`listen.host: "127.0.0.1"`. Do not add the HTTP port 80 artifact origin to the
management allowlist. The proxy accepts 15 MiB JSON bodies, matching the upload API.

Back up the live Nginx and environment configuration, run `nginx -t`, restart
`agent-environment.service`, and reload Nginx. Existing self-signed certificates
require client trust; use a trusted certificate when available. A new browser
origin requires signing in again, while the enrolled wallet and update policies
are preserved. Reverting the saved configurations and reloading/restarting the
same services restores the previous entrypoint.

The product name is ADES. Existing `agent-environment.service`, configuration
paths, CLI subcommands and API identifiers remain unchanged for compatibility.

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

After wallet login, viewers can list and open existing publications, including
those created through the CLI. Operators and admins can upload HTML files and
assets, or select a folder preserving relative asset paths. Select the HTML
entrypoint and a lowercase artifact name. Each upload accepts at most 200 files
and 10 MiB of decoded content. The UI requires confirmation before replacing an
existing name or deleting a publication. Replacement replaces the whole bundle;
files omitted from the new upload are removed. Deletion cannot be undone.
Published URLs are readable by anyone who can reach the publisher, without wallet
login. The dashboard never renders uploaded HTML inside its own origin.

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

The live React dashboard is served by the NestJS management service, not by `artifacts`:

```text
https://<vm-host>:6790/
```

All environment read endpoints require an authenticated wallet session via
`Authorization: Bearer <session>`. Missing, invalid, expired, and signed-out
sessions receive HTTP 401 without environment details:

```text
GET  /api/v1/auth/session
GET  /healthz
GET  /api/v1/health
GET  /api/v1/components
GET  /api/v1/runs
GET  /api/v1/schedule
GET  /api/v1/policy
GET  /api/v1/registration/status
```

Before login, the dashboard serves an empty sign-in shell without VM identity,
health, component data, policy, or run history. It makes no environment API
requests until login succeeds. Disconnect, account change, sign-out, and session
expiry clear the displayed data. `POST /api/v1/auth/logout` revokes the session.
A session lasts twelve hours, survives service restarts, and is kept in
`sessionStorage` for same-tab reloads. Reload validates it through
`GET /api/v1/auth/session`; it never requests another wallet signature.
The server stores token hashes only. A fresh login is required after expiry;
there is no silent extension or refresh token.

During first bootstrap, clicking connect starts `Wallet registration`. The first
wallet signs a one-time registration message and becomes the VM's `admin`.
The root-only enrollment helper verifies the signature and writes the wallet
allowlist; the unprivileged dashboard process never writes the root-owned
manifest directly. The systemd mount namespace makes `/etc/ade` writable for
the root helper; root directory and file ownership still prevent the service
user from changing the manifest.
The enrollment helper sets its own cache under the writable state directory,
because sudo resets the service's environment. It uses the virtual environment
already prepared by the service without attempting to sync the read-only checkout.

The registration bootstrap endpoints are available only while the VM has no
authorized wallet:

```text
GET  /api/v1/registration/challenge?address=<0x-ethereum-address>
POST /api/v1/registration
```

The sign-in challenge and verification endpoints remain available before login:
`GET /api/v1/auth/challenge?address=<0x-ethereum-address>` and
`POST /api/v1/auth/verify`. Bootstrap messages bind an opaque digest of the VM
identity without disclosing its internal identifier or environment inventory.

State-changing endpoints use the bearer session without another wallet
signature. Viewer sessions can read; operator and admin sessions can update or
restart components; only admin sessions can change the daily policy:

```text
POST /api/v1/update-runs
PUT  /api/v1/policy
```

For example, POST `{"action":"update","component_ids":["codex"],"target_versions":{}}`
to create a manual run. `"action":"restart"` uses the manifest restart command.
PUT `{"enabled":true,"components":["codex"],"allow_restart":true,"release_channel":"stable"}`
to enable daily updates, or `{"enabled":false}` to stop them. New policies do not
accept an expiry. The former control and policy challenge routes are removed.

Operators and admins can update or restart an eligible component from its card,
or run the action across all eligible public components. The dashboard confirms
restarts because the component and its declared dependencies may briefly stop;
the runner restarts dependencies first. The server validates the complete
dependency chain against the manifest before queuing a run. While a run is
queued or active, update and restart controls stay disabled. The run history
refreshes automatically and shows the final status and public component results.
Manual restart runs do not change the daily update policy.

The browser UI discovers MetaMask through EIP-6963 or its injected Ethereum
provider. The root authorization helper verifies EIP-191 login signatures and never stores wallet private keys.
Login uses an exact VM-bound application challenge, not SIWE. Ordinary Ethereum
accounts are supported; ERC-1271 contract wallets are not. No RPC, ETH balance,
or on-chain transaction is required. Wallet
enrollment is one-time from the dashboard for the first wallet, then remains
explicit in the root-owned manifest for additional wallet changes.

Bootstrap the first wallet from the VM console with its public Ethereum address:

```bash
sudo uv run --frozen --project /opt/ade python -m ade.cli environment enroll \
  --config /etc/ade/agent-environment.json \
  --address <0x-ethereum-address> \
  --role admin
sudo systemctl restart agent-environment.service
```

The enrollment command changes only the wallet allowlist. A persistent update
policy can then be created from the management API or a compatible client.

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
`/etc/ade/agent-environment.auth` for the default manifest. It must reside under
a directory writable only by the config owner. `sessions.json` is private to
root and stores one-time login challenges, bearer-token hashes, and one-use
manual approvals. `policy.json` is locally readable but writable only by root.
The HTTP service cannot create or modify these records directly.

The fixed `agent-environment-authorize` helper accepts bounded JSON on stdin;
bearer credentials never appear in command arguments. At execution, the updater
checks the approved run ID, manual trigger, action, components, target versions,
requester, live session, and current wallet role. It consumes each approval once.
Revocation or expiry blocks pending manual runs; an already executing run can
finish. Updates follow the root-owned manifest's dependency graph.

An admin can enable or replace a daily policy during a live session. Once enabled,
it has no expiry and continues through sign-out, session expiry, restarts, or
changes to the original author's wallet role. It stops only when an admin disables
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
checkout and its Python environment are trusted deployment code.

## HTTP over company VPN or Tailscale

The operator can explicitly select HTTP in the root-owned manifest:

```json
{
  "listen": {"host": "0.0.0.0", "port": 6790},
  "public_origin": "http://172.16.240.41:6790",
  "allow_http": true,
  "tls": {},
  "allowed_origins": ["http://<tailscale-ip>:6790"]
}
```

Replace the Tailscale placeholder with this VM's address, then restart only
`agent-environment.service`. This mode uses the same wallet login and session authorization and relies
on the network for page integrity and session confidentiality. Each extra
browser origin must be explicitly listed; do not use a wildcard. Changing
wallet accounts clears the dashboard session and requires another login.
Old Solana allowlists and signed policies cannot be used as Ethereum identities;
re-enroll intentionally when migrating an already-registered VM.
