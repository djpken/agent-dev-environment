# Agent environment service

`Agent environment service` is a per-VM management plane for agent runtime
components. It is separate from the `Web artifact publisher`: the service owns
health, component updates, manual controls, signed update policies, and the
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
The signed target version is then appended as an argv value to that allowlisted
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

Read-only endpoints are public:

```text
GET  /healthz
GET  /api/v1/health
GET  /api/v1/components
GET  /api/v1/runs
GET  /api/v1/schedule
GET  /api/v1/policy
```

State-changing endpoints require a Solana wallet session and a fresh
`solana:signMessage` signature over a canonical request envelope:

```text
GET  /api/v1/auth/challenge?address=<base58-address>
POST /api/v1/auth/verify
POST /api/v1/control-challenges
POST /api/v1/update-runs
POST /api/v1/policy-challenges
PUT  /api/v1/policy
```

The browser UI supports the common Solana wallet object and the `signIn` path;
the API verifies Ed25519 signatures and never stores private keys. Wallet
enrollment is intentionally explicit in the root-owned manifest.

Bootstrap the first wallet from the VM console with its public Solana address:

```bash
sudo uv run --frozen --project /opt/ade python -m ade.cli environment enroll \
  --config /etc/ade/agent-environment.json \
  --address <solana-public-address> \
  --role admin
sudo systemctl restart agent-environment.service
```

The enrollment command changes only the wallet allowlist. The first signed
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

The scheduled updater applies changes only when a valid signed policy exists at
`/var/lib/ade/agent-environment/policy.json`. The policy binds a VM identity,
component allowlist, stable release channel, restart permission, and expiry to
an authorized Solana wallet. Manual operations use a short-lived signed
control request and do not require a scheduled policy.

The root helper accepts only an update run UUID from the unprivileged service.
It fixes the manifest path, validates the run state, executes manifest-owned
commands, follows dependencies, waits for health, and records a completed,
partial-failure, failed, or blocked result.
