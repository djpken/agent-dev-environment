"""Per-VM agent environment management primitives.

The environment service deliberately keeps its control plane separate from the
web artifact publisher.  Components are declared in a root-owned manifest and
are executed as argv arrays; requests never carry shell commands.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import html
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature

from . import core, environment_artifacts
from .trending import TrendingFetchError, TrendingRequestError, TrendingService


ENVIRONMENT_VERSION = "1"
DEFAULT_CONFIG_PATH = Path("/etc/ade/agent-environment.json")
STATUS_VALUES = ("healthy", "degraded", "unhealthy", "unknown", "updating")
ROLES = ("viewer", "operator", "admin")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
UNIT_PATTERN = re.compile(r"^[A-Za-z0-9_.@:%+-]+$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f-]{36}$")
TARGET_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
TARGET_VERSION_ARG_PATTERN = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
REGISTRATION_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class EnvironmentError(ValueError):
    """A configuration, authorization, or environment operation error."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EnvironmentError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EnvironmentError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"invalid JSON state: {path}") from exc


def _write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".environment-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.chmod(mode)
    os.replace(temporary, path)
    if os.geteuid() == 0:
        os.chown(path, os.getuid(), path.parent.stat().st_gid)


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def normalize_wallet_address(address: str) -> str:
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise EnvironmentError("wallet address must be a 0x-prefixed Ethereum address")
    return address.lower()


def verify_wallet_signature(address: str, message: bytes, signature: str) -> bool:
    """Verify MetaMask personal_sign (EIP-191) without an RPC or private key."""
    try:
        address = normalize_wallet_address(address)
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            return False
        recovered = Account.recover_message(encode_defunct(primitive=message), signature=signature)
        return secrets.compare_digest(recovered.lower(), address)
    except (EnvironmentError, ValueError, TypeError, BadSignature):
        return False


def _redact(value: str, limit: int = 280) -> str:
    value = re.sub(
        r"(?i)(token|secret|password|api[_-]?key|private[_-]?key)([=:])[^\s,;]+",
        r"\1\2[redacted]",
        value,
    )
    value = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer [redacted]", value)
    return " ".join(value.split())[-limit:]


def _command(value: Any, field: str, required: bool = False) -> tuple[str, ...] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise EnvironmentError(f"{field} must be a non-empty argv array")
    if not value[0].startswith("/"):
        raise EnvironmentError(f"{field}[0] must be an absolute executable path")
    return tuple(value)


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise EnvironmentError(f"{field} must be a safe identifier")
    return value


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    kind: str
    health: Mapping[str, Any]
    version_file: str | None
    version_command: tuple[str, ...] | None
    update_command: tuple[str, ...] | None
    target_version_arg: str | None
    restart_command: tuple[str, ...] | None
    rollback_command: tuple[str, ...] | None
    dependencies: tuple[str, ...]
    enabled: bool
    public: bool
    timeout_seconds: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Component":
        component_id = _id(value.get("id"), "component.id")
        label = value.get("label", component_id)
        if not isinstance(label, str) or not label.strip():
            raise EnvironmentError(f"component {component_id} label must be text")
        kind = value.get("kind", "service")
        if not isinstance(kind, str) or not kind.strip():
            raise EnvironmentError(f"component {component_id} kind must be text")
        health = value.get("health")
        if not isinstance(health, Mapping):
            raise EnvironmentError(f"component {component_id} health must be an object")
        health_type = health.get("type")
        if health_type not in {"systemd", "command", "http"}:
            raise EnvironmentError(f"component {component_id} has unsupported health type")
        if health_type == "systemd":
            unit = health.get("unit")
            if not isinstance(unit, str) or not UNIT_PATTERN.fullmatch(unit):
                raise EnvironmentError(f"component {component_id} has invalid systemd unit")
        elif health_type == "command":
            _command(health.get("command"), f"component {component_id} health.command", True)
        else:
            url = health.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise EnvironmentError(f"component {component_id} has invalid health URL")
        version_file = value.get("version_file")
        if version_file is not None:
            if not isinstance(version_file, str) or not version_file.startswith("/"):
                raise EnvironmentError(f"component {component_id} version_file must be absolute")
        target_version_arg = value.get("target_version_arg")
        if target_version_arg is not None and (
            not isinstance(target_version_arg, str)
            or not TARGET_VERSION_ARG_PATTERN.fullmatch(target_version_arg)
        ):
            raise EnvironmentError(f"component {component_id} target_version_arg is invalid")
        dependencies = value.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise EnvironmentError(f"component {component_id} dependencies must be a list")
        timeout = value.get("timeout_seconds", 1800)
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise EnvironmentError(f"component {component_id} timeout_seconds is invalid")
        return cls(
            id=component_id,
            label=label.strip(),
            kind=kind.strip(),
            health=dict(health),
            version_file=version_file,
            version_command=_command(value.get("version_command"), f"component {component_id} version_command"),
            update_command=_command(value.get("update_command"), f"component {component_id} update_command"),
            target_version_arg=target_version_arg,
            restart_command=_command(value.get("restart_command"), f"component {component_id} restart_command"),
            rollback_command=_command(value.get("rollback_command"), f"component {component_id} rollback_command"),
            dependencies=tuple(_id(item, f"component {component_id} dependency") for item in dependencies),
            enabled=value.get("enabled", True) is True,
            public=value.get("public", True) is True,
            timeout_seconds=timeout,
        )

    def version(self) -> str | None:
        try:
            if self.version_file:
                value = Path(self.version_file).read_text(encoding="utf-8").strip()
            elif self.version_command:
                result = subprocess.run(
                    list(self.version_command),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                )
                value = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else ""
                if result.returncode != 0:
                    return None
            else:
                return None
            return _redact(value, 128) or None
        except (OSError, subprocess.SubprocessError, IndexError):
            return None

    def probe(self) -> dict[str, Any]:
        checked_at = iso_now()
        probe_type = self.health["type"]
        try:
            if probe_type == "systemd":
                result = subprocess.run(
                    ["/usr/bin/systemctl", "is-active", "--quiet", self.health["unit"]],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                )
                healthy = result.returncode == 0
                detail = "active" if healthy else _redact((result.stdout or result.stderr).strip() or "inactive", 160)
            elif probe_type == "command":
                result = subprocess.run(
                    list(_command(self.health["command"], "health.command", True) or ()),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                )
                healthy = result.returncode == 0
                detail = "ok" if healthy else _redact((result.stderr or result.stdout).strip() or "command failed", 160)
            else:
                request = urllib.request.Request(str(self.health["url"]), headers=dict(self.health.get("headers", {})))
                with urllib.request.urlopen(request, timeout=5) as response:
                    healthy = 200 <= response.status < 400
                    detail = f"HTTP {response.status}"
            return {
                "status": "healthy" if healthy else "unhealthy",
                "checked_at": checked_at,
                "detail": detail,
                "version": self.version(),
            }
        except (OSError, urllib.error.URLError, subprocess.SubprocessError, ValueError) as exc:
            return {
                "status": "unknown",
                "checked_at": checked_at,
                "detail": _redact(str(exc), 160),
                "version": self.version(),
            }

    def public_status(self, probe: Mapping[str, Any]) -> dict[str, Any]:
        status = probe.get("status", "unknown") if probe.get("status") in STATUS_VALUES else "unknown"
        if status == "healthy":
            detail = "healthy"
        elif status == "unhealthy":
            detail = "unhealthy"
        else:
            detail = "probe unavailable"
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "status": status,
            "version": probe.get("version"),
            "checked_at": probe.get("checked_at"),
            "detail": detail,
            "update_supported": self.update_command is not None,
            "target_version_supported": self.update_command is not None and self.target_version_arg is not None,
            "restart_supported": self.restart_command is not None,
            "rollback_supported": self.rollback_command is not None,
            "dependencies": list(self.dependencies),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class EnvironmentConfig:
    path: Path
    vm_id: str
    source_root: Path
    state_root: Path
    listen_host: str
    listen_port: int
    public_origin: str
    tls_cert: Path | None
    tls_key: Path | None
    allowed_origins: tuple[str, ...]
    components: tuple[Component, ...]
    authorized_wallets: tuple[Mapping[str, str], ...]
    schedule: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    manual_trigger: Path
    registration_trigger: Path
    authorization_trigger: Path
    artifacts: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "EnvironmentConfig":
        config_path = Path(path).expanduser().resolve()
        raw = _read_json(config_path, None)
        if not isinstance(raw, Mapping):
            raise EnvironmentError(f"environment config does not exist: {config_path}")
        vm_id = _id(raw.get("vm_id"), "vm_id")
        source_root = Path(raw.get("source_root", ".")).expanduser().resolve()
        state_root = Path(raw.get("state_root", "/var/lib/ade/agent-environment")).expanduser().resolve()
        listen = raw.get("listen", {})
        if not isinstance(listen, Mapping):
            raise EnvironmentError("listen must be an object")
        listen_host = listen.get("host", "0.0.0.0")
        listen_port = listen.get("port", 6790)
        if not isinstance(listen_host, str) or not listen_host:
            raise EnvironmentError("listen.host must be text")
        if not isinstance(listen_port, int) or not 1 <= listen_port <= 65535:
            raise EnvironmentError("listen.port is invalid")
        public_origin = raw.get("public_origin", "")
        if not isinstance(public_origin, str) or not public_origin.startswith(("http://", "https://")):
            raise EnvironmentError("public_origin must be an HTTP(S) origin")
        tls = raw.get("tls", {})
        if not isinstance(tls, Mapping):
            raise EnvironmentError("tls must be an object")
        tls_cert = Path(tls["cert"]).expanduser().resolve() if tls.get("cert") else None
        tls_key = Path(tls["key"]).expanduser().resolve() if tls.get("key") else None
        if (tls_cert is None) != (tls_key is None):
            raise EnvironmentError("tls.cert and tls.key must be supplied together")
        allow_http = raw.get("allow_http", False)
        if not isinstance(allow_http, bool):
            raise EnvironmentError("allow_http must be a boolean")
        if listen_host not in {"127.0.0.1", "::1", "localhost"} and tls_cert is None and not allow_http:
            raise EnvironmentError("TLS is required when the environment service is not loopback-only")
        allowed_origins = raw.get("allowed_origins", [])
        if not isinstance(allowed_origins, list) or not all(isinstance(item, str) for item in allowed_origins):
            raise EnvironmentError("allowed_origins must be a list")
        try:
            artifacts = environment_artifacts.configuration(raw.get("artifacts", {}), [public_origin, *allowed_origins])
        except (core.Error, ValueError) as exc:
            raise EnvironmentError(str(exc)) from exc
        raw_components = raw.get("components", [])
        if not isinstance(raw_components, list) or not raw_components:
            raise EnvironmentError("components must be a non-empty list")
        components = tuple(Component.from_mapping(item) for item in raw_components if isinstance(item, Mapping))
        if len(components) != len(raw_components) or len({item.id for item in components}) != len(components):
            raise EnvironmentError("components must be unique objects")
        component_ids = {item.id for item in components}
        for component in components:
            missing = set(component.dependencies) - component_ids
            if missing:
                raise EnvironmentError(f"component {component.id} has unknown dependencies: {sorted(missing)}")
        wallets = raw.get("wallets", {}).get("authorized", []) if isinstance(raw.get("wallets", {}), Mapping) else []
        if not isinstance(wallets, list):
            raise EnvironmentError("wallets.authorized must be a list")
        authorized = []
        for item in wallets:
            if not isinstance(item, Mapping) or not isinstance(item.get("address"), str) or item.get("role") not in ROLES:
                raise EnvironmentError("wallet entries require address and viewer/operator/admin role")
            authorized.append({"address": normalize_wallet_address(item["address"]), "role": item["role"]})
        schedule = raw.get("schedule", {})
        if not isinstance(schedule, Mapping):
            raise EnvironmentError("schedule must be an object")
        if schedule.get("time", "04:00") != "04:00" or schedule.get("timezone", "UTC+8") != "UTC+8":
            raise EnvironmentError("the environment schedule is fixed at 04:00 UTC+8")
        snapshot = raw.get("snapshot", {})
        if not isinstance(snapshot, Mapping):
            raise EnvironmentError("snapshot must be an object")
        manual_trigger = Path(raw.get("manual_trigger", "/usr/local/sbin/agent-environment-trigger")).expanduser().resolve()
        registration_trigger = Path(
            raw.get("registration_trigger", "/usr/local/sbin/agent-environment-enroll")
        ).expanduser().resolve()
        return cls(
            path=config_path,
            vm_id=vm_id,
            source_root=source_root,
            state_root=state_root,
            listen_host=listen_host,
            listen_port=listen_port,
            public_origin=public_origin.rstrip("/"),
            tls_cert=tls_cert,
            tls_key=tls_key,
            allowed_origins=tuple(allowed_origins),
            components=components,
            authorized_wallets=tuple(authorized),
            schedule=dict(schedule),
            snapshot=dict(snapshot),
            artifacts=artifacts,
            manual_trigger=manual_trigger,
            registration_trigger=registration_trigger,
            authorization_trigger=Path(raw.get("authorization_trigger", "/usr/local/sbin/agent-environment-authorize")).expanduser().resolve(),
        )

    def component_map(self) -> dict[str, Component]:
        return {item.id: item for item in self.components}

    def public_config(self) -> dict[str, Any]:
        return {
            "version": ENVIRONMENT_VERSION,
            "vm_id": self.vm_id,
            "public_origin": self.public_origin,
            "registration_required": not bool(self.authorized_wallets),
            "schedule": {"time": "04:00", "timezone": "UTC+8", "persistent": True},
            "snapshot": {"enabled": bool(self.snapshot.get("enabled", False))},
        }


class RunStore:
    """Small file-backed store shared by the unprivileged API and root updater."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.state_path = self.root / "runs.json"
        self.history_path = self.root / "runs.jsonl"
        self.lock_path = self.root / ".runs.lock"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o2750)

    def _read(self) -> dict[str, Any]:
        value = _read_json(self.state_path, {"runs": []})
        if not isinstance(value, Mapping) or not isinstance(value.get("runs", []), list):
            raise EnvironmentError("invalid update run state")
        return {"runs": list(value.get("runs", []))}

    def create(
        self,
        trigger: str,
        component_ids: list[str],
        requested_by: str = "system",
        action: str = "update",
        target_versions: Mapping[str, str] | None = None,
        authorization: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        run = {
            "id": str(uuid.uuid4()),
            "trigger": trigger,
            "component_ids": component_ids,
            "action": action,
            "target_versions": dict(target_versions or {}),
            "authorization": dict(authorization or {}),
            "requested_by": requested_by,
            "status": "queued",
            "created_at": iso_now(),
            "started_at": None,
            "finished_at": None,
            "components": [],
            "publication": None,
            "error": None,
        }
        with _locked(self.lock_path):
            state = self._read()
            state["runs"] = [run, *state["runs"][:99]]
            _write_json(self.state_path, state, 0o640)
        return run

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        with _locked(self.lock_path):
            return next((dict(item) for item in self._read()["runs"] if item.get("id") == run_id), None)

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with _locked(self.lock_path):
            state = self._read()
            for index, item in enumerate(state["runs"]):
                if item.get("id") == run_id:
                    updated = {**item, **changes}
                    state["runs"][index] = updated
                    _write_json(self.state_path, state, 0o640)
                    if updated.get("status") in {"completed", "partial_failure", "failed", "blocked"}:
                        with self.history_path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(updated, ensure_ascii=False, sort_keys=True) + "\n")
                        self.history_path.chmod(0o640)
                        if os.geteuid() == 0:
                            os.chown(self.history_path, os.getuid(), self.history_path.parent.stat().st_gid)
                    return dict(updated)
        raise EnvironmentError(f"update run not found: {run_id}")

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with _locked(self.lock_path):
            return [dict(item) for item in self._read()["runs"][: max(1, min(limit, 100))]]

    def write_policy(self, policy: Mapping[str, Any]) -> None:
        _write_json(self.root / "policy.json", dict(policy), 0o640)

    def read_policy(self) -> dict[str, Any] | None:
        value = _read_json(self.root / "policy.json", None)
        return dict(value) if isinstance(value, Mapping) else None


def control_message(
    vm_id: str,
    action: str,
    component_ids: list[str],
    target_versions: Mapping[str, str] | None,
    nonce: str,
    issued_at: str,
    expires_at: str,
    policy_id: str | None = None,
) -> str:
    payload = {
        "action": action,
        "component_ids": sorted(component_ids),
        "expires_at": expires_at,
        "issued_at": issued_at,
        "nonce": nonce,
        "policy_id": policy_id,
        "target_versions": dict(sorted((target_versions or {}).items())),
        "vm_id": vm_id,
    }
    return "ADE-ENVIRONMENT-CONTROL-V1\n" + _json_bytes(payload).decode("utf-8")


def control_payload(message: str) -> dict[str, Any]:
    if not isinstance(message, str) or not message.startswith("ADE-ENVIRONMENT-CONTROL-V1\n"):
        raise EnvironmentError("control message has an invalid prefix")
    try:
        payload = json.loads(message.split("\n", 1)[1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise EnvironmentError("control message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EnvironmentError("control message payload must be an object")
    required = {"action", "component_ids", "expires_at", "issued_at", "nonce", "policy_id", "target_versions", "vm_id"}
    if set(payload) != required:
        raise EnvironmentError("control message fields are invalid")
    return payload


def registration_message(
    vm_id: str,
    address: str,
    nonce: str,
    issued_at: str,
    expires_at: str,
) -> str:
    payload = {
        "action": "register",
        "address": address,
        "expires_at": expires_at,
        "issued_at": issued_at,
        "nonce": nonce,
        "role": "admin",
        "vm_binding": hashlib.sha256(vm_id.encode()).hexdigest(),
    }
    return "ADE-ENVIRONMENT-REGISTRATION-V1\n" + _json_bytes(payload).decode("utf-8")


def registration_payload(message: str) -> dict[str, Any]:
    prefix = "ADE-ENVIRONMENT-REGISTRATION-V1\n"
    if not isinstance(message, str) or not message.startswith(prefix):
        raise EnvironmentError("registration message has an invalid prefix")
    try:
        payload = json.loads(message.removeprefix(prefix))
    except json.JSONDecodeError as exc:
        raise EnvironmentError("registration message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EnvironmentError("registration message payload must be an object")
    required = {"action", "address", "expires_at", "issued_at", "nonce", "role", "vm_binding"}
    if set(payload) != required:
        raise EnvironmentError("registration message fields are invalid")
    return payload


def verify_registration_request(
    config: EnvironmentConfig,
    address: str,
    message: str,
    signature: str,
) -> tuple[bool, str]:
    if config.authorized_wallets:
        return False, "registration is already complete"
    try:
        address = normalize_wallet_address(address)
        payload = registration_payload(message)
        if payload["action"] != "register" or payload["role"] != "admin":
            return False, "registration role or action is invalid"
        if payload["address"] != address or payload["vm_binding"] != hashlib.sha256(config.vm_id.encode()).hexdigest():
            return False, "registration identity does not match this VM"
        if not isinstance(payload["nonce"], str) or not REGISTRATION_NONCE_PATTERN.fullmatch(payload["nonce"]):
            return False, "registration nonce is invalid"
        issued_at = parse_timestamp(payload["issued_at"])
        expires_at = parse_timestamp(payload["expires_at"])
        now = utc_now()
        if issued_at > now + dt.timedelta(minutes=1):
            return False, "registration message is issued in the future"
        if expires_at <= now or expires_at <= issued_at:
            return False, "registration message has expired"
        if expires_at - issued_at > dt.timedelta(minutes=15):
            return False, "registration message lifetime is too long"
        if not verify_wallet_signature(address, message.encode("utf-8"), signature):
            return False, "registration signature is invalid"
    except (EnvironmentError, KeyError, TypeError, ValueError) as exc:
        return False, str(exc)
    return True, "ok"


def enroll_signed_wallet(
    config: EnvironmentConfig,
    address: str,
    message: str,
    signature: str,
) -> dict[str, Any]:
    """Atomically enroll the first wallet through the root-only helper."""
    if os.geteuid() != 0:
        raise EnvironmentError("signed wallet enrollment must run as root")
    address = normalize_wallet_address(address)
    lock_path = config.state_root / ".registration.lock"
    with _locked(lock_path):
        current = EnvironmentConfig.load(config.path)
        valid, reason = verify_registration_request(current, address, message, signature)
        if not valid:
            raise EnvironmentError(reason)
        raw = _read_json(current.path, None)
        if not isinstance(raw, dict):
            raise EnvironmentError("environment config must be a JSON object")
        wallets_config = raw.setdefault("wallets", {})
        if not isinstance(wallets_config, dict):
            raise EnvironmentError("wallets must be an object")
        wallets = wallets_config.setdefault("authorized", [])
        if not isinstance(wallets, list):
            raise EnvironmentError("wallets.authorized must be a list")
        if wallets:
            raise EnvironmentError("registration is already complete")
        owner = current.path.stat()
        wallets_config["authorized"] = [{"address": address, "role": "admin"}]
        _write_json(current.path, raw, current.path.stat().st_mode & 0o777 or 0o640)
        os.chown(current.path, owner.st_uid, owner.st_gid)
    return {"registered": True, "address": address, "role": "admin", "config": str(config.path)}


def validate_target_versions(
    config: EnvironmentConfig,
    component_ids: list[str] | tuple[str, ...] | set[str],
    target_versions: Mapping[str, str] | None,
) -> dict[str, str]:
    if target_versions is None:
        return {}
    if not isinstance(target_versions, Mapping):
        raise EnvironmentError("target_versions must be an object")
    selected = set(component_ids)
    known = config.component_map()
    validated: dict[str, str] = {}
    for component_id, version in target_versions.items():
        if not isinstance(component_id, str) or component_id not in selected:
            raise EnvironmentError("target_versions must only contain selected components")
        if not isinstance(version, str) or not TARGET_VERSION_PATTERN.fullmatch(version):
            raise EnvironmentError(f"target version is invalid: {component_id}")
        component = known.get(component_id)
        if component is None or component.target_version_arg is None:
            raise EnvironmentError(f"component does not support exact target versions: {component_id}")
        validated[component_id] = version
    return dict(sorted(validated.items()))


def verify_control_authorization(config: EnvironmentConfig, run: Mapping[str, Any]) -> tuple[bool, str]:
    from .environment_auth import EnvironmentAuthority

    return EnvironmentAuthority(config).consume_run(run)


def read_environment_policy(config: EnvironmentConfig) -> dict[str, Any] | None:
    from .environment_auth import EnvironmentAuthority

    return EnvironmentAuthority(config).read_policy()


def policy_message(policy: Mapping[str, Any]) -> str:
    payload = {
        "allow_restart": bool(policy.get("allow_restart", True)),
        "components": sorted(policy.get("components", [])),
        "expires_at": policy.get("expires_at"),
        "policy_id": policy.get("policy_id"),
        "release_channel": policy.get("release_channel", "stable"),
        "schedule": "daily@04:00+08",
        "target_versions": dict(sorted((policy.get("target_versions") or {}).items())),
        "vm_id": policy.get("vm_id"),
    }
    return "ADE-ENVIRONMENT-POLICY-V1\n" + _json_bytes(payload).decode("utf-8")


def wallet_role(config: EnvironmentConfig, address: str) -> str | None:
    try:
        address = normalize_wallet_address(address)
    except EnvironmentError:
        return None
    for wallet in config.authorized_wallets:
        if wallet["address"] == address:
            return wallet["role"]
    return None


def verify_policy(config: EnvironmentConfig, policy: Mapping[str, Any] | None) -> tuple[bool, str]:
    if not policy:
        return False, "daily updates are not enabled"
    if policy.get("authorization") == "session":
        # Only the root-owned canonical record authorizes persistent schedules.
        if policy != read_environment_policy(config):
            return False, "policy does not match the approved schedule"
        if not policy.get("enabled"):
            return False, "daily updates are disabled"
    if policy.get("vm_id") != config.vm_id:
        return False, "policy VM identity does not match"
    if policy.get("release_channel", "stable") != "stable":
        return False, "only stable release channel is allowed"
    if not isinstance(policy.get("components"), list) or not all(isinstance(item, str) for item in policy["components"]):
        return False, "policy components are invalid"
    ids = set(policy["components"])
    known = {component.id for component in config.components if component.enabled and component.update_command}
    if not ids or not ids.issubset(known):
        return False, "policy components are outside the VM allowlist"
    try:
        target_versions = validate_target_versions(config, ids, policy.get("target_versions", {}))
    except EnvironmentError as exc:
        return False, str(exc)
    if target_versions != dict(sorted((policy.get("target_versions") or {}).items())):
        return False, "policy target versions are invalid"
    if policy.get("authorization") == "session":
        return True, "daily updates enabled until disabled"
    signer = policy.get("signer")
    signature = policy.get("signature")
    if not isinstance(signer, str) or wallet_role(config, signer) not in {"operator", "admin"}:
        return False, "policy signer is not authorized"
    if not isinstance(signature, str) or not verify_wallet_signature(signer, policy_message(policy).encode(), signature):
        return False, "policy signature is invalid"
    try:
        if parse_timestamp(str(policy["expires_at"])) <= utc_now():
            return False, "policy has expired"
    except (KeyError, EnvironmentError):
        return False, "policy expiry is invalid"
    return True, "ok"


def _component_order(config: EnvironmentConfig, requested: list[str]) -> list[Component]:
    components = config.component_map()
    selected: set[str] = set()

    def include(component_id: str) -> None:
        if component_id not in components:
            raise EnvironmentError(f"unknown component: {component_id}")
        if component_id in selected:
            return
        selected.add(component_id)
        for dependency in components[component_id].dependencies:
            include(dependency)

    for component_id in requested:
        include(component_id)
    result: list[Component] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(component_id: str) -> None:
        if component_id in visited:
            return
        if component_id in visiting:
            raise EnvironmentError("component dependency cycle detected")
        visiting.add(component_id)
        for dependency in components[component_id].dependencies:
            if dependency in selected:
                visit(dependency)
        visiting.remove(component_id)
        visited.add(component_id)
        result.append(components[component_id])

    for component_id in requested:
        visit(component_id)
    return result


def environment_health(config: EnvironmentConfig, include_private: bool = False) -> dict[str, Any]:
    statuses = []
    for component in config.components:
        if not component.enabled or (not include_private and not component.public):
            continue
        statuses.append(component.public_status(component.probe()))
    overall = "healthy"
    if any(item["status"] in {"unhealthy", "unknown"} for item in statuses):
        overall = "degraded" if any(item["status"] == "healthy" for item in statuses) else "unhealthy"
    return {
        "service": "agent-environment",
        "version": ENVIRONMENT_VERSION,
        "vm_id": config.vm_id,
        "status": overall,
        "checked_at": iso_now(),
        "components": statuses,
    }


def render_snapshot(config: EnvironmentConfig, health: Mapping[str, Any], run: Mapping[str, Any] | None = None) -> str:
    component_rows = []
    for item in health.get("components", []):
        status = html.escape(str(item.get("status", "unknown")))
        component_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('label', item.get('id', ''))))}</td>"
            f"<td><span class=\"status {status}\">{status}</span></td>"
            f"<td>{html.escape(str(item.get('version') or 'unknown'))}</td>"
            f"<td>{html.escape(str(item.get('checked_at') or ''))}</td>"
            "</tr>"
        )
    run_text = "No update run recorded"
    if run:
        run_text = f"{html.escape(str(run.get('status', 'unknown')))} · {html.escape(str(run.get('finished_at') or run.get('created_at') or ''))}"
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>ADES · {html.escape(config.vm_id)}</title>
<style>
body{{font:15px system-ui,sans-serif;background:#101318;color:#e8edf2;margin:0;padding:32px}}
main{{max-width:960px;margin:auto}}h1{{font-size:28px;margin:0 0 8px}}p{{color:#aab5c1}}
table{{width:100%;border-collapse:collapse;background:#171c23;border:1px solid #2a3440;border-radius:12px;overflow:hidden}}
th,td{{padding:13px 15px;text-align:left;border-bottom:1px solid #2a3440}}th{{color:#aab5c1;font-size:12px;text-transform:uppercase;letter-spacing:.08em}}
.status{{border-radius:999px;padding:4px 9px;font-size:12px}}.healthy{{background:#123d2a;color:#7ce2a5}}.degraded,.unknown{{background:#433514;color:#ffd37a}}.unhealthy{{background:#4b1e27;color:#ff9aa8}}
code{{color:#9fd4ff}}
</style></head><body><main>
<h1>ADES</h1><p>Agent Development Environment Service</p><p>VM <code>{html.escape(config.vm_id)}</code> · overall <strong>{html.escape(str(health.get('status', 'unknown')))}</strong></p>
<table><thead><tr><th>Component</th><th>Health</th><th>Version</th><th>Checked</th></tr></thead><tbody>{''.join(component_rows)}</tbody></table>
<p>Last update run: {run_text}</p>
</main></body></html>"""


def publish_snapshot(
    config: EnvironmentConfig,
    health: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Publish an opt-in sanitized snapshot through ADE's artifact CLI."""
    if not config.snapshot.get("enabled", False):
        return None
    name = config.snapshot.get("name") or f"environment-{config.vm_id}"
    artifact_root = config.snapshot.get("artifact_root", "/var/lib/ade/web-artifacts")
    base_url = config.snapshot.get("base_url", "http://172.16.240.41:80")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="agent-environment-snapshot-", suffix=".html", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(render_snapshot(config, health, run))
        temporary.chmod(0o644)
        command = [
            "/usr/sbin/runuser",
            "-u",
            "orca",
            "--",
            "/usr/bin/env",
            "HOME=/home/orca",
            "ADE_WEB_ARTIFACT_ROOT=" + str(artifact_root),
            "ADE_WEB_ARTIFACT_BASE_URL=" + str(base_url),
            "/home/orca/.local/bin/uv",
            "run",
            "--frozen",
            "--project",
            str(config.source_root),
            "python",
            "-m",
            "ade.cli",
            "publish-html",
            "publish",
            str(temporary),
            "--name",
            str(name),
            "--artifact-root",
            str(artifact_root),
            "--base-url",
            str(base_url),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            detail = _redact((result.stderr or result.stdout).strip() or "publisher unavailable")
            return {"status": "blocked", "error": "publish blocked: " + detail}
        try:
            receipt = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"status": "blocked", "error": "publish blocked: invalid publisher receipt"}
        return {"status": "published", **receipt}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "blocked", "error": "publish blocked: " + _redact(str(exc))}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class UpdateCoordinator:
    def __init__(self, config: EnvironmentConfig, store: RunStore):
        self.config = config
        self.store = store

    def create_run(
        self,
        trigger: str,
        component_ids: list[str] | None = None,
        requested_by: str = "system",
        action: str = "update",
        target_versions: Mapping[str, str] | None = None,
        authorization: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if trigger == "scheduled" and component_ids is None:
            policy = read_environment_policy(self.config)
            selected = list(policy.get("components", [])) if isinstance(policy, Mapping) else []
            target_versions = policy.get("target_versions", {}) if isinstance(policy, Mapping) else {}
        else:
            selected = component_ids or [component.id for component in self.config.components if component.enabled and component.update_command]
            target_versions = target_versions or {}
        if action not in {"update", "restart"}:
            raise EnvironmentError("unsupported update action")
        for component_id in selected:
            component = self.config.component_map().get(component_id)
            if component is None or not component.enabled:
                raise EnvironmentError(f"unknown component: {component_id}")
            if action == "update" and component.update_command is None:
                raise EnvironmentError(f"component has no update command: {component_id}")
            if action == "restart" and component.restart_command is None:
                raise EnvironmentError(f"component has no restart command: {component_id}")
        _component_order(self.config, selected)
        validated_targets = validate_target_versions(self.config, selected, target_versions)
        return self.store.create(trigger, selected, requested_by, action, validated_targets, authorization)

    def execute(self, run_id: str, expected_trigger: str | None = None) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise EnvironmentError(f"update run not found: {run_id}")
        if expected_trigger is not None and run.get("trigger") != expected_trigger:
            raise EnvironmentError("update run trigger does not match the privileged invocation")
        if run.get("trigger") not in {"scheduled", "manual"}:
            raise EnvironmentError("unsupported update trigger")
        if run.get("status") != "queued":
            raise EnvironmentError("update run has already started")
        if run.get("trigger") == "scheduled":
            policy = read_environment_policy(self.config)
            valid, reason = verify_policy(self.config, policy)
            if not valid:
                blocked = self.store.update(
                    run_id,
                    status="blocked",
                    finished_at=iso_now(),
                    error=reason,
                )
                publication = publish_snapshot(self.config, environment_health(self.config), blocked)
                return self.store.update(run_id, publication=publication) if publication else blocked
            if (set(run.get("component_ids", [])) != set(policy["components"])
                    or run.get("action") != "update"
                    or run.get("target_versions", {}) != policy.get("target_versions", {})):
                blocked = self.store.update(
                    run_id,
                    status="blocked",
                    finished_at=iso_now(),
                    error="scheduled run does not match the approved policy",
                )
                publication = publish_snapshot(self.config, environment_health(self.config), blocked)
                return self.store.update(run_id, publication=publication) if publication else blocked
        elif run.get("trigger") == "manual":
            valid, reason = verify_control_authorization(self.config, run)
            if not valid:
                blocked = self.store.update(
                    run_id,
                    status="blocked",
                    finished_at=iso_now(),
                    error=reason,
                )
                publication = publish_snapshot(self.config, environment_health(self.config), blocked)
                return self.store.update(run_id, publication=publication) if publication else blocked
        started_at = iso_now()
        self.store.update(run_id, status="running", started_at=started_at)
        results = []
        failed_ids: set[str] = set()
        try:
            ordered = _component_order(self.config, list(run["component_ids"]))
            action = run.get("action", "update")
            for component in ordered:
                if any(dependency in failed_ids for dependency in component.dependencies):
                    results.append({"id": component.id, "status": "blocked", "detail": "dependency failed"})
                    failed_ids.add(component.id)
                    continue
                command = component.update_command if action == "update" else component.restart_command
                if not command:
                    results.append({"id": component.id, "status": "skipped", "detail": f"no {action} command declared"})
                    continue
                command = list(command)
                target_version = run.get("target_versions", {}).get(component.id)
                if action == "update" and target_version is not None:
                    if component.target_version_arg is None:
                        results.append({
                            "id": component.id,
                            "status": "failed",
                            "action": action,
                            "detail": "component does not support exact target versions",
                        })
                        failed_ids.add(component.id)
                        continue
                    command.extend([component.target_version_arg, target_version])
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=component.timeout_seconds,
                    check=False,
                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                )
                probe = component.probe()
                ok = result.returncode == 0 and probe["status"] == "healthy"
                rollback_status = None
                if not ok and action == "update" and component.rollback_command:
                    rollback = subprocess.run(
                        list(component.rollback_command),
                        capture_output=True,
                        text=True,
                        timeout=component.timeout_seconds,
                        check=False,
                        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                    )
                    rollback_status = "completed" if rollback.returncode == 0 else "failed"
                    probe = component.probe()
                item = {
                    "id": component.id,
                    "status": "completed" if ok else "failed",
                    "action": action,
                    "returncode": result.returncode,
                    "version": probe.get("version"),
                    "health": probe.get("status"),
                    "rollback": rollback_status,
                    "detail": "ok" if ok else _redact((result.stderr or result.stdout).strip() or probe.get("detail", "update failed")),
                }
                results.append(item)
                if not ok:
                    failed_ids.add(component.id)
        except (EnvironmentError, OSError, subprocess.SubprocessError) as exc:
            results.append({"id": "environment", "status": "failed", "detail": _redact(str(exc))})
            failed_ids.add("environment")
        status = "completed" if not failed_ids else ("partial_failure" if any(item.get("status") == "completed" for item in results) else "failed")
        finished = self.store.update(
            run_id,
            status=status,
            finished_at=iso_now(),
            components=results,
            error=None if not failed_ids else "one or more components failed",
        )
        publication = publish_snapshot(self.config, environment_health(self.config), finished)
        return self.store.update(run_id, publication=publication) if publication else finished


def dashboard_html(config: EnvironmentConfig) -> str:
    return """<!doctype html>
<html lang=\"zh-TW\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>ADES · Agent Development Environment Service</title>
<style>
:root{--danger:#ff9eaa;--warning:#f3ce83}

:root{color-scheme:dark;--bg:#0a1012;--sidebar:#0d1517;--surface:#111a1d;--line:#263639;--text:#e8f0ec;--muted:#9aada6;--accent:#8fd8b8;--accent-strong:#baf2d6;font:15px/1.55 \"Avenir Next\",\"Segoe UI Variable\",\"Noto Sans TC\",sans-serif;background:var(--bg);color:var(--text)}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;background:radial-gradient(ellipse at 82% 0%,#162626 0,transparent 38%),var(--bg)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}p{margin:7px 0;color:var(--muted)}h1,h2,h3{margin:0;line-height:1.2;letter-spacing:-.025em}h1{font-size:clamp(24px,2.4vw,31px);font-weight:650}h2{font-size:20px;font-weight:620}h3{font-size:16px;font-weight:600}button{font:inherit;border:1px solid #7bcaa6;background:#285b48;color:#eafff4;border-radius:10px;padding:11px 16px;cursor:pointer;transition:background .18s ease,transform .18s ease,border-color .18s ease}button:hover:not(:disabled){background:#347458;border-color:#a0e1bf;transform:translateY(-1px)}button:active:not(:disabled){transform:translateY(1px) scale(.99)}button:disabled{opacity:.5;cursor:not-allowed}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent-strong);outline-offset:3px}
.skip-link{position:absolute;left:16px;top:-48px;padding:9px 12px;background:var(--accent-strong);color:#102019;border-radius:8px;z-index:2}.skip-link:focus{top:12px}.app-shell{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100dvh}.sidebar{position:sticky;top:0;height:100dvh;display:flex;flex-direction:column;padding:27px 17px 18px;background:var(--sidebar);border-right:1px solid var(--line)}.brand{display:flex;align-items:center;gap:12px;margin:0 0 48px;padding:0 7px;color:var(--text)}.brand:hover{text-decoration:none}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid #456a5a;border-radius:12px;background:#162621;color:var(--accent-strong);font-weight:750;letter-spacing:-.06em}.brand-name{display:block;font-size:14px;font-weight:700;letter-spacing:.04em}.brand-caption{display:block;margin-top:1px;color:#82958e;font-size:10px;letter-spacing:.11em;text-transform:uppercase}.sidebar-label{margin:0 10px 11px;color:#71837d;font-size:10px;font-weight:650;letter-spacing:.14em;text-transform:uppercase}.side-nav{display:grid;gap:5px}.side-link{display:flex;align-items:center;gap:12px;padding:10px 11px;border:1px solid transparent;border-radius:10px;color:#aebdb7;font-size:13px;transition:background .16s ease,color .16s ease}.side-link:hover{background:#14201f;color:var(--text);text-decoration:none}.side-link[aria-current=\"page\"]{background:#172823;border-color:#294338;color:var(--accent-strong)}.nav-index{width:20px;color:#71857c;font-size:10px;font-variant-numeric:tabular-nums}.side-link[aria-current=\"page\"] .nav-index{color:var(--accent)}.sidebar-bottom{display:flex;align-items:center;gap:9px;margin-top:auto;padding:13px 10px;border-top:1px solid var(--line);color:#899c94;font-size:11px}.service-dot{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px #20352b}
.workspace{min-width:0}.topbar{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:27px clamp(20px,4vw,58px) 22px;border-bottom:1px solid #1e2b2d}.eyebrow{margin:0 0 5px;color:#81958d;font-size:10px;font-weight:650;letter-spacing:.15em;text-transform:uppercase}.topbar-actions{display:flex;align-items:center;gap:10px}.identity{max-width:250px;overflow:hidden;color:#acbbb5;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.button-quiet{padding:8px 12px;border-color:var(--line);background:transparent;color:#c4d0cb;font-size:12px}.button-quiet:hover:not(:disabled){background:#172221;border-color:#526b5f}.workspace main{max-width:1500px;margin:0 auto;padding:18px clamp(20px,4vw,58px) 68px}
.auth-stage{display:grid;place-items:center;min-height:min(47vh,430px);padding:24px 0 36px}.auth-card{position:relative;width:min(100%,510px);padding:31px 34px 27px;overflow:hidden;border:1px solid #2d433b;border-radius:20px;background:linear-gradient(150deg,#14201d,#10191b 72%);box-shadow:0 28px 70px #02080755}.auth-card:before{position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent);content:\"\"}.auth-mark{display:grid;place-items:center;width:43px;height:43px;margin-bottom:22px;border:1px solid #456a5a;border-radius:14px;background:#1c3028;color:var(--accent-strong);font-size:13px;font-weight:750;letter-spacing:-.04em}.auth-tag{display:inline-flex;align-items:center;gap:7px;margin:0 0 14px;color:#a4c7b5;font-size:10px;font-weight:650;letter-spacing:.12em;text-transform:uppercase}.auth-tag:before{width:6px;height:6px;border-radius:50%;background:var(--accent);content:\"\"}.auth-card h2{font-size:25px}.auth-description{max-width:42ch;margin:11px 0 23px;font-size:14px}.auth-card #wallet{min-width:190px}.auth-note{min-height:22px;margin:13px 0 0;font-size:12px}.auth-note.error,.trend-error{color:var(--danger)}.auth-footnote{margin-top:20px;padding-top:16px;border-top:1px solid #263639;color:#7f938b;font-size:11px}
.section-block{margin:22px 0 0;padding:23px 25px;border:1px solid var(--line);border-radius:17px;background:#10181a}.section-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:21px}.section-title{display:flex;align-items:center;gap:11px}.section-mark{display:grid;place-items:center;width:34px;height:34px;border:1px solid #29473b;border-radius:10px;background:#172720;color:var(--accent);font-size:11px;font-weight:700}.section-subtitle{max-width:62ch;margin-top:7px;font-size:12px}.field-row{display:flex;flex-wrap:wrap;align-items:end;gap:12px}.field{display:grid;gap:6px;color:#aab9b3;font-size:12px}.field select{min-width:180px}input,select{background:#0c1315;color:var(--text);border:1px solid #3a4b4c;border-radius:9px;padding:9px 11px;max-width:100%;font:inherit}select{cursor:pointer}.rank-list{list-style:none;margin:18px 0 0;padding:0}.rank-item{display:grid;grid-template-columns:40px minmax(0,1fr);gap:13px;padding:15px 2px;border-top:1px solid #263437}.rank-no{padding-top:1px;color:#7fa991;font-size:16px;font-weight:650;font-variant-numeric:tabular-nums}.rank-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.repo,.source-link{color:var(--accent-strong);text-decoration:none}.repo{font-weight:650}.repo:hover,.source-link:hover{text-decoration:underline}.lang{border:1px solid #39494a;border-radius:6px;padding:2px 7px;color:#afc1b9;font-size:10px}.rank-meta{display:flex;flex-wrap:wrap;gap:12px;color:#91a39b;font-size:11px;margin-top:6px}.rank-desc{max-width:72ch;margin-top:6px;font-size:12px;word-break:break-word}.trend-error{font-size:12px}.muted{color:var(--muted);font-size:12px}.row{display:flex;justify-content:space-between;align-items:center;gap:14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.card{padding:17px;border:1px solid #29383a;border-radius:13px;background:#141e20}.card h3{margin:7px 0 8px}.card p{font-size:12px}.badge{display:inline-block;border-radius:6px;padding:3px 7px;font-size:10px;font-weight:650;letter-spacing:.02em}.healthy{background:#183528;color:#a8e6be}.degraded,.unknown{background:#3a301b;color:var(--warning)}.unhealthy{background:#46242a;color:#ffacb5}.updating{background:#233443;color:#a8c9e7}code{color:var(--accent-strong)}pre{white-space:pre-wrap;word-break:break-word;color:#c9d1d9}.notice{border-color:#695b35;background:#211e16}.panel-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:18px}
@media(max-width:760px){.app-shell{grid-template-columns:minmax(0,1fr)}.sidebar{position:static;height:auto;padding:14px 17px 10px;border-right:0;border-bottom:1px solid var(--line)}.brand{margin:0 0 13px}.sidebar-label,.sidebar-bottom{display:none}.side-nav{display:flex;gap:5px;overflow-x:auto;padding-bottom:2px}.side-link{flex:0 0 auto;padding:8px 10px}.topbar{align-items:flex-start;padding:19px 18px 16px}.topbar-actions{gap:6px}.identity{max-width:130px}.workspace main{padding:13px 16px 44px}.auth-stage{min-height:0;padding:24px 0 34px}.auth-card{padding:27px 25px 23px}.section-block{padding:18px 16px;margin-top:15px}.section-head{flex-direction:column;align-items:stretch}.section-head>.row{align-items:flex-start}.field{flex:1}.field select{min-width:0;width:100%}.rank-item{grid-template-columns:30px minmax(0,1fr);gap:9px}}
@media(max-width:420px){.topbar{flex-direction:column;gap:12px}.topbar-actions{width:100%;justify-content:space-between}.identity{max-width:180px}.auth-card h2{font-size:22px}.row{align-items:flex-start;flex-direction:column}}
.global-error{max-width:300px;color:var(--danger);font-size:11px}</style></head><body data-registration-required=\"__REGISTRATION_REQUIRED__\"><a class=\"skip-link\" href=\"#main-content\">略過導覽</a><div class=\"app-shell\">
<aside class=\"sidebar\"><a class=\"brand\" href=\"#trending\"><span class=\"brand-mark\">AE</span><span><span class=\"brand-name\">ADES</span><span class=\"brand-caption\">Environment service</span></span></a>
<p class=\"sidebar-label\">Workspace</p><nav class=\"side-nav\" aria-label=\"主要導覽\"><a class=\"side-link\" href=\"#trending\" aria-current=\"page\"><span class=\"nav-index\">01</span><span>專案排行</span></a>
<p class=\"sidebar-label\" id=\"private-nav-label\" hidden>管理</p><a class=\"side-link\" data-private-nav href=\"#environment\" hidden><span class=\"nav-index\">02</span><span>環境總覽</span></a><a class=\"side-link\" data-private-nav href=\"#schedule-panel\" hidden><span class=\"nav-index\">03</span><span>更新排程</span></a><a class=\"side-link\" data-private-nav href=\"#runs-panel\" hidden><span class=\"nav-index\">04</span><span>執行紀錄</span></a></nav>
<div class=\"sidebar-bottom\"><span class=\"service-dot\"></span><span>公開閱讀模式</span></div></aside>
<div class=\"workspace\"><header class=\"topbar\"><div><p class=\"eyebrow\">ADES / PUBLIC FEED</p><h1>GitHub 專案排行</h1></div><div class=\"topbar-actions\"><span id=\"auth-identity\" class=\"identity\">尚未登入</span><span id=\"global-error\" class=\"global-error\" role=\"alert\" hidden></span><button id=\"refresh\" class=\"button-quiet\" hidden>重新整理</button><button id=\"logout\" class=\"button-quiet\" hidden>登出</button></div></header>
<main id=\"main-content\"><section class=\"auth-stage\" id=\"auth-panel\" aria-labelledby=\"auth-title\"><article class=\"auth-card\"><div class=\"auth-mark\" aria-hidden=\"true\">AE</div><p class=\"auth-tag\" id=\"registration-state\">檢查註冊狀態</p><h2 id=\"auth-title\">連接 MetaMask</h2><p id=\"auth-description\" class=\"auth-description\">正在確認這台環境的註冊狀態。</p><button id=\"wallet\" disabled>請稍候</button><p id=\"auth\" class=\"auth-note\" role=\"status\" aria-live=\"polite\"></p><p class=\"auth-footnote\">登入後可查看環境狀態、更新排程與執行紀錄。</p></article></section>
<section class="section-block" id="trending"><div class="section-head"><div><div class="section-title"><span class="section-mark">01</span><h2>GitHub 專案即時排行</h2></div><p class="section-subtitle">唯讀查看公開排行，不會寫入候選、收藏或 ADE 管理資料。</p></div><button id="trending-refresh">重新整理</button></div>
<div class="field-row"><label class="field">資料來源<select id="trending-source"><option value="github">GitHub Trending（全部語言）</option><option value="trendshift">Trendshift（熱門榜）</option></select></label>
<label class="field">期間<select id="trending-since"><option value="daily">每日</option><option value="weekly">每週</option><option value="monthly">每月</option></select></label></div>
<p id="trending-error" class="trend-error" hidden></p><p id="trending-meta" class="muted" aria-live="polite">載入排行中…</p><p id="trending-status" class="muted" aria-live="polite"></p><ol id="trending-items" class="rank-list"></ol></section>
<div id="private" hidden><section class=\"section-block\" id=\"environment\"><div class=\"section-head\"><div><div class=\"section-title\"><span class=\"section-mark\">02</span><h2>環境總覽</h2></div><p id=\"checked\" class=\"section-subtitle\"></p></div><div class=\"row\"><strong id=\"overall\"></strong><button id=\"update-all\" disabled>全部更新</button></div></div><div id=\"components\" class=\"grid\"></div></section>
<section class="section-block" id="artifact-panel" hidden><h2>網頁發布</h2>
<p>發布後，持有連結的人都能開啟作品。請勿上傳密鑰或私人資料。</p>
<p id="artifact-status" role="status" aria-live="polite"></p>
<form id="artifact-form" hidden>
<label for="artifact-name">作品名稱</label><input id="artifact-name" required pattern="[a-z][a-z0-9-]{0,63}" maxlength="64" placeholder="my-report">
<p class="muted">使用小寫英文字母、數字與連字號，以英文字母開頭。</p>
<label for="artifact-files">選擇 HTML 與資源檔</label><input id="artifact-files" type="file" multiple>
<label for="artifact-folder">或選擇完整資料夾</label><input id="artifact-folder" type="file" webkitdirectory multiple>
<label for="artifact-entrypoint">首頁</label><select id="artifact-entrypoint" required></select>
<p id="artifact-selection" class="muted">最多 200 個檔案，合計 10 MiB。資料夾會保留內部路徑。</p>
<button id="artifact-publish" type="submit" disabled>發布網頁</button>
</form><div id="artifacts" class="grid"></div></section>
<section class=\"section-block\" id=\"schedule-panel\"><div class=\"section-title\"><span class=\"section-mark\">03</span><h2>每日更新排程</h2></div><p id=\"schedule\"></p><p id=\"policy\" class=\"muted\"></p><div class=\"panel-actions\"><button id=\"authorize-policy\" disabled>啟用每日更新</button><button class=\"button-quiet\" id=\"disable-policy\" disabled>停用每日更新</button></div></section>
<section class=\"section-block\" id=\"runs-panel\"><div class=\"section-title\"><span class=\"section-mark\">04</span><h2>更新執行紀錄</h2></div><div id=\"runs\" class=\"muted\"></div></section>
</div>
<script>
const state = {session:null, address:null, role:null, provider:null, components:[], artifacts:[], uploadFiles:[], publishing:false, publisherReady:false, registrationRequired:document.body.dataset.registrationRequired === 'true', connecting:false, generation:0};
const storageKey = 'ade.environment.session.v1';
let sessionExpiryTimer;
let trendingRequestId = 0;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function remember(value) {
  try { value ? sessionStorage.setItem(storageKey, JSON.stringify(value)) : sessionStorage.removeItem(storageKey); }
  catch (_) { /* Memory-only login remains usable when browser storage is disabled. */ }
}
async function json(url, options={}) {
  const token = state.session;
  const response = await fetch(url, {...options, headers:{'Content-Type':'application/json',
    ...(token ? {Authorization:`Bearer ${token}`} : {}), ...(options.headers || {})}});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && token === state.session) resetWallet();
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}
async function revoke(token) {
  if (!token) return;
  const response = await fetch('/api/v1/auth/logout', {method:'POST', headers:{'Content-Type':'application/json', Authorization:`Bearer ${token}`}, body:'{}'});
  if (!response.ok && response.status !== 401) throw new Error('Sign-out failed. Please retry.');
}
const walletProviders = [];
window.addEventListener('eip6963:announceProvider', event => {
  if (event.detail?.info?.rdns === 'io.metamask') {
    walletProviders.push(event.detail.provider);
    if (state.session && !state.provider) attachProvider(event.detail.provider);
  }
});
window.dispatchEvent(new Event('eip6963:requestProvider'));
function walletObject() {
  return walletProviders[0] || window.ethereum?.providers?.find(p => p.isMetaMask && !p.isRabby)
    || (window.ethereum?.isMetaMask && !window.ethereum?.isRabby ? window.ethereum : null);
}
function attachProvider(provider) {
  if (state.provider === provider) return;
  state.provider?.removeListener?.('accountsChanged', walletChanged);
  state.provider?.removeListener?.('disconnect', walletChanged);
  state.provider = provider;
  provider.on?.('accountsChanged', walletChanged);
  provider.on?.('disconnect', walletChanged);
}
function walletChanged(accounts) {
  if (!state.session && !state.address) return;
  if (Array.isArray(accounts) && accounts[0]?.toLowerCase() === state.address) return;
  const token = state.session;
  resetWallet();
  revoke(token).catch(error => { $('auth').textContent = error.message; });
}
async function sign(message) {
  const provider = state.provider;
  if (!provider) throw new Error('Connect MetaMask first');
  const accounts = await provider.request({method:'eth_accounts'});
  if (accounts[0]?.toLowerCase() !== state.address) throw new Error('MetaMask account changed. Reconnect before signing.');
  const hex = '0x' + Array.from(new TextEncoder().encode(message), b => b.toString(16).padStart(2,'0')).join('');
  return provider.request({method:'personal_sign', params:[hex, state.address]});
}
function renderAuthMode() {
  if (state.session) {
    $('auth-panel').hidden = true;
    return;
  }
  $('auth-panel').hidden = false;
  const registering = state.registrationRequired;
  $('registration-state').textContent = state.connecting
    ? 'WALLET 授權進行中'
    : registering ? 'FIRST REGISTRATION' : 'WALLET LOGIN';
  $('auth-title').textContent = registering ? '註冊這台環境' : '登入 ADES';
  $('auth-description').textContent = registering
    ? '尚未完成註冊。第一個註冊的 wallet 會成為這台環境的管理者。'
    : '已完成首次註冊。請用已授權的 MetaMask wallet 登入。';
  $('wallet').textContent = state.connecting
    ? (registering ? '註冊中…' : '登入中…')
    : (registering ? '使用 MetaMask 註冊' : '使用 MetaMask 登入');
  $('wallet').disabled = state.connecting;
}
function resetWallet() {
  clearTimeout(sessionExpiryTimer);
  state.generation++;
  state.session = state.address = state.role = null;
  state.components = [];
  state.artifacts = [];
  state.uploadFiles = [];
  state.publisherReady = false;
  for (const id of ['artifact-name','artifact-files','artifact-folder']) $(id).value = '';
  $('artifact-entrypoint').innerHTML = '';
  state.connecting = false;
  remember(null);
  $('auth').textContent = '';
  $('auth').className = 'auth-note';
  $('auth-identity').textContent = '尚未登入';
  $('global-error').textContent = '';
  $('global-error').hidden = true;
  for (const id of ['private','refresh','logout','private-nav-label','artifact-panel']) $(id).hidden = true;
  for (const id of ['overall','checked','components','schedule','policy','runs','artifacts','artifact-status','artifact-selection']) $(id).textContent = '';
  document.querySelectorAll('[data-private-nav]').forEach(link => { link.hidden = true; });
  $('overall').className = '';
  for (const id of ['update-all','authorize-policy','disable-policy','artifact-publish']) $(id).disabled = true;
  renderAuthMode();
}
function acceptSession(session) {
  state.session = session.session;
  state.address = session.address;
  state.role = session.role;
  state.connecting = false;
  remember(session);
  clearTimeout(sessionExpiryTimer);
  sessionExpiryTimer = setTimeout(resetWallet, Math.max(0, Date.parse(session.expires_at) - Date.now()));
  $('auth-identity').textContent = session.role + ' · ' + session.address;
  $('auth').textContent = '';
  renderAuthMode();
}
async function login() {
  const address = state.address, generation = state.generation;
  const challenge = await json('/api/v1/auth/challenge?address=' + encodeURIComponent(address) + '&origin=' + encodeURIComponent(location.origin));
  const signature = await sign(challenge.message);
  const verified = await json('/api/v1/auth/verify', {method:'POST', body:JSON.stringify({
    challenge_id:challenge.challenge_id, address, message:challenge.message, signature})});
  if (state.generation !== generation || state.address !== address) {
    await revoke(verified.session);
    throw new Error('Account changed. Please reconnect.');
  }
  acceptSession(verified);
  await refresh();
}
async function connect() {
  if (state.session || state.connecting) return;
  resetWallet();
  state.connecting = true;
  const generation = state.generation;
  renderAuthMode();
  try {
  const provider = walletObject();
  if (!provider) throw new Error('MetaMask extension not detected. Install or enable MetaMask, then reload this page.');
  attachProvider(provider);
  const accounts = await provider.request({method:'eth_requestAccounts'});
  if (state.generation !== generation) return;
  state.address = accounts[0]?.toLowerCase();
  if (!state.address) throw new Error('MetaMask did not return an account');
  let registration;
  try { registration = await json('/api/v1/registration/challenge?address=' + encodeURIComponent(state.address)); }
  catch (error) {
    if (error.status !== 409) throw error;
    state.registrationRequired = false;
  }
  if (registration) {
    state.registrationRequired = true;
    renderAuthMode();
    $('auth').textContent = '請在 MetaMask 確認首次註冊簽章。';
    const signature = await sign(registration.message);
    if (state.generation !== generation) return;
    await json('/api/v1/registration', {method:'POST', body:JSON.stringify({
      challenge_id:registration.challenge_id, address:state.address, message:registration.message, signature})});
    state.registrationRequired = false;
  }
  renderAuthMode();
  if (state.generation !== generation) return;
  await login();
  } finally {
    if (state.generation === generation && !state.session) {
      state.connecting = false;
      renderAuthMode();
    }
  }
}
async function beginUpdate(ids) {
  if (!state.session) throw new Error('Connect a wallet first');
  await json('/api/v1/update-runs', {method:'POST', body:JSON.stringify({action:'update', component_ids:ids, target_versions:{}})});
  await refresh();
}
async function setPolicy(enabled) {
  if (!state.session || state.role !== 'admin') throw new Error('Admin wallet required');
  const components = state.components.filter(component => component.update_supported).map(component => component.id);
  await json('/api/v1/policy', {method:'PUT', body:JSON.stringify({enabled, components, allow_restart:true, release_channel:'stable'})});
  await refresh();
}
async function refresh() {
  if (!state.session) { resetWallet(); return; }
  const token = state.session;
  const [health, schedule, runs, artifacts] = await Promise.all([json('/api/v1/health'), json('/api/v1/schedule'), json('/api/v1/runs'), json('/api/v1/artifacts').catch(error => { if (error.status === 401) throw error; return {enabled:true, artifacts:[], error:error.message}; })]);
  if (token !== state.session) return;
  renderArtifacts(artifacts);
  $('private').hidden = false;
  $('auth-panel').hidden = true;
  $('refresh').hidden = $('logout').hidden = false;
  $('private-nav-label').hidden = false;
  document.querySelectorAll('[data-private-nav]').forEach(link => { link.hidden = false; });
  $('auth-identity').textContent = state.role + ' · ' + state.address;
  state.components = health.components || [];
  $('overall').textContent = `${health.status} · ${health.vm_id}`;
  $('overall').className = `badge ${health.status}`;
  $('checked').textContent = `Checked ${health.checked_at}`;
  $('components').innerHTML = state.components.map(c => `<article class="card"><span class="badge ${esc(c.status)}">${esc(c.status)}</span><h3>${esc(c.label)}</h3><p class="muted">${esc(c.kind)} · version ${esc(c.version || 'unknown')}</p><p class="muted">${esc(c.detail || '')}</p>${c.update_supported ? `<button class="update-one" data-id="${esc(c.id)}" ${['operator','admin'].includes(state.role) ? '' : 'disabled'}>Update</button>` : ''}</article>`).join('');
  document.querySelectorAll('.update-one').forEach(button => button.onclick = () => beginUpdate([button.dataset.id]).catch(showError));
  $('schedule').textContent = `Daily at ${schedule.schedule.time} ${schedule.schedule.timezone} · persistent ${schedule.schedule.persistent}`;
  $('policy').textContent = schedule.policy_valid ? '已啟用每日更新，持續執行直到停用。' : schedule.policy_reason;
  $('runs').innerHTML = (runs.runs || []).map(run => `<p><code>${esc(run.id)}</code> · ${esc(run.trigger)} · <strong>${esc(run.status)}</strong> · ${esc(run.created_at)}</p>`).join('') || 'No runs yet';
  $('update-all').disabled = !['operator','admin'].includes(state.role) || !state.components.some(c => c.update_supported);
  $('authorize-policy').disabled = state.role !== 'admin' || !state.components.some(c => c.update_supported);
  $('disable-policy').disabled = state.role !== 'admin' || !schedule.policy_valid;
}
function canPublish() { return ['operator','admin'].includes(state.role); }
function updatePublishButton() {
  $('artifact-publish').disabled = !canPublish() || !state.publisherReady || state.publishing || !state.uploadFiles.length || !$('artifact-entrypoint').value;
}
function renderArtifacts(data) {
  state.artifacts = data.artifacts || [];
  state.publisherReady = !!data.publisher_ready;
  $('artifact-panel').hidden = !data.enabled;
  $('artifact-form').hidden = !canPublish();
  $('artifact-status').textContent = data.error || (data.publisher_ready ? `${state.artifacts.length} 個作品` : 'publish blocked：網頁發布服務尚未就緒。');
  $('artifacts').innerHTML = state.artifacts.map(a => `<article class="card"><h3>${esc(a.name)}</h3><p>${a.file_count} 個檔案 · ${(a.size_bytes / 1024).toFixed(1)} KiB</p><div class="panel-actions">${(a.pages || []).map(p => `<a href="${esc(p.access_url)}" target="_blank" rel="noopener noreferrer">${esc(p.entrypoint)}</a>`).join(' ')}${canPublish() ? `<button type="button" class="artifact-delete" data-name="${esc(a.name)}">刪除</button>` : ''}</div></article>`).join('') || '<p>尚未發布作品。</p>';
  document.querySelectorAll('.artifact-delete').forEach(button => button.onclick = () => deleteArtifact(button.dataset.name).catch(showError));
  updatePublishButton();
}
function selectArtifactFiles(input, folder) {
  const files = Array.from(input.files || []);
  $(folder ? 'artifact-files' : 'artifact-folder').value = '';
  state.uploadFiles = files.map(file => ({file, path:folder ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name}));
  const size = files.reduce((total, file) => total + file.size, 0);
  if (files.length > 200 || size > 10 * 1024 * 1024) {
    state.uploadFiles = [];
    $('artifact-status').textContent = '檔案超過限制：最多 200 個檔案、合計 10 MiB。';
  }
  const pages = state.uploadFiles.filter(item => /[.]html?$/i.test(item.path));
  pages.sort((a,b) => (a.path !== 'index.html') - (b.path !== 'index.html') || a.path.localeCompare(b.path));
  $('artifact-entrypoint').innerHTML = pages.map(p => `<option value="${esc(p.path)}">${esc(p.path)}</option>`).join('');
  $('artifact-selection').textContent = `${state.uploadFiles.length} 個檔案 · ${(size / 1024).toFixed(1)} KiB` + (pages.length ? '' : ' · 請選擇至少一個 HTML 檔案');
  updatePublishButton();
}
async function deleteArtifact(name) {
  if (!canPublish() || !window.confirm(`刪除「${name}」？作品連結將失效，且無法復原。`)) return;
  const token = state.session;
  await json('/api/v1/artifacts/' + encodeURIComponent(name), {method:'DELETE'});
  if (token === state.session) await refresh();
}
async function publishArtifact(event) {
  event.preventDefault();
  if (!canPublish() || state.publishing || !state.uploadFiles.length) return;
  const name = $('artifact-name').value;
  const entrypoint = $('artifact-entrypoint').value;
  const overwrite = state.artifacts.some(a => a.name === name);
  if (overwrite && !window.confirm(`取代「${name}」的全部檔案？舊內容將被覆蓋。`)) return;
  const token = state.session;
  state.publishing = true;
  updatePublishButton();
  $('artifact-status').textContent = '正在發布…';
  try {
    const files = [];
    for (const item of state.uploadFiles) {
      const bytes = new Uint8Array(await item.file.arrayBuffer());
      let binary = '';
      for (let i=0; i<bytes.length; i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
      files.push({path:item.path, content_base64:btoa(binary)});
    }
    if (token !== state.session) return;
    const receipt = await json('/api/v1/artifacts', {method:'POST', body:JSON.stringify({name, entrypoint, files, overwrite})});
    if (token !== state.session) return;
    await refresh();
    if (token === state.session) $('artifact-status').textContent = `已發布 ${receipt.name}，可從下方連結開啟。`;
  } catch (error) {
    if (token === state.session) $('artifact-status').textContent = error.message;
  } finally {
    state.publishing = false;
    updatePublishButton();
  }
}
$('artifact-form').onsubmit = publishArtifact;
$('artifact-files').onchange = event => selectArtifactFiles(event.target, false);
$('artifact-folder').onchange = event => selectArtifactFiles(event.target, true);
$('artifact-entrypoint').onchange = updatePublishButton;
function showError(error) {
  if (state.session) {
    $('global-error').textContent = error.message;
    $('global-error').hidden = false;
  } else {
    $('auth').textContent = error.message;
    $('auth').className = 'auth-note error';
  }
}
async function loadTrending() {
  const requestId = ++trendingRequestId;
  const source = $('trending-source').value;
  const since = $('trending-since').value;
  $('trending-refresh').disabled = true;
  $('trending-error').hidden = true;
  $('trending-status').textContent = '載入排行中…';
  try {
    const data = await json(`/api/v1/trending?source=${encodeURIComponent(source)}&since=${encodeURIComponent(since)}`);
    if (requestId !== trendingRequestId) return;
    const sourceLink = esc(String(data.sourceUrl || ''));
    const fetchedAt = esc(new Date(data.fetchedAt).toLocaleString('zh-TW', {hour12:false}));
    $('trending-meta').innerHTML = `資料即時抓自 <a class="source-link" href="${sourceLink}" target="_blank" rel="noreferrer">${esc(String(data.sourceUrl || '').replace(/^https?:\\/\\//, ''))}</a> · 抓取於 ${fetchedAt}`;
    const items = Array.isArray(data.items) ? data.items : [];
    $('trending-items').innerHTML = items.map((item, index) => {
      const language = item.language ? `<span class="lang">${esc(item.language)}</span>` : '';
      const gained = Number(item.starsToday || 0);
      const gainLine = gained > 0 ? `<span>↗ ${gained.toLocaleString('en-US')} 本期新增</span>` : '';
      return `<li class="rank-item"><span class="rank-no">${index + 1}</span><div><div class="rank-head"><a class="repo" href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.fullName)}</a>${language}</div><div class="rank-meta"><span>★ ${Number(item.starsTotal || 0).toLocaleString('en-US')}</span>${gainLine}</div>${item.description ? `<p class="muted rank-desc">${esc(item.description)}</p>` : ''}</div></li>`;
    }).join('');
    $('trending-status').textContent = items.length ? `共 ${items.length} 個專案` : '這個資料源目前沒有可顯示的項目。';
  } catch (error) {
    if (requestId !== trendingRequestId) return;
    $('trending-error').textContent = error instanceof Error ? error.message : String(error);
    $('trending-error').hidden = false;
    $('trending-status').textContent = $('trending-items').children.length ? '更新失敗，保留上一份排行。' : '目前無法取得排行。';
  } finally {
    if (requestId === trendingRequestId) $('trending-refresh').disabled = false;
  }
}
async function restoreSession() {
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey)); } catch (_) {}
  if (!saved?.session || !(Date.parse(saved.expires_at) > Date.now())) { resetWallet(); return; }
  const generation = state.generation;
  state.session = saved.session;
  try {
    const session = await json('/api/v1/auth/session');
    if (state.generation !== generation) return;
    const provider = walletObject();
    if (provider) {
      attachProvider(provider);
      const accounts = await provider.request({method:'eth_accounts'});
      if (state.generation !== generation) return;
      if (accounts[0]?.toLowerCase() !== session.address) { walletChanged(); return; }
    }
    acceptSession({...session, session:saved.session});
    await refresh();
  } catch (error) {
    if (state.generation === generation) {
      // A temporary server/network failure does not invalidate the credential.
      // Keep it for retry without exposing cached environment information.
      $('refresh').hidden = false;
      showError(error);
    }
  }
}
$('wallet').onclick = () => connect().catch(showError);
document.querySelectorAll('.side-link').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('.side-link').forEach(item => item.removeAttribute('aria-current'));
  link.setAttribute('aria-current', 'page');
}));
renderAuthMode();
$('refresh').onclick = () => (state.role ? refresh() : restoreSession()).catch(showError);
$('logout').onclick = async () => {
  const token = state.session;
  try { await revoke(token); if (state.session === token) resetWallet(); } catch (error) { showError(error); }
};
$('update-all').onclick = () => beginUpdate(state.components.filter(c => c.update_supported).map(c => c.id)).catch(showError);
$('authorize-policy').onclick = () => setPolicy(true).catch(showError);
$('disable-policy').onclick = () => setPolicy(false).catch(showError);
$('trending-source').onchange = () => void loadTrending();
$('trending-since').onchange = () => void loadTrending();
$('trending-refresh').onclick = () => void loadTrending();
void loadTrending();
restoreSession();
</script></main></div></div></body></html>""".replace(
        "__REGISTRATION_REQUIRED__",
        str(not bool(getattr(config, "authorized_wallets", ()))).lower(),
    )


def _policy_public(policy: Mapping[str, Any] | None, config: EnvironmentConfig) -> dict[str, Any]:
    if not policy:
        return {"enrolled": False, "enabled": False, "valid": False, "reason": "daily updates are not enabled"}
    valid, reason = verify_policy(config, policy)
    return {
        "enrolled": True,
        "enabled": policy.get("enabled", True),
        "valid": valid,
        "reason": reason,
        "policy_id": policy.get("policy_id"),
        "vm_id": policy.get("vm_id"),
        "components": list(policy.get("components", [])),
        "target_versions": dict(policy.get("target_versions", {})),
        "release_channel": policy.get("release_channel", "stable"),
        "expires_at": policy.get("expires_at"),
        "signer": policy.get("signer"),
    }


def _run_public(run: Mapping[str, Any], config: EnvironmentConfig) -> dict[str, Any]:
    public_ids = {component.id for component in config.components if component.public}
    components = []
    for item in run.get("components", []):
        if item.get("id") not in public_ids:
            continue
        components.append({
            key: item[key]
            for key in ("id", "status", "action", "returncode", "version", "health", "rollback", "detail")
            if key in item
        })
    result = {
        key: run[key]
        for key in ("id", "trigger", "action", "status", "created_at", "started_at", "finished_at", "error", "publication", "target_versions")
        if key in run
    }
    result["component_ids"] = [item for item in run.get("component_ids", []) if item in public_ids]
    result["components"] = components
    return result


class EnvironmentHTTPServer:
    """HTTP control plane for one VM's agent environment."""

    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self.store = RunStore(config.state_root)
        self.coordinator = UpdateCoordinator(config, self.store)
        self.trending = TrendingService()
        self.registration_challenges: dict[str, dict[str, Any]] = {}
        self.auth_failures: dict[str, list[float]] = {}
        self.lock = threading.RLock()
        self.httpd: Any = None

    def _cleanup(self) -> None:
        now = utc_now()
        cutoff = time.monotonic() - 300
        with self.lock:
            self.registration_challenges = {
                key: value for key, value in self.registration_challenges.items()
                if parse_timestamp(value["expires_at"]) > now
            }
            self.auth_failures = {
                key: values for key, values in self.auth_failures.items()
                if any(value > cutoff for value in values)
            }

    def auth_allowed(self, address: str) -> bool:
        cutoff = time.monotonic() - 300
        with self.lock:
            values = [value for value in self.auth_failures.get(address, []) if value > cutoff]
            self.auth_failures[address] = values
            return len(values) < 10

    def note_auth_failure(self, address: str) -> None:
        with self.lock:
            values = self.auth_failures.setdefault(address, [])
            values.append(time.monotonic())

    def _authority(self, operation: str, **payload: Any) -> dict[str, Any]:
        helper = self.config.authorization_trigger
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise EnvironmentError("privileged authorization helper is unavailable")
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", str(helper)],
            input=json.dumps({**payload, "operation": operation}),
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise EnvironmentError("privileged authorization helper failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EnvironmentError("invalid authorization helper response") from exc
        if not isinstance(value, dict):
            raise EnvironmentError("invalid authorization helper response")
        if "error" in value:
            raise EnvironmentError(value["error"])
        return value

    @staticmethod
    def _token(handler: Any) -> str:
        authorization = handler.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise EnvironmentError("wallet session required")
        return authorization.removeprefix("Bearer ").strip()

    def session(self, handler: Any, minimum_role: str = "viewer") -> dict[str, Any]:
        value = self._authority("session", token=self._token(handler))
        rank = {"viewer": 0, "operator": 1, "admin": 2}
        if rank[value["role"]] < rank[minimum_role]:
            raise EnvironmentError(f"wallet role {minimum_role} required")
        return value

    def registration_status(self) -> dict[str, Any]:
        return {
            "required": not bool(self.config.authorized_wallets),
            "vm_id": self.config.vm_id,
            "role": "admin" if not self.config.authorized_wallets else None,
        }

    def create_registration_challenge(self, address: str) -> dict[str, Any]:
        self._cleanup()
        if self.config.authorized_wallets:
            raise EnvironmentError("registration is already complete")
        address = normalize_wallet_address(address)
        issued_at = iso_now()
        expires_at = (utc_now() + dt.timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        challenge_id = str(uuid.uuid4())
        message = registration_message(
            self.config.vm_id,
            address,
            secrets.token_urlsafe(24),
            issued_at,
            expires_at,
        )
        challenge = {
            "challenge_id": challenge_id,
            "address": address,
            "role": "admin",
            "message": message,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        with self.lock:
            if self.config.authorized_wallets:
                raise EnvironmentError("registration is already complete")
            self.registration_challenges[challenge_id] = challenge
            while len(self.registration_challenges) > 32:
                self.registration_challenges.pop(next(iter(self.registration_challenges)))
        return challenge

    def _trigger_registration(self, address: str, message: str, signature: str) -> dict[str, Any]:
        trigger = self.config.registration_trigger
        if not trigger.is_file() or not os.access(trigger, os.X_OK):
            raise EnvironmentError("wallet registration helper is unavailable")
        request = json.dumps(
            {"address": address, "message": message, "signature": signature},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", str(trigger)],
            input=request,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            detail = _redact((result.stderr or result.stdout).strip() or "registration helper failed")
            raise EnvironmentError("wallet registration failed: " + detail)
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EnvironmentError("wallet registration returned an invalid response") from exc
        if not isinstance(response, Mapping) or response.get("registered") is not True:
            raise EnvironmentError("wallet registration was not confirmed")
        return dict(response)

    def _reload_config(self) -> None:
        config = EnvironmentConfig.load(self.config.path)
        with self.lock:
            self.config = config
            self.coordinator = UpdateCoordinator(config, self.store)

    def register_wallet(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        address = normalize_wallet_address(payload.get("address"))
        challenge_id = payload.get("challenge_id")
        message = payload.get("message")
        signature = payload.get("signature")
        if not all(isinstance(item, str) for item in (address, challenge_id, message, signature)):
            raise EnvironmentError("address, challenge_id, message, and signature are required")
        self._cleanup()
        with self.lock:
            challenge = self.registration_challenges.get(challenge_id)
        if challenge is None or challenge["address"] != address or not secrets.compare_digest(challenge["message"], message):
            raise EnvironmentError("registration challenge is invalid")
        valid, reason = verify_registration_request(self.config, address, message, signature)
        if not valid:
            raise EnvironmentError(reason)
        self._trigger_registration(address, message, signature)
        self._reload_config()
        with self.lock:
            self.registration_challenges.pop(challenge_id, None)
        return {"registered": True, "address": address, "role": "admin", "login_required": True}

    def create_auth_challenge(self, address: str, origin: str | None = None) -> dict[str, Any]:
        return self._authority("challenge", address=address, origin=origin)

    def verify_auth(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._authority("login", **{key: payload.get(key) for key in
                                         ("challenge_id", "address", "message", "signature")})

    def _trigger_manual(self, run_id: str) -> None:
        if not self.config.manual_trigger.is_file() or not os.access(self.config.manual_trigger, os.X_OK):
            raise EnvironmentError("privileged updater trigger is unavailable")
        subprocess.Popen(
            ["/usr/bin/sudo", "-n", str(self.config.manual_trigger), run_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def start_update(self, handler: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        run = self._authority("authorize_run", token=self._token(handler),
                              action=payload.get("action", "update"),
                              component_ids=payload.get("component_ids"),
                              target_versions=payload.get("target_versions", {}))
        try:
            self._trigger_manual(run["id"])
        except (EnvironmentError, OSError, subprocess.SubprocessError) as exc:
            run = self.store.update(run["id"], status="failed", finished_at=iso_now(), error=_redact(str(exc)))
            raise EnvironmentError(f"update run was queued but could not start: {run['error']}") from exc
        return run

    def save_policy(self, handler: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = {key: payload[key] for key in
                  ("enabled", "components", "target_versions", "allow_restart", "release_channel", "expires_at")
                  if key in payload}
        return self._authority("save_policy", token=self._token(handler), **fields)

    def handle(self, handler: Any) -> tuple[int, Any]:
        parsed = urllib.parse.urlparse(handler.path)
        path = parsed.path
        method = handler.command
        public_routes = {
            ("GET", "/"),
            ("GET", "/api/v1/trending"),
            ("GET", "/api/v1/auth/challenge"),
            ("POST", "/api/v1/auth/verify"),
            ("GET", "/api/v1/registration/challenge"),
            ("POST", "/api/v1/registration"),
        }
        if (method, path) not in public_routes:
            self.session(handler)
        if method == "POST" and path == "/api/v1/auth/logout":
            return 200, self._authority("logout", token=self._token(handler))
        if method == "GET" and path == "/api/v1/auth/session":
            return 200, self.session(handler)
        if method == "GET" and path == "/":
            return 200, ("text/html; charset=utf-8", dashboard_html(self.config))
        if method == "GET" and path == "/api/v1/trending":
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"source", "since"} or any(len(values) != 1 for values in query.values()):
                return 400, {"error": "only one source and one since value are allowed"}
            source = query.get("source", ["github"])[0]
            since = query.get("since", ["daily"])[0]
            try:
                return 200, self.trending.get(source, since)
            except TrendingRequestError as exc:
                return 400, {"error": str(exc)}
            except TrendingFetchError as exc:
                return 502, {"error": str(exc)}
        if method == "GET" and path == "/healthz":
            return 200, {
                "status": "ok",
                "service": "agent-environment",
                "vm_id": self.config.vm_id,
                "registration_required": not bool(self.config.authorized_wallets),
            }
        if method == "GET" and path == "/api/v1/health":
            return 200, environment_health(self.config)
        if method == "GET" and path == "/api/v1/components":
            return 200, {"vm_id": self.config.vm_id, "components": environment_health(self.config)["components"]}
        if path == "/api/v1/artifacts" or path.startswith("/api/v1/artifacts/"):
            try:
                if method == "GET" and path == "/api/v1/artifacts":
                    return 200, environment_artifacts.listing(self.config.artifacts)
                if method == "POST" and path == "/api/v1/artifacts":
                    self.session(handler, "operator")
                    return 201, environment_artifacts.upload(self.config.artifacts, handler.json_payload())
                if method == "DELETE" and path.startswith("/api/v1/artifacts/"):
                    self.session(handler, "operator")
                    return 200, environment_artifacts.remove(self.config.artifacts, path.removeprefix("/api/v1/artifacts/"))
            except core.Error as exc:
                raise EnvironmentError(str(exc)) from exc
            except OSError as exc:
                raise EnvironmentError("web artifact storage is unavailable") from exc
        if method == "GET" and path == "/api/v1/runs":
            return 200, {"runs": [_run_public(run, self.config) for run in self.store.list()]}
        if method == "GET" and path == "/api/v1/schedule":
            policy = read_environment_policy(self.config)
            public = self.config.public_config()
            valid, reason = verify_policy(self.config, policy)
            public["policy_valid"] = valid
            public["policy_reason"] = reason
            return 200, public
        if method == "GET" and path == "/api/v1/policy":
            return 200, _policy_public(read_environment_policy(self.config), self.config)
        if method == "GET" and path == "/api/v1/registration/status":
            return 200, self.registration_status()
        if method == "GET" and path == "/api/v1/registration/challenge":
            query = urllib.parse.parse_qs(parsed.query)
            return 200, self.create_registration_challenge(query.get("address", [""])[0])
        if method == "POST" and path == "/api/v1/registration":
            return 200, self.register_wallet(handler.json_payload())
        if method == "GET" and path == "/api/v1/auth/challenge":
            query = urllib.parse.parse_qs(parsed.query)
            return 200, self.create_auth_challenge(query.get("address", [""])[0], query.get("origin", [None])[0])
        if method == "POST" and path == "/api/v1/auth/verify":
            return 200, self.verify_auth(handler.json_payload())
        if method == "POST" and path == "/api/v1/update-runs":
            return 202, self.start_update(handler, handler.json_payload())
        if method == "PUT" and path == "/api/v1/policy":
            return 200, self.save_policy(handler, handler.json_payload())
        raise EnvironmentError("route not found")

    def serve_forever(self) -> None:
        import ssl
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = ""
            sys_version = ""

            def json_payload(self) -> Mapping[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise EnvironmentError("invalid content length") from exc
                limit = environment_artifacts.MAX_REQUEST_BYTES if self.command == "POST" and self.path == "/api/v1/artifacts" else 64 * 1024
                if length <= 0 or length > limit:
                    raise EnvironmentError("JSON request body is too large or empty")
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EnvironmentError("request body must be valid JSON") from exc
                if not isinstance(value, Mapping):
                    raise EnvironmentError("request body must be a JSON object")
                return value

            def origin_allowed(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return origin in app.config.allowed_origins or origin == app.config.public_origin or "*" in app.config.allowed_origins

            def send_value(self, status: int, value: Any) -> None:
                # Rejected uploads may leave unread bytes; never reuse that connection.
                self.close_connection = True
                if value is None:
                    body = b""
                    content_type = "text/plain; charset=utf-8"
                elif isinstance(value, tuple):
                    content_type, text = value
                    body = text.encode("utf-8")
                else:
                    content_type = "application/json; charset=utf-8"
                    body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
                self.send_response_only(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                origin = self.headers.get("Origin")
                if origin and self.origin_allowed():
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def dispatch(self) -> None:
                if not self.origin_allowed():
                    self.send_value(403, {"error": "origin is not allowed"})
                    return
                if self.path.split("?", 1)[0] == "/api/v1/auth/verify" and not app.auth_allowed(self.client_address[0]):
                    self.send_value(429, {"error": "too many authentication failures"})
                    return
                try:
                    status, value = app.handle(self)
                    self.send_value(status, value)
                except EnvironmentError as exc:
                    if self.path.split("?", 1)[0] == "/api/v1/auth/verify":
                        app.note_auth_failure(self.client_address[0])
                    message = str(exc)
                    status = 404 if message == "route not found" else 400
                    if "registration is already complete" in message:
                        status = 409
                    if "role" in message or "session" in message or "enrolled" in message:
                        status = 401 if "session" in message or "enrolled" in message else 403
                    self.send_value(status, {"error": message})
                except Exception:  # pragma: no cover - last-resort boundary
                    self.send_value(500, {"error": "internal server error"})

            def do_GET(self) -> None:
                self.dispatch()

            def do_POST(self) -> None:
                self.dispatch()

            def do_PUT(self) -> None:
                self.dispatch()

            def do_DELETE(self) -> None:
                self.dispatch()

            def do_OPTIONS(self) -> None:
                if not self.origin_allowed():
                    self.send_value(403, {"error": "origin is not allowed"})
                    return
                self.send_response_only(204)
                self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
                self.send_header("Access-Control-Max-Age", "600")
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                print(f"[agent-environment] {self.address_string()} {format % args}", flush=True)

        httpd = ThreadingHTTPServer((self.config.listen_host, self.config.listen_port), Handler)
        httpd.daemon_threads = True
        if self.config.tls_cert and self.config.tls_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(self.config.tls_cert), str(self.config.tls_key))
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        self.httpd = httpd
        print(f"agent-environment listening on {self.config.public_origin}", flush=True)
        if not self.config.authorized_wallets:
            print(
                f"registration required: connect a MetaMask at {self.config.public_origin}/",
                flush=True,
            )
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
