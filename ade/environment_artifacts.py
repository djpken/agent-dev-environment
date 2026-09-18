"""Authenticated dashboard uploads over the shared web artifact publisher."""

import base64
import binascii
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import core, publish


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_FILES = 200
MAX_REQUEST_BYTES = 15 * 1024 * 1024


def _origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def configuration(value, management_origins):
    core.check(isinstance(value, dict), "artifacts must be an object")
    core.check(isinstance(value.get("enabled", False), bool), "artifacts.enabled must be a boolean")
    if not value.get("enabled", False):
        return {"enabled": False}
    root = value.get("artifact_root", "/var/lib/ade/web-artifacts")
    base_url = value.get("base_url", "http://172.16.240.41:80")
    core.check(isinstance(root, str) and Path(root).is_absolute(), "artifacts.artifact_root must be absolute")
    publish.validate_base_url(base_url)
    core.check("*" not in management_origins and all(_origin(base_url) != _origin(url) for url in management_origins),
               "artifacts must use a separate origin from the management service")
    return {"enabled": True, "artifact_root": root, "base_url": base_url}


def listing(config):
    if not config.get("enabled"):
        return {"enabled": False, "artifacts": []}
    ready = True
    try:
        publish.publisher_ready(config["base_url"])
    except core.Error:
        ready = False
    return {"enabled": True, "publisher_ready": ready,
            "max_upload_bytes": MAX_UPLOAD_BYTES, "max_upload_files": MAX_UPLOAD_FILES,
            "artifacts": publish.inventory(config["artifact_root"], config["base_url"])}


def upload(config, payload):
    core.check(config.get("enabled"), "web publishing is disabled")
    name = payload.get("name")
    publish._validate_name(name)
    entrypoint = publish._validate_entrypoint(payload.get("entrypoint", "index.html"))
    overwrite = payload.get("overwrite", False)
    core.check(isinstance(overwrite, bool), "overwrite must be a boolean")
    files = payload.get("files")
    core.check(isinstance(files, list) and 0 < len(files) <= MAX_UPLOAD_FILES,
               f"upload must contain 1 to {MAX_UPLOAD_FILES} files")
    publish.publisher_ready(config["base_url"])
    with tempfile.TemporaryDirectory(prefix="ade-upload-") as temporary:
        root = Path(temporary)
        seen, total = set(), 0
        for item in files:
            core.check(isinstance(item, dict), "invalid upload file")
            path = item.get("path")
            core.check(isinstance(path, str) and 0 < len(path) <= 1024 and "\\" not in path and "\x00" not in path,
                       "invalid upload path")
            parts = path.split("/")
            core.check(all(part and not part.startswith(".") for part in parts), "invalid upload path")
            core.check(path not in seen, "duplicate upload path")
            seen.add(path)
            encoded = item.get("content_base64")
            core.check(isinstance(encoded, str) and len(encoded) <= 4 * ((MAX_UPLOAD_BYTES + 2) // 3),
                       "upload exceeds 10 MiB")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise core.Error("invalid base64 file content") from exc
            total += len(data)
            core.check(total <= MAX_UPLOAD_BYTES, "upload exceeds 10 MiB")
            target = root.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as exc:
                raise core.Error("conflicting upload paths") from exc
        return publish.publish(root, name, config["artifact_root"], config["base_url"], entrypoint, overwrite=overwrite)


def remove(config, name):
    core.check(config.get("enabled"), "web publishing is disabled")
    return publish.delete(name, config["artifact_root"])
