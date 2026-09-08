"""Validated composition and immutable installation generations."""

import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator


class Error(ValueError):
    pass


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def check(condition, message):
    if not condition:
        raise Error(message)


def manifest(value):
    validator = Draft202012Validator(read(Path(__file__).with_name("provider.schema.json")))
    errors = sorted(validator.iter_errors(value), key=lambda e: str(e.path))
    check(not errors, "invalid provider manifest: " + (errors[0].message if errors else ""))
    if "url" in value:
        url = urlparse(value["url"])
        check(url.hostname == "127.0.0.1" and url.port and not url.username,
              "shared services must use a loopback endpoint")
    return value


def compose(lock, user, workspace):
    check(lock.get("schema_version") == 1, "unsupported lockfile version")
    check(set(workspace) <= {"providers", "prompts"},
          "workspace may only select providers and prompts; grants/endpoints belong to user config")
    check(set(user) <= {"providers", "prompts", "grants", "llm_endpoint", "extensions"},
          "unknown user config field")
    providers = {}
    locked_providers = len(lock["providers"])
    for position, value in enumerate(lock["providers"] + user.get("extensions", [])):
        value = manifest(copy.deepcopy(value))
        check(not value.get("builtin") or position < locked_providers,
              "user extensions may not declare builtin providers")
        check(value["id"] not in providers, "duplicate provider namespace: " + value["id"])
        providers[value["id"]] = value
    result = copy.deepcopy(lock["defaults"])
    for layer in (user, workspace):
        selected = layer.get("providers", {})
        check(isinstance(selected, dict), "providers must be an object")
        for name, enabled in selected.items():
            check(name in providers and type(enabled) is bool, "unknown provider or invalid enabled flag")
            result["providers"][name] = enabled
        if "prompts" in layer:
            result["prompts"] = layer["prompts"]
    check(isinstance(result["prompts"], list), "prompts must be an array")
    for name in result["prompts"]:
        check(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+\.md", name),
              "prompt must be a filename within the Workflow Pack")
    grants = user.get("grants", {})
    check(isinstance(grants, dict), "grants must be an object")
    for name, permissions in grants.items():
        check(name in providers and isinstance(permissions, list) and
              all(isinstance(p, str) for p in permissions), "invalid provider grant")
    endpoint = user.get("llm_endpoint")
    if endpoint is not None:
        check(isinstance(endpoint, str), "llm_endpoint must be a URL or null")
        url = urlparse(endpoint)
        check(url.scheme in ("https", "http") and url.hostname and not url.username and
              not url.password and not url.query and not url.fragment, "invalid LLM endpoint")
        check(url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"),
              "non-local LLM endpoints require HTTPS")
    result.update(manifests=providers, grants=grants, llm_endpoint=endpoint)
    return result


def authorize(config, name):
    check(config["providers"].get(name) is True, "provider disabled: " + name)
    provider = config["manifests"][name]
    missing = set(provider["permissions"]) - set(config["grants"].get(name, []))
    check(not missing, "missing user grants for " + name + ": " + ", ".join(sorted(missing)))
    if "llm-network" in provider["permissions"]:
        check(config["llm_endpoint"], "set an explicit llm_endpoint in user config before running " + name)
    return provider


def generation(root):
    pointer = Path(root) / "current"
    if not pointer.is_symlink():
        check(not pointer.exists(), "current must be an ADE-managed symlink")
        return None
    target = pointer.resolve()
    check(target.parent == (Path(root) / "releases").resolve() and target.is_dir(),
          "invalid generation pointer")
    return target


@contextlib.contextmanager
def transaction(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "update.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def switch(root, target):
    root = Path(root)
    old = generation(root)
    temp = root / (".current-" + uuid.uuid4().hex)
    temp.symlink_to(target.relative_to(root))
    os.replace(temp, root / "current")
    return old


def bundle(repository):
    """Capture only the Workflow Pack; include contents in the installation plan."""
    repository = Path(repository).resolve()
    paths = [repository / ".claude-plugin/plugin.json", repository / "LICENSE"]
    for folder in ("skills", "prompt"):
        base = repository / folder
        check(base.is_dir() and not base.is_symlink(), "missing or linked Workflow Pack directory: " + folder)
        for path in sorted(base.rglob("*")):
            check(not (path.is_symlink() and path.is_dir()), "linked Workflow Pack directories are not supported")
            if path.is_file() or path.is_symlink():
                check(path.resolve().is_relative_to(base.resolve()), "Workflow Pack link escapes its directory")
                paths.append(path)
    captured = {}
    for path in sorted(paths):
        check(path.is_file() and path.resolve().is_relative_to(repository), "invalid Workflow Pack file: " + str(path))
        captured[str(path.relative_to(repository))] = (path.read_bytes(), bool(path.stat().st_mode & 0o111))
    return captured


def bundle_id(captured):
    return digest({name: [hashlib.sha256(data).hexdigest(), executable]
                   for name, (data, executable) in captured.items()})


def plan(lock, config, root, repository=None, captured=None):
    active = generation(root)
    if lock["workflow"].get("source") == "bundled":
        check(repository is not None or captured is not None, "bundled workflow requires a repository")
        revision = bundle_id(captured if captured is not None else bundle(repository))
    else:
        revision = lock["workflow"]["revision"]
    selected = [name for name, enabled in config["providers"].items() if enabled]
    blocked = {}
    for name in selected:
        try:
            authorize(config, name)
        except Error as exc:
            blocked[name] = str(exc)
    return {"plan_id": digest({"lock": lock, "config": config, "current": str(active), "workflow": revision}),
            "current": str(active) if active else None,
            "workflow_revision": revision,
            "providers": selected, "blocked_until_configured": blocked,
            "actions": ["stage Workflow Pack", "verify pinned artifacts", "write host exports", "switch current atomically"]}


def snapshot(repository, revision, destination):
    check(re.fullmatch(r"[0-9a-f]{40}", revision), "workflow revision must be a full commit SHA")
    archive = subprocess.run(["git", "-C", str(repository), "archive", revision],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        check(all(m.isfile() or m.isdir() or m.issym() for m in members), "workflow archive may not contain devices")
        tar.extractall(destination, members=members, filter="data")


def artifact(lock, name):
    item = lock["artifacts"][name]
    key = platform.system().lower() + "-" + platform.machine().lower()
    check(key in item["platforms"], "unsupported artifact platform: " + key)
    pinned = item["platforms"][key]
    check(re.fullmatch(r"[0-9a-f]{64}", pinned["sha256"]), "invalid SHA-256")
    url = item["base_url"] + pinned["file"]
    check(urlparse(url).scheme == "https", "artifact downloads require HTTPS")
    return url, pinned["sha256"]


def download(url, sha, path):
    hasher = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, Path(path).open("wb") as output:
        check(urlparse(response.url).scheme == "https", "insecure artifact redirect")
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            hasher.update(chunk)
    check(hasher.hexdigest() == sha, "artifact checksum mismatch")
    Path(path).chmod(0o755)


def argv(provider, release, health=False):
    values = provider["health" if health else "command"]
    return [arg.replace("{release}", str(release)) for arg in values]


def clean_env(provider, environment=None):
    allowed = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    allowed.update(provider.get("env_vars", []))
    # Keep HOME for provider-owned local state; never persist environment secrets.
    allowed.add("HOME")
    source = os.environ if environment is None else environment
    return {key: value for key, value in source.items() if key in allowed}


def health(provider, release):
    if provider.get("builtin"):
        return "ok"
    output = subprocess.run(argv(provider, release, health=True), capture_output=True,
                            timeout=20, env=clean_env(provider), text=True, check=False)
    check(output.returncode == 0, "health command failed: " + provider["id"])
    text = output.stdout + output.stderr
    check(re.search(r"(?<![0-9.])" + re.escape(provider["version"]) + r"(?![0-9.])", text),
          "installed version does not match manifest: " + provider["id"])
    return "ok"


def exports(config, release):
    claude, opencode, codex = {}, {}, []
    skipped = {}
    for name, enabled in config["providers"].items():
        if not enabled:
            continue
        try:
            provider = authorize(config, name)
        except Error as exc:
            skipped[name] = str(exc)
            continue
        if provider["transport"] == "cli":
            continue
        key = "ade-" + name
        if provider["transport"] == "http":
            url = provider["url"]
            claude[key] = {"type": "http", "url": url}
            opencode[key] = {"type": "remote", "url": url, "enabled": True}
            codex += [f'[mcp_servers.{key}]', 'url = ' + json.dumps(url), ""]
        else:
            # Route host-spawned MCP through ADE for grant checks and environment filtering.
            command = [sys.executable, "-m", "ade.cli", "--root", str(release.parent.parent), "provider", name]
            claude[key] = {"type": "stdio", "command": command[0], "args": command[1:]}
            opencode[key] = {"type": "local", "command": command, "enabled": True}
            codex += [f'[mcp_servers.{key}]', 'command = ' + json.dumps(command[0]),
                      'args = ' + json.dumps(command[1:]), ""]
    return {"claude.mcp.json": {"mcpServers": claude},
            "opencode.json": {"mcp": opencode}, "codex.toml": "\n".join(codex),
            "blocked.json": skipped}


def install(lock, config, root, repository, expected_plan=None):
    root = Path(root).resolve()
    with transaction(root):
        captured = bundle(repository) if lock["workflow"].get("source") == "bundled" else None
        proposed = plan(lock, config, root, repository, captured)
        if expected_plan:
            check(proposed["plan_id"] == expected_plan, "stale plan; run plan again")
        releases = root / "releases"
        releases.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=releases))
        final = releases / uuid.uuid4().hex
        try:
            if captured is not None:
                for name, (data, executable) in captured.items():
                    target = stage / "workflow" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o755 if executable else 0o644)
            else:
                snapshot(repository, lock["workflow"]["revision"], stage / "workflow")
            (stage / "bin").mkdir()
            for name, enabled in config["providers"].items():
                if not enabled:
                    continue
                provider = config["manifests"][name]
                if name in lock["artifacts"]:
                    check(provider["version"] == lock["artifacts"][name]["version"], "artifact version mismatch")
                    url, sha = artifact(lock, name)
                    download(url, sha, stage / "bin" / name)
                    health(provider, stage)
                else:
                    authorize(config, name)
                    if not provider.get("builtin"):
                        health(provider, stage)
            parts = []
            for name in config["prompts"]:
                path = stage / "workflow" / "prompt" / name
                check(path.is_file(), "unknown Workflow Pack prompt: " + name)
                parts.append(path.read_text())
            (stage / "personal-prompt.md").write_text("\n\n---\n\n".join(parts))
            write(stage / "lock.json", lock)
            write(stage / "workflow-source.json", {"digest_or_revision": proposed["workflow_revision"]})
            write(stage / "config.json", config)
            for name, value in exports(config, final).items():
                path = stage / "exports" / name
                path.parent.mkdir(exist_ok=True)
                if isinstance(value, str):
                    path.write_text(value)
                else:
                    write(path, value)
            # Previous pointer is part of the generation, so commit uses one atomic switch.
            write(stage / "previous.json", {"generation": proposed["current"]})
            stage.rename(final)
            switch(root, final)
            return {"generation": str(final), "previous": proposed["current"],
                    "blocked_until_configured": proposed["blocked_until_configured"]}
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def rollback(root):
    root = Path(root).resolve()
    with transaction(root):
        current = generation(root)
        check(current is not None, "no active generation")
        previous = read(current / "previous.json")["generation"]
        check(previous, "no previous generation")
        target = Path(previous)
        check(target.parent == root / "releases" and target.is_dir(), "invalid previous generation")
        switch(root, target)
        return {"generation": str(target)}
