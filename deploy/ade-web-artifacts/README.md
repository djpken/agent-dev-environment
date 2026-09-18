# ADE Web artifact publisher

This configuration serves the output of `ade publish-html` from an `orca serve` VM. The existing `shared-local` provider contract remains loopback-only; this is a separate static HTTP service.

## Install

1. Choose a directory readable by the nginx worker and writable by the ADE user. The example uses `/var/lib/ade/web-artifacts`.
2. Copy `nginx.conf.example` to the nginx server configuration directory and replace `artifact.internal.example` with the fixed internal hostname.
3. Configure the publisher command with the same root and browser origin:

   ```bash
   export ADE_WEB_ARTIFACT_ROOT=/var/lib/ade/web-artifacts
   export ADE_WEB_ARTIFACT_BASE_URL=http://artifact.internal.example:80
   ```

4. Validate and enable the systemd-managed nginx service:

   ```bash
   nginx -t
   systemctl enable --now nginx
   systemctl reload nginx
   ```

The service exposes `/healthz` for the `ade publish-html` readiness check. The public content path is `/artifacts/<name>/<entrypoint>`. Directory listing is disabled and hidden paths are rejected.

## Codex `developer_instructions`

Keep the shared Workflow Pack prompt unchanged. Add a profile-specific rule to the active Codex `config.toml`:

```text
When the current ADE execution profile is `orca serve` and the agent creates HTML for the user's browser, publish it with `ade publish-html`. Support a single HTML file or a bundle and return the `access_url` from the JSON receipt. Use `http://172.16.240.41:80` as the browser origin unless an explicit hostname is configured. If publishing fails, report `publish blocked` and never present a guest path as a browser URL. Do not use this publication workflow for the `Local` profile.
```

Set `ADE_WEB_ARTIFACT_ROOT` and `ADE_WEB_ARTIFACT_BASE_URL` in the `orca serve` VM environment before the agent runs the command.

## CLI

Publish a single HTML file:

```bash
ade publish-html publish report.html --name architecture
```

Publish a bundle whose entrypoint is `index.html`:

```bash
ade publish-html publish report-bundle/ --name architecture --entrypoint index.html
```

Delete the fixed-name artifact manually:

```bash
ade publish-html delete architecture
```

The command returns JSON with `access_url` and `entrypoint`. If the HTTP publisher is unavailable, it returns `publish blocked` and does not write the artifact.

## ADES integration

The [ADES dashboard](../agent-environment/README.md#web-publishing-from-the-dashboard)
can list, upload and delete publications in this same directory using wallet
session authorization. Nginx remains the static content server on port 80; keep
the dashboard on its separate origin. Existing CLI publications appear in the
dashboard automatically, including bundles with nested HTML entrypoints.
