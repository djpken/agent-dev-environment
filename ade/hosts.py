"""Non-destructive workspace attachment for supported agent hosts."""

import json
import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path

import tomlkit

from . import core

HOSTS = {
    "claude": (".mcp.json", "claude.mcp.json", "mcpServers", ".claude/skills"),
    "codex": (".codex/config.toml", "codex.toml", "mcp_servers", ".agents/skills"),
    "opencode": ("opencode.json", "opencode.json", "mcp", ".agents/skills"),
}


def attach(root, workspace, host, apply=False):
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    with core.transaction(root):
        release = core.generation(root)
        core.check(release is not None, "run apply first")
        name, export, key, skills_dir = HOSTS[host]
        if host == "opencode":
            core.check(not (workspace / "opencode.jsonc").exists(),
                       "opencode.jsonc exists; JSONC merging is not supported")
        target = workspace / name
        # Do not follow config symlinks into another workspace or user home.
        core.check(target.resolve().is_relative_to(workspace), "host config escapes workspace")
        original = target.read_text() if target.exists() else None
        parse = tomlkit.parse if host == "codex" else json.loads
        existing = parse(original) if original is not None else {}
        incoming = parse((release / "exports" / export).read_text())
        entries = existing.setdefault(key, {})
        core.check(isinstance(entries, MutableMapping), "host MCP settings must be a table/object")
        for provider, value in incoming.get(key, {}).items():
            core.check(provider not in entries or entries[provider] == value,
                       "existing host provider conflicts: " + provider)
            entries[provider] = value
        text = tomlkit.dumps(existing) if host == "codex" else json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
        pack = root / "current" / "workflow"
        registry = core.read(pack / ".claude-plugin/plugin.json")["skills"]
        candidates = registry + ["skills/" + name for name in ("base", "implement", "code-review", "wait-what")]
        links = []
        seen = set()
        for relative in candidates:
            source = pack / relative
            core.check(source.resolve().is_relative_to(pack.resolve()) and (source / "SKILL.md").is_file(),
                       "invalid Workflow Pack registry path")
            label = source.name
            core.check(label not in seen, "duplicate skill name: " + label)
            seen.add(label)
            dest = workspace / skills_dir / label
            core.check(dest.parent.resolve().is_relative_to(workspace), "skills directory escapes workspace")
            if dest.is_symlink():
                core.check(os.readlink(dest) == str(source), "existing skill link conflicts: " + str(dest))
                continue
            core.check(not dest.exists(), "existing skill conflicts: " + str(dest))
            links.append((dest, source))
        result = {"host": host, "workspace": str(workspace), "config": str(target),
                  "skills_to_link": [str(dest) for dest, _ in links], "applied": apply}
        if not apply:
            return result
        backup = root / "attachments" / core.digest({"workspace": str(workspace), "host": host})
        # Original contents are local recovery data and never part of a lockfile.
        if not backup.exists():
            core.write(backup, {"path": str(target), "original": original})
            backup.chmod(0o600)
        made = []
        try:
            for dest, source in links:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(source)
                made.append(dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as tmp:
                tmp.write(text)
                staged = Path(tmp.name)
            try:
                # Detect an edit made outside ADE after our preview read.
                core.check((target.read_text() if target.exists() else None) == original,
                           "host config changed during attachment")
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
        except BaseException:
            for dest in reversed(made):
                dest.unlink()
            raise
        return result
