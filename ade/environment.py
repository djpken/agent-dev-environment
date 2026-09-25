"""Per-VM environment primitives used by ADE's CLI and privileged helpers.

Components are declared in a root-owned manifest and are executed as argv
arrays; update requests never carry shell commands.
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
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature

from . import core, environment_artifacts
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
    public_origin: str
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
        public_origin = raw.get("public_origin", "")
        if not isinstance(public_origin, str) or not public_origin.startswith(("http://", "https://")):
            raise EnvironmentError("public_origin must be an HTTP(S) origin")
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
            public_origin=public_origin.rstrip("/"),
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
    """Small file-backed store shared by the update worker and root helper."""

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
