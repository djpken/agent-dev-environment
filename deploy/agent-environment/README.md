# Agent environment service

`Agent environment service` is a per-VM management plane for agent runtime
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

The installer creates a per-VM manifest at `/etc/ade/agent-environment.json`,
state under `/var/lib/ade/agent-environment`, a root-owned update helper, a
non-root dashboard service, and an HTTPS certificate for the supplied host.
Replace the generated self-signed certificate with a trusted certificate when
the VM is accessed through a browser wallet. The service listens on port `6790`
by default and the existing artifact publisher remains on HTTP port `80`.

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

## API and dashboard

The live dashboard is served by the management service, not by `artifacts`:

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
