"""Publish browser-readable HTML artifacts from an ADE runtime."""

import fcntl
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

from . import core


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
HTML_SUFFIXES = (".html", ".htm")


def _validate_name(name):
    core.check(isinstance(name, str) and NAME_PATTERN.fullmatch(name),
               "invalid artifact name")


def _validate_entrypoint(entrypoint):
    core.check(isinstance(entrypoint, str) and entrypoint and
               not entrypoint.startswith("/") and "\\" not in entrypoint,
               "invalid HTML entrypoint")
    parts = entrypoint.split("/")
    core.check(all(part not in ("", ".", "..") for part in parts),
               "invalid HTML entrypoint")
    core.check(PurePosixPath(entrypoint).suffix.lower() in HTML_SUFFIXES,
               "HTML entrypoint must end with .html or .htm")
    return "/".join(parts)


def validate_base_url(base_url):
    core.check(isinstance(base_url, str), "publish URL must be an HTTP port 80 origin")
    try:
        parsed = urlparse(base_url)
        port = parsed.port
    except ValueError as exc:
        raise core.Error("publish URL has an invalid port") from exc
    core.check(parsed.scheme == "http" and parsed.hostname and
               not parsed.username and not parsed.password and
               not parsed.query and not parsed.fragment and
               parsed.path in ("", "/") and port in (None, 80),
               "publish URL must be an HTTP port 80 origin")
    return parsed


def _access_url(base_url, name, entrypoint):
    parsed = validate_base_url(base_url)
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    return "http://" + host + ":80/artifacts/" + quote(name) + "/" + quote(entrypoint, safe="/")


def _path_exists(path):
    return path.exists() or path.is_symlink()


def _remove_path(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _copy_tree(source, destination):
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        core.check(not path.is_symlink(), "HTML bundle may not contain symbolic links")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copyfile(path, target)
            target.chmod(0o644)
        else:
            raise core.Error("HTML bundle may contain regular files only")


def _copy_source(source, stage, entrypoint):
    source = Path(source)
    core.check(not source.is_symlink(), "HTML source may not be a symbolic link")
    core.check(source.is_file() or source.is_dir(), "HTML source does not exist")
    if source.is_file():
        core.check(source.suffix.lower() in HTML_SUFFIXES, "source must be an HTML file")
        target = stage / entrypoint
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(source, target)
        target.chmod(0o644)
        return
    _copy_tree(source, stage)


def _make_public_tree(stage):
    for path in (stage, *sorted(stage.rglob("*"))):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def _replace(destination, stage):
    backup = None
    if _path_exists(destination):
        backup = destination.parent / ("." + destination.name + ".old-" + uuid.uuid4().hex)
        os.replace(destination, backup)
    try:
        os.replace(stage, destination)
    except Exception:
        if backup is not None and not _path_exists(destination):
            os.replace(backup, destination)
        raise
    if backup is not None:
        _remove_path(backup)


def _lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    root.chmod(0o755)
    return (root / ".ade-publish.lock").open("a")


def _public_root(root):
    public_root = root / "artifacts"
    core.check(not public_root.is_symlink(), "artifact root may not contain symbolic links")
    if public_root.exists():
        core.check(public_root.is_dir(), "artifact root artifacts path must be a directory")
    else:
        public_root.mkdir(mode=0o755)
    public_root.chmod(0o755)
    return public_root


def publish(source, name, artifact_root, base_url, entrypoint=None):
    """Publish one HTML file or bundle and return its browser access receipt."""
    _validate_name(name)
    source_path = Path(source)
    core.check(not source_path.is_symlink(), "HTML source may not be a symbolic link")
    source = source_path.resolve()
    root = Path(artifact_root).resolve()
    public_root = root / "artifacts"
    destination = public_root / name
    core.check(source != root and not source.is_relative_to(root) and
               not root.is_relative_to(source),
               "HTML source and artifact root may not overlap")
    entrypoint = _validate_entrypoint(entrypoint or "index.html")
    access_url = _access_url(base_url, name, entrypoint)

    with _lock(root) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        public_root = _public_root(root)
        stage = Path(tempfile.mkdtemp(prefix="." + name + ".stage-", dir=public_root))
        stage.chmod(0o755)
        try:
            _copy_source(source, stage, entrypoint)
            _make_public_tree(stage)
            core.check((stage / entrypoint).is_file(), "HTML entrypoint does not exist")
            _replace(destination, stage)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"name": name, "entrypoint": entrypoint,
            "access_url": access_url}


def delete(name, artifact_root):
    """Delete a named web artifact and return a deletion receipt."""
    _validate_name(name)
    root = Path(artifact_root).resolve()
    with _lock(root) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        destination = _public_root(root) / name
        core.check(_path_exists(destination), "artifact not found: " + name)
        _remove_path(destination)
    return {"name": name, "deleted": True}
