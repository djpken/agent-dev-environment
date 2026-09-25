"""Root-owned account authentication and operation approvals for ADES.

The management server calls a fixed privileged helper over stdin. Only token
hashes are persisted. Run history is untrusted input; an immutable, one-use
approval binds each manual execution to a live session. Policies have a
separate lifetime.
"""

from __future__ import annotations

import datetime as dt
import base64
import hashlib
import os
import re
import secrets
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import environment as env


SESSION_HOURS = 12
PASSWORD_SCRYPT_N = 1 << 15
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_HASH_BYTES = 32
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@+-]{0,127}$")
UNKNOWN_ACCOUNT_SALT = b"ade-unknown-account-v1"


class EnvironmentAuthority:
    def __init__(self, config: env.EnvironmentConfig):
        self.config = config
        # The config directory is root-owned in deployment, unlike state_root.
        self.root = config.path.parent / (config.path.stem + ".auth")
        self.owner = config.path.stat().st_uid
        parent = self.root.parent.stat()
        if parent.st_uid != self.owner or parent.st_mode & 0o022:
            raise env.EnvironmentError("authorization parent must be owned by the config owner and not writable by others")
        self.root.mkdir(mode=0o755, exist_ok=True)
        self._check_path(self.root, directory=True)
        # The service sets UMask=0077. Policy metadata is intentionally readable
        # by the HTTP user, while sessions.json remains owner-only.
        if os.geteuid() == self.owner:
            self.root.chmod(0o755)
        self.state_path = self.root / "sessions.json"
        self.lock_path = self.root / ".lock"
        self.policy_path = self.root / "policy.json"
        self.accounts_path = self.root / "accounts.json"

    def _check_path(self, path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(info.st_mode) or info.st_uid != self.owner or info.st_mode & 0o022:
            raise env.EnvironmentError("authorization storage ownership or permissions are invalid")

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            self._check_path(self.state_path)
            if self.state_path.stat().st_mode & 0o077:
                raise env.EnvironmentError("session storage must be private to its owner")
        state = env._read_json(self.state_path, {"vm_id": self.config.vm_id, "sessions": {}, "approvals": {}})
        if state.get("vm_id") != self.config.vm_id:
            raise env.EnvironmentError("session storage VM identity does not match")
        now = env.utc_now()
        for group in ("sessions", "approvals"):
            state[group] = {key: value for key, value in state[group].items()
                            if env.parse_timestamp(value["expires_at"]) > now}
        return state

    @staticmethod
    def token_id(token: Any) -> str:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise env.EnvironmentError("session expired")
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def normalize_username(value: Any) -> str:
        if not isinstance(value, str):
            raise env.EnvironmentError("invalid credentials")
        username = value.strip().lower()
        if not USERNAME_PATTERN.fullmatch(username):
            raise env.EnvironmentError("invalid credentials")
        return username

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode(), salt=salt, n=PASSWORD_SCRYPT_N,
                              r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P,
                              dklen=PASSWORD_HASH_BYTES, maxmem=64 * 1024 * 1024)

    def _load_accounts(self) -> dict[str, Any]:
        if not self.accounts_path.exists():
            return {}
        self._check_path(self.accounts_path)
        if self.accounts_path.stat().st_mode & 0o077:
            raise env.EnvironmentError("account storage must be private to its owner")
        stored = env._read_json(self.accounts_path, {})
        if stored.get("vm_id") != self.config.vm_id or not isinstance(stored.get("users"), dict):
            raise env.EnvironmentError("account storage is invalid")
        users = stored["users"]
        for username, account in users.items():
            if (not isinstance(account, dict) or account.get("username") != username
                    or not USERNAME_PATTERN.fullmatch(username) or account.get("role") not in env.ROLES
                    or not isinstance(account.get("credential_id"), str)
                    or account.get("hash_version") != 1):
                raise env.EnvironmentError("account storage is invalid")
            try:
                salt = base64.b64decode(account["salt"] + "===")
                password_hash = base64.b64decode(account["password_hash"] + "===")
            except (KeyError, ValueError, TypeError) as exc:
                raise env.EnvironmentError("account storage is invalid") from exc
            if len(salt) != 16 or len(password_hash) != PASSWORD_HASH_BYTES:
                raise env.EnvironmentError("account storage is invalid")
        return users

    def set_account(self, username: str, password: str, role: str = "admin") -> None:
        if os.geteuid() != self.owner:
            raise env.EnvironmentError("account setup requires the config owner")
        normalized = self.normalize_username(username)
        if role not in env.ROLES:
            raise env.EnvironmentError("role must be viewer, operator, or admin")
        if not isinstance(password, str) or not 12 <= len(password.encode()) <= 1024:
            raise env.EnvironmentError("password must be between 12 and 1024 bytes")
        with env._locked(self.lock_path):
            state = self._load()
            users = self._load_accounts()
            salt = secrets.token_bytes(16)
            users[normalized] = {
                "username": normalized,
                "role": role,
                "credential_id": str(uuid.uuid4()),
                "hash_version": 1,
                "salt": base64.b64encode(salt).decode().rstrip("="),
                "password_hash": base64.b64encode(self._password_hash(password, salt)).decode().rstrip("="),
            }
            env._write_json(self.accounts_path, {"vm_id": self.config.vm_id, "users": users}, 0o600)
            removed = set()
            for session_id, session in list(state["sessions"].items()):
                if session.get("username") == normalized:
                    del state["sessions"][session_id]
                    removed.add(session_id)
            state["approvals"] = {
                run_id: approval for run_id, approval in state["approvals"].items()
                if approval.get("session_id") not in removed
            }
            env._write_json(self.state_path, state, 0o600)

    def _session(self, state: Mapping[str, Any], session_id: str, role: str = "viewer") -> dict[str, Any]:
        session = state["sessions"].get(session_id)
        if not session or env.parse_timestamp(session["expires_at"]) <= env.utc_now():
            raise env.EnvironmentError("session expired")
        account = self._load_accounts().get(session["username"])
        if not account or session.get("account_version") != account["credential_id"]:
            raise env.EnvironmentError("session is no longer valid")
        rank = {"viewer": 0, "operator": 1, "admin": 2}
        current_role = account["role"]
        if rank[current_role] < rank[role]:
            raise env.EnvironmentError(f"account role {role} required")
        return {"username": session["username"], "role": current_role,
                "expires_at": session["expires_at"]}

    def _login(self, state: dict[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        username = self.normalize_username(request.get("username"))
        password = request.get("password")
        if not isinstance(password, str) or not password:
            raise env.EnvironmentError("invalid credentials")
        account = self._load_accounts().get(username)
        salt = (base64.b64decode(account["salt"] + "===") if account
                else UNKNOWN_ACCOUNT_SALT)
        actual = self._password_hash(password, salt)
        if not account or not secrets.compare_digest(
                actual, base64.b64decode(account["password_hash"] + "===")):
            raise env.EnvironmentError("invalid credentials")
        token = secrets.token_urlsafe(32)
        session = {"username": username, "role": account["role"],
                   "account_version": account["credential_id"],
                   "expires_at": (env.utc_now() + dt.timedelta(hours=SESSION_HOURS)).isoformat()}
        state["sessions"][self.token_id(token)] = session
        return {"session": token, "username": username, "role": account["role"],
                "expires_at": session["expires_at"]}

    def _selection(self, request: Mapping[str, Any], *, policy: bool = False) -> tuple[str, list[str], dict[str, str]]:
        action = "update" if policy else request.get("action", "update")
        if not isinstance(action, str) or action not in {"update", "restart"}:
            raise env.EnvironmentError("unsupported control action")
        ids = request.get("components" if policy else "component_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
            raise env.EnvironmentError("components must be a non-empty list of unique identifiers")
        for component in env._component_order(self.config, ids):
            if not component.enabled or not (component.update_command if action == "update" else component.restart_command):
                raise env.EnvironmentError(f"component does not support {action}: {component.id}")
        targets = env.validate_target_versions(self.config, ids, request.get("target_versions", {}))
        if action == "restart" and targets:
            raise env.EnvironmentError("restart does not accept target versions")
        return action, list(ids), targets

    @staticmethod
    def run_binding(run: Mapping[str, Any]) -> dict[str, Any]:
        return {key: run.get(key) for key in
                ("id", "trigger", "action", "component_ids", "target_versions", "requested_by")}

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise env.EnvironmentError("authorization request must be an object")
        # All mutation paths, including logout and approval consumption, share a
        # root-owned file lock across helper processes.
        with env._locked(self.lock_path):
            state = self._load()
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise env.EnvironmentError("authorization operation must be text")
            if operation == "login":
                result = self._login(state, request)
            else:
                session_id = self.token_id(request.get("token"))
                if operation == "logout":
                    state["sessions"].pop(session_id, None)
                    result = {"signed_out": True}
                else:
                    minimum_role = {"authorize_run": "operator", "save_policy": "admin"}.get(operation, "viewer")
                    session = self._session(state, session_id, minimum_role)
                    if operation == "session":
                        result = session
                    elif operation == "policy_status":
                        result = env._policy_public(self.read_policy(), self.config)
                    elif operation == "authorize_run":
                        action, ids, targets = self._selection(request)
                        store = env.RunStore(self.config.state_root)
                        run = env.UpdateCoordinator(self.config, store).create_run(
                            "manual", ids, session["username"], action, targets, {"kind": "session"})
                        state["approvals"][run["id"]] = {
                            "binding": self.run_binding(run), "session_id": session_id,
                            "expires_at": session["expires_at"],
                        }
                        result = run
                    elif operation == "save_policy":
                        enabled = request.get("enabled", True)
                        if not isinstance(enabled, bool):
                            raise env.EnvironmentError("policy enabled must be a boolean")
                        if request.get("expires_at") is not None:
                            raise env.EnvironmentError("scheduled policies do not have an expiry")
                        if enabled:
                            _, ids, targets = self._selection(request, policy=True)
                            if request.get("release_channel", "stable") != "stable":
                                raise env.EnvironmentError("only stable release channel is allowed")
                            if request.get("allow_restart", True) is not True:
                                raise env.EnvironmentError("component updates require restart permission")
                            policy = {"components": ids, "target_versions": targets,
                                      "release_channel": "stable", "allow_restart": True}
                        else:
                            policy = {"components": [], "target_versions": {},
                                      "release_channel": "stable", "allow_restart": True}
                        policy.update(policy_id=str(uuid.uuid4()), vm_id=self.config.vm_id,
                                      enabled=enabled, signer=session["username"],
                                      authorization="session", updated_at=env.iso_now())
                        env._write_json(self.policy_path, policy, 0o644)
                        result = env._policy_public(policy, self.config)
                    else:
                        raise env.EnvironmentError("unsupported authorization operation")
            env._write_json(self.state_path, state)
            return result

    def read_policy(self) -> dict[str, Any] | None:
        if self.policy_path.exists():
            self._check_path(self.policy_path)
            return env._read_json(self.policy_path, None)
        # An existing signed policy remains valid until explicitly replaced.
        policy = env.RunStore(self.config.state_root).read_policy()
        return policy if policy and policy.get("authorization") != "session" else None

    def consume_run(self, run: Mapping[str, Any]) -> tuple[bool, str]:
        with env._locked(self.lock_path):
            state = self._load()
            approval = state["approvals"].get(run.get("id"))
            if not approval:
                return False, "manual run approval is missing, expired, or already used"
            if approval["binding"] != self.run_binding(run) or run.get("status") != "queued":
                return False, "manual run does not match its session approval"
            try:
                self._session(state, approval["session_id"], "operator")
                self._selection(run)
            except env.EnvironmentError as exc:
                return False, str(exc)
            del state["approvals"][run["id"]]
            env._write_json(self.state_path, state)
            return True, "ok"
