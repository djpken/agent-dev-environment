# zvec-grep ADE POC

This directory pins the npm dependency tree for an opt-in ADE provider POC. It
does not add zvec-grep to `ade.lock.json` or the default provider set. The
generated ADE user config is machine-specific and is written under the ignored
`.ade/` directory.

## Setup

Use Node.js 22 or newer. Install the exact npm dependency and lockfile from this
directory:

```bash
npm ci --prefix integrations/zvec-grep-poc
mkdir -p .ade/bin
go build -o .ade/bin/ade ./cmd/ade
python integrations/zvec-grep-poc/configure.py
```

The configure script writes `.ade/zvec-grep-poc-user.json` with the resolved
absolute path to the local `zg` executable. It refuses to replace an existing
file. The manifest exposes only the default search MCP toolset through
`http://127.0.0.1:7999/mcp` and asks ADE to supervise `zg server run` in the
foreground.

## Apply and start

The sample uses an isolated ADE root under ignored `.ade/`, so it does not
replace the default ADE generation. Plan and apply the generated config with
the same user-config path:

```bash
.ade/bin/ade --root .ade/zvec-grep-poc-runtime plan --user .ade/zvec-grep-poc-user.json
.ade/bin/ade --root .ade/zvec-grep-poc-runtime apply --user .ade/zvec-grep-poc-user.json --plan-id <plan_id>
.ade/bin/ade --root .ade/zvec-grep-poc-runtime provider zvec-grep --shared
```

`provider --shared` stays in the foreground. Stop it with `Ctrl-C`; ADE forwards
termination to the server process. After apply, use this command to add the HTTP
MCP entry to a workspace:

```bash
.ade/bin/ade --root .ade/zvec-grep-poc-runtime attach --host codex --workspace <path> --apply
```

## Grants and state

The manifest grants workspace read/write because the index lives in the
workspace's `.zvec-grep/` directory, `local-state` for model and server state,
`listen-loopback` for the MCP endpoint, and `external-network` for the default
local model download. The grants are ADE policy declarations; they do not
create an OS sandbox. Remote Embedding remains unconfigured.

This is an opt-in POC. It does not migrate or back up indexes during upgrades,
and ADE rollback only changes the active ADE generation. Do not reuse an
existing index until the pinned version's upgrade and recovery behavior has
been checked in a disposable workspace.

## Current setup status

The current VM has Node.js `v26.7.0` and npm `11.19.0`. `npm ci` resolved the
lockfile and added 192 packages. npm reported that it did not run install
scripts for `@zvec/zvec`, `node-llama-cpp`, `onnxruntime-node`, `protobufjs`,
and `sharp`; the `@zvec/zvec` prebuilt-binding check was then run directly and
exited successfully. The ADE config has been applied only to the isolated
`.ade/zvec-grep-poc-runtime` root. The server has not been started, and no
workspace index has been created.
