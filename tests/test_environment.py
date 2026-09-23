import datetime as dt
import json
import hashlib
import os
import sys
import tempfile
import unittest
import threading
import time
import urllib.request
import urllib.error
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct

from ade.environment_auth import EnvironmentAuthority

from ade.environment import (
    EnvironmentConfig,
    EnvironmentError,
    EnvironmentHTTPServer,
    RunStore,
    UpdateCoordinator,
    enroll_signed_wallet,
    environment_health,
    iso_now,
    policy_message,
    registration_message,
    render_snapshot,
    read_environment_policy,
    utc_now,
    verify_policy,
    verify_wallet_signature,
)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        def authority(server, operation, **payload):
            return EnvironmentAuthority(server.config).dispatch({**payload, "operation": operation})
        bridge = patch.object(EnvironmentHTTPServer, "_authority", authority)
        bridge.start()
        self.addCleanup(bridge.stop)

    def authenticate(self, config, key):
        server = EnvironmentHTTPServer(config)
        challenge = server.create_auth_challenge(key.address)
        auth = server.verify_auth({"address": key.address, "challenge_id": challenge["challenge_id"],
            "message": challenge["message"],
            "signature": "0x" + key.sign_message(encode_defunct(text=challenge["message"])).signature.hex()})
        handler = SimpleNamespace(headers={"Authorization": "Bearer " + auth["session"]})
        server._trigger_manual = lambda _: None
        return server, handler, auth

    def config(self, components, wallets=None, snapshot=None):
        path = self.root / "agent-environment.json"
        path.write_text(
            json.dumps(
                {
                    "vm_id": "vm-test",
                    "source_root": str(self.root),
                    "state_root": str(self.root / "state"),
                    "listen": {"host": "127.0.0.1", "port": 6790},
                    "public_origin": "https://vm-test.example:6790",
                    "tls": {},
                    "allowed_origins": [],
                    "wallets": {"authorized": wallets or []},
                    "schedule": {"time": "04:00", "timezone": "UTC+8"},
                    "snapshot": snapshot or {"enabled": False},
                    "components": components,
                }
            ),
            encoding="utf-8",
        )
        return EnvironmentConfig.load(path)

    def test_personal_sign_verifies_message_and_address(self):
        key = Account.create()
        message = b"ADE-ENVIRONMENT-CONTROL-V1\n{}"
        signature = "0x" + key.sign_message(encode_defunct(primitive=message)).signature.hex()
        self.assertTrue(verify_wallet_signature(key.address, message, signature))
        self.assertTrue(verify_wallet_signature(key.address.lower(), message, signature))
        self.assertFalse(verify_wallet_signature(key.address, b"changed", signature))
        self.assertFalse(verify_wallet_signature(Account.create().address, message, signature))
        for bad in ("", "0x", "0x" + "00" * 65, "not-hex", None):
            self.assertFalse(verify_wallet_signature(key.address, message, bad))

    def test_config_rejects_relative_commands_and_unknown_dependencies(self):
        with self.assertRaises(EnvironmentError):
            self.config(
                [
                    {
                        "id": "bad",
                        "health": {"type": "command", "command": ["echo", "ok"]},
                    }
                ]
            )
        with self.assertRaises(EnvironmentError):
            self.config(
                [
                    {
                        "id": "bad-dependency",
                        "dependencies": ["missing"],
                        "health": {"type": "systemd", "unit": "missing.service"},
                    }
                ]
            )

    def test_public_listener_requires_tls(self):
        path = self.root / "agent-environment.json"
        path.write_text(
            json.dumps(
                {
                    "vm_id": "vm-public",
                    "source_root": str(self.root),
                    "state_root": str(self.root / "state"),
                    "listen": {"host": "0.0.0.0", "port": 6790},
                    "public_origin": "http://vm-public.example:6790",
                    "components": [
                        {"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EnvironmentError, "TLS is required"):
            EnvironmentConfig.load(path)

    def test_http_opt_in_and_live_origin_checks(self):
        config = self.config([{"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}])
        raw = json.loads(config.path.read_text())
        key = Account.create()
        raw["wallets"] = {"authorized": [{"address": key.address, "role": "viewer"}]}
        raw.update(listen={"host": "0.0.0.0", "port": 6790},
                   public_origin="http://access.example:6790", allow_http=True,
                   allowed_origins=["http://100.64.0.1:6790"])
        config.path.write_text(json.dumps(raw))
        config = EnvironmentConfig.load(config.path)
        server = EnvironmentHTTPServer(replace(config, listen_host="127.0.0.1", listen_port=0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for _ in range(100):
            if getattr(server, "httpd", None):
                break
            time.sleep(0.01)
        self.assertTrue(getattr(server, "httpd", None))
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.httpd.shutdown)
        url = "http://127.0.0.1:" + str(server.httpd.server_port)
        protected = ["/healthz", "/api/v1/health", "/api/v1/components", "/api/v1/runs",
                     "/api/v1/schedule", "/api/v1/policy", "/api/v1/registration/status"]
        for path in protected:
            for headers in ({}, {"Authorization": "Bearer invalid"}):
                with self.subTest(path=path, headers=headers):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(url + path, headers=headers))
                    self.assertEqual(error.exception.code, 401)
                    body = error.exception.read().decode()
                    self.assertNotIn(config.vm_id, body)
                    self.assertNotIn("components", body)
                    self.assertIsNone(error.exception.headers.get("Server"))
        with urllib.request.urlopen(url + "/") as response:
            shell = response.read().decode()
            self.assertNotIn(config.vm_id, shell)
            self.assertNotIn(str(config.source_root), shell)
        with patch.object(server.trending, "get", return_value={"source": "trendshift", "items": []}) as get_trending:
            with urllib.request.urlopen(url + "/api/v1/trending?source=trendshift&since=weekly") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"source": "trendshift", "items": []})
            get_trending.assert_called_once_with("trendshift", "weekly")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + "/api/v1/trending?source=https%3A%2F%2Fexample.com")
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + "/api/v1/trending?source=github&source=trendshift")
        self.assertEqual(error.exception.code, 400)
        with urllib.request.urlopen(url + "/api/v1/auth/challenge?address=" + key.address) as response:
            challenge = json.load(response)
        self.assertNotIn(config.vm_id, json.dumps(challenge))
        payload = {"address": key.address, "challenge_id": challenge["challenge_id"],
                   "message": challenge["message"],
                   "signature": "0x" + key.sign_message(encode_defunct(text=challenge["message"])).signature.hex()}
        request = urllib.request.Request(url + "/api/v1/auth/verify", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            token = json.load(response)["session"]
        for path in protected:
            with urllib.request.urlopen(urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})) as response:
                self.assertEqual(response.status, 200)
        for origin in [config.public_origin, *config.allowed_origins]:
            request = urllib.request.Request(url + "/healthz", headers={"Origin": origin, "Authorization": "Bearer " + token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
        request = urllib.request.Request(url + "/healthz", headers={"Origin": "http://attacker.example"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)
        request = urllib.request.Request(url + "/api/v1/update-runs", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 401)
        with urllib.request.urlopen(urllib.request.Request(url + "/api/v1/auth/logout", data=b"{}",
                                    headers={"Authorization": "Bearer " + token})) as response:
            self.assertTrue(json.load(response)["signed_out"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url + "/api/v1/health", headers={"Authorization": "Bearer " + token}))
        self.assertEqual(error.exception.code, 401)
        raw["allow_http"] = "true"
        config.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(EnvironmentError, "allow_http must be a boolean"):
            EnvironmentConfig.load(config.path)

    def test_login_rejects_tampering_replay_expiry_and_unlisted_origin(self):
        key = Account.create()
        config = self.config([{"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}],
                             wallets=[{"address": key.address, "role": "admin"}])
        server = EnvironmentHTTPServer(replace(config, allowed_origins=("http://100.64.0.1:6790",)))
        with self.assertRaisesRegex(EnvironmentError, "origin is not allowed"):
            server.create_auth_challenge(key.address, "http://attacker.example")
        challenge = server.create_auth_challenge(key.address, "http://100.64.0.1:6790")
        self.assertIn('"origin":"http://100.64.0.1:6790"', challenge["message"])
        binding = hashlib.sha256(config.vm_id.encode()).hexdigest()
        self.assertIn(binding, challenge["message"])
        self.assertNotIn(config.vm_id, challenge["message"])
        payload = {"address": key.address, "challenge_id": challenge["challenge_id"],
                   "message": challenge["message"],
                   "signature": "0x" + key.sign_message(encode_defunct(text=challenge["message"])).signature.hex()}
        for altered in (challenge["message"] + "extra", challenge["message"].replace(binding, "different-binding")):
            with self.assertRaisesRegex(EnvironmentError, "authentication challenge is invalid"):
                server.verify_auth({**payload, "message": altered})
        self.assertEqual(server.verify_auth(payload)["role"], "admin")
        with self.assertRaisesRegex(EnvironmentError, "authentication challenge is invalid"):
            server.verify_auth(payload)
        challenge = server.create_auth_challenge(key.address)
        with patch("ade.environment.utc_now", return_value=utc_now() + dt.timedelta(minutes=11)):
            with self.assertRaisesRegex(EnvironmentError, "authentication challenge is invalid"):
                server.verify_auth({**payload, "challenge_id": challenge["challenge_id"], "message": challenge["message"]})

    def test_login_challenge_is_consumed_once_under_concurrency(self):
        key = Account.create()
        config = self.config([{"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}],
                             wallets=[{"address": key.address, "role": "admin"}])
        server = EnvironmentHTTPServer(config)
        challenge = server.create_auth_challenge(key.address)
        payload = {"address": key.address, "challenge_id": challenge["challenge_id"],
                   "message": challenge["message"],
                   "signature": "0x" + key.sign_message(encode_defunct(text=challenge["message"])).signature.hex()}
        barrier = threading.Barrier(2)
        def login():
            barrier.wait(timeout=3)
            try:
                return server.verify_auth(payload)
            except EnvironmentError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: login(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(EnvironmentAuthority(config)._load()["sessions"]), 1)

    def test_health_and_snapshot_are_sanitized(self):
        version = self.root / "VERSION"
        version.write_text("v1.2.3\n", encoding="utf-8")
        config = self.config(
            [
                {
                    "id": "test-service",
                    "label": "Test service",
                    "kind": "service",
                    "version_file": str(version),
                    "health": {"type": "command", "command": [sys.executable, "-c", "pass"]},
                }
            ]
        )
        health = environment_health(config)
        self.assertEqual(health["status"], "healthy")
        snapshot = render_snapshot(config, health)
        self.assertIn("Test service", snapshot)
        self.assertNotIn("/tmp", snapshot)

    def test_public_health_does_not_expose_probe_details(self):
        config = self.config(
            [
                {
                    "id": "private-service",
                    "health": {
                        "type": "command",
                        "command": [sys.executable, "-c", "import sys; sys.stderr.write('/private/path'); sys.exit(1)"],
                    },
                }
            ]
        )
        health = environment_health(config)
        self.assertEqual(health["components"][0]["status"], "unhealthy")
        self.assertEqual(health["components"][0]["detail"], "unhealthy")
        self.assertNotIn("/private/path", json.dumps(health))

    def test_first_wallet_registration_requires_signed_challenge(self):
        key = Account.create()
        address = key.address.lower()
        config = self.config(
            [{"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}]
        )
        server = EnvironmentHTTPServer(config)
        challenge = server.create_registration_challenge(address)
        request = {
            "challenge_id": challenge["challenge_id"],
            "address": address,
            "message": challenge["message"],
            "signature": "0x" + key.sign_message(encode_defunct(primitive=challenge["message"].encode("utf-8"))).signature.hex(),
        }
        with self.assertRaisesRegex(EnvironmentError, "registration signature is invalid"):
            server.register_wallet({**request, "signature": "0x" + "00" * 65})
        triggered = {}
        server._trigger_registration = lambda *args: triggered.update({"args": args}) or {"registered": True}
        server._reload_config = lambda: None
        result = server.register_wallet(request)
        self.assertEqual(result, {"registered": True, "address": address, "role": "admin", "login_required": True})
        self.assertEqual(triggered["args"], (address, challenge["message"], request["signature"]))

        with self.assertRaisesRegex(EnvironmentError, "registration challenge is invalid"):
            server.register_wallet(request)

    def test_signed_enrollment_writes_the_first_admin_wallet(self):
        key = Account.create()
        address = key.address.lower()
        config = self.config(
            [{"id": "service", "health": {"type": "command", "command": ["/bin/true"]}}]
        )
        issued_at = iso_now()
        expires_at = (utc_now() + dt.timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        message = registration_message(config.vm_id, address, "registration-test-nonce", issued_at, expires_at)
        signature = "0x" + key.sign_message(encode_defunct(primitive=message.encode("utf-8"))).signature.hex()
        with patch("ade.environment.os.geteuid", return_value=0):
            result = enroll_signed_wallet(config, address, message, signature)
        self.assertEqual(result["role"], "admin")
        enrolled = EnvironmentConfig.load(config.path)
        self.assertEqual(enrolled.authorized_wallets, ({"address": address, "role": "admin"},))

    def test_dependency_order_runs_dependencies_first(self):
        marker = self.root / "marker"
        script = self.root / "update.py"
        script.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('done')\n",
            encoding="utf-8",
        )
        config = self.config(
            [
                {
                    "id": "dependent",
                    "dependencies": ["base"],
                    "update_command": [sys.executable, str(script)],
                    "health": {"type": "command", "command": [sys.executable, "-c", "pass"]},
                },
                {
                    "id": "base",
                    "update_command": [sys.executable, str(script)],
                    "health": {"type": "command", "command": [sys.executable, "-c", "pass"]},
                },
            ]
        )
        store = RunStore(config.state_root)
        key = Account.create()
        config = replace(config, authorized_wallets=({"address": key.address.lower(), "role": "operator"},))
        server, handler, _ = self.authenticate(config, key)
        run = server.start_update(handler, {"component_ids": ["dependent"]})
        result = UpdateCoordinator(config, store).execute(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["id"] for item in result["components"]], ["base", "dependent"])

    def test_signed_policy_is_bound_to_vm_and_authorized_wallet(self):
        key = Account.create()
        address = key.address.lower()
        config = self.config(
            [
                {
                    "id": "codex",
                    "update_command": [sys.executable, "-c", "pass"],
                    "health": {"type": "command", "command": [sys.executable, "-c", "pass"]},
                }
            ],
            wallets=[{"address": address, "role": "admin"}],
        )
        policy = {
            "policy_id": "policy-test",
            "vm_id": config.vm_id,
            "components": ["codex"],
            "allow_restart": True,
            "release_channel": "stable",
            "expires_at": "2999-01-01T00:00:00Z",
        }
        policy["signer"] = address
        policy["signature"] = "0x" + key.sign_message(encode_defunct(primitive=policy_message(policy).encode())).signature.hex()
        self.assertEqual(verify_policy(config, policy), (True, "ok"))
        policy["vm_id"] = "other-vm"
        self.assertFalse(verify_policy(config, policy)[0])

    def test_wallet_session_authorizes_updates_without_another_signature(self):
        key = Account.create()
        address = key.address.lower()
        config = self.config(
            [
                {
                    "id": "codex",
                    "update_command": ["/bin/true"],
                    "target_version_arg": "--release",
                    "health": {"type": "command", "command": ["/bin/true"]},
                }
            ],
            wallets=[{"address": address, "role": "operator"}],
        )
        server = EnvironmentHTTPServer(config)
        auth_challenge = server.create_auth_challenge(address)
        auth = server.verify_auth(
            {
                "challenge_id": auth_challenge["challenge_id"],
                "address": address,
                "message": auth_challenge["message"],
                "signature": "0x" + key.sign_message(encode_defunct(primitive=auth_challenge["message"].encode())).signature.hex(),
            }
        )

        class Handler:
            def __init__(self, token):
                self.headers = {"Authorization": "Bearer " + token}

        handler = Handler(auth["session"])
        server._trigger_manual = lambda _run_id: None
        run = server.start_update(handler, {
            "action": "update", "component_ids": ["codex"], "target_versions": {"codex": "0.154.0"}
        })
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["target_versions"], {"codex": "0.154.0"})
        result = UpdateCoordinator(config, RunStore(config.state_root)).execute(run["id"])
        self.assertEqual(result["status"], "completed")

    def test_signed_target_version_is_passed_to_allowlisted_command(self):
        arguments = self.root / "arguments"
        script = self.root / "update.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"Path({str(arguments)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )
        config = self.config(
            [
                {
                    "id": "codex",
                    "update_command": [sys.executable, str(script)],
                    "target_version_arg": "--release",
                    "health": {"type": "command", "command": [sys.executable, "-c", "pass"]},
                }
            ]
        )
        store = RunStore(config.state_root)
        key = Account.create()
        config = replace(config, authorized_wallets=({"address": key.address.lower(), "role": "operator"},))
        server, handler, _ = self.authenticate(config, key)
        run = server.start_update(handler, {"component_ids": ["codex"], "target_versions": {"codex": "0.154.0"}})
        result = UpdateCoordinator(config, store).execute(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(arguments.read_text(encoding="utf-8"), "--release 0.154.0")

    def session_config(self, key, role="admin"):
        return self.config([{
            "id": "codex", "update_command": ["/bin/true"],
            "restart_command": ["/bin/true"], "target_version_arg": "--release",
            "health": {"type": "command", "command": ["/bin/true"]},
        }], wallets=[{"address": key.address, "role": role}])

    def test_all_controls_use_one_login_and_tokens_are_not_persisted(self):
        key = Account.create()
        config = self.session_config(key)
        server, handler, auth = self.authenticate(config, key)
        # Any further wallet verification would violate the session contract.
        with patch("ade.environment.verify_wallet_signature", side_effect=AssertionError("unexpected wallet signature")):
            for action in ("update", "restart"):
                run = server.start_update(handler, {"action": action, "component_ids": ["codex"]})
                self.assertEqual(UpdateCoordinator(config, server.store).execute(run["id"])["status"], "completed")
            policy = server.save_policy(handler, {"components": ["codex"]})
            self.assertTrue(policy["valid"])
            self.assertIsNone(policy["expires_at"])
            self.assertTrue(server.session(handler))
        authority = EnvironmentAuthority(config)
        self.assertNotIn(auth["session"], authority.state_path.read_text())
        self.assertEqual(authority.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(EnvironmentHTTPServer(config).session(handler)["address"], key.address.lower())

    def test_roles_cannot_be_elevated_by_request_payload(self):
        key = Account.create()
        config = self.session_config(key, "viewer")
        server, handler, auth = self.authenticate(config, key)
        with self.assertRaisesRegex(EnvironmentError, "role operator required"):
            server.start_update(handler, {"component_ids": ["codex"], "role": "admin"})
        with self.assertRaisesRegex(EnvironmentError, "role admin required"):
            server.save_policy(handler, {"components": ["codex"], "role": "admin"})
        elevated = replace(config, authorized_wallets=({"address": key.address.lower(), "role": "admin"},))
        authority = EnvironmentAuthority(elevated)
        self.assertEqual(authority.dispatch({"operation": "session", "token": auth["session"]})["role"], "viewer")
        server.config = replace(config, authorized_wallets=())
        with self.assertRaisesRegex(EnvironmentError, "not enrolled"):
            server.session(handler)

    def test_logout_and_expiry_revoke_pending_manual_approvals(self):
        key = Account.create()
        config = self.session_config(key)
        for reason in ("logout", "expiry", "downgrade"):
            with self.subTest(reason=reason):
                server, handler, auth = self.authenticate(config, key)
                run = server.start_update(handler, {"component_ids": ["codex"]})
                authority = EnvironmentAuthority(config)
                if reason == "logout":
                    authority.dispatch({"operation": "logout", "token": auth["session"]})
                    self.assertFalse(authority.consume_run(run)[0])
                    with self.assertRaisesRegex(EnvironmentError, "session expired"):
                        server.session(handler)
                elif reason == "expiry":
                    with patch("ade.environment.utc_now", return_value=utc_now() + dt.timedelta(hours=13)):
                        self.assertFalse(authority.consume_run(run)[0])
                        with self.assertRaisesRegex(EnvironmentError, "session expired"):
                            server.save_policy(handler, {"components": ["codex"]})
                else:
                    downgraded = replace(config, authorized_wallets=({"address": key.address.lower(), "role": "viewer"},))
                    self.assertFalse(EnvironmentAuthority(downgraded).consume_run(run)[0])

    def test_manual_approval_rejects_tampering_and_replay(self):
        key = Account.create()
        config = self.session_config(key)
        server, handler, _ = self.authenticate(config, key)
        run = server.start_update(handler, {"component_ids": ["codex"]})
        authority = EnvironmentAuthority(config)
        changes = {"id": "00000000-0000-0000-0000-000000000000", "trigger": "scheduled",
                   "action": "restart", "component_ids": [], "target_versions": {"codex": "9.9.9"},
                   "requested_by": "other", "status": "completed"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(authority.consume_run({**run, field: value})[0])
        self.assertEqual(authority.consume_run(run), (True, "ok"))
        self.assertFalse(authority.consume_run(run)[0])

    def test_persistent_schedule_survives_logout_expiry_restart_and_identity_changes(self):
        key = Account.create()
        config = self.session_config(key)
        server, handler, auth = self.authenticate(config, key)
        server.save_policy(handler, {"components": ["codex"], "target_versions": {"codex": "1.2.3"}})
        EnvironmentAuthority(config).dispatch({"operation": "logout", "token": auth["session"]})
        config = replace(config, authorized_wallets=())
        with patch("ade.environment.utc_now", return_value=utc_now() + dt.timedelta(days=3650)):
            policy = read_environment_policy(config)
            self.assertNotIn("expires_at", policy)
            self.assertTrue(verify_policy(config, policy)[0])
            coordinator = UpdateCoordinator(config, server.store)
            run = coordinator.create_run("scheduled")
            self.assertEqual(coordinator.execute(run["id"], expected_trigger="scheduled")["status"], "completed")

    def test_disable_replaces_schedule_and_shared_state_cannot_reenable_it(self):
        key = Account.create()
        config = self.session_config(key)
        server, handler, _ = self.authenticate(config, key)
        server.save_policy(handler, {"components": ["codex"]})
        original = read_environment_policy(config)
        run = UpdateCoordinator(config, server.store).create_run("scheduled")
        server.save_policy(handler, {"enabled": False})
        server.store.write_policy(original)
        self.assertFalse(verify_policy(config, original)[0])
        self.assertEqual(UpdateCoordinator(config, server.store).execute(run["id"])["status"], "blocked")
        with self.assertRaisesRegex(EnvironmentError, "do not have an expiry"):
            server.save_policy(handler, {"components": ["codex"], "expires_at": "2999-01-01T00:00:00Z"})

    def test_forged_session_policy_in_shared_storage_is_rejected(self):
        key = Account.create()
        config = self.session_config(key)
        forged = {"authorization": "session", "enabled": True, "vm_id": config.vm_id,
                  "components": ["codex"], "target_versions": {}, "signer": key.address.lower()}
        RunStore(config.state_root).write_policy(forged)
        self.assertIsNone(read_environment_policy(config))
        self.assertFalse(verify_policy(config, forged)[0])

    def test_scheduled_runs_cannot_change_action_targets_or_privileged_trigger(self):
        key = Account.create()
        config = self.session_config(key)
        server, handler, _ = self.authenticate(config, key)
        server.save_policy(handler, {"components": ["codex"]})
        coordinator = UpdateCoordinator(config, server.store)
        for changes in ({"action": "restart"}, {"target_versions": {"codex": "9.9.9"}}):
            run = coordinator.create_run("scheduled")
            server.store.update(run["id"], **changes)
            self.assertEqual(coordinator.execute(run["id"])["status"], "blocked")
        run = coordinator.create_run("scheduled")
        with self.assertRaisesRegex(EnvironmentError, "trigger does not match"):
            coordinator.execute(run["id"], expected_trigger="manual")
        server.store.update(run["id"], trigger="test")
        with self.assertRaisesRegex(EnvironmentError, "unsupported update trigger"):
            coordinator.execute(run["id"])

    def test_authority_rejects_symlink_or_writable_storage(self):
        key = Account.create()
        config = self.session_config(key)
        authority = EnvironmentAuthority(config)
        authority.root.chmod(0o777)
        with self.assertRaisesRegex(EnvironmentError, "ownership or permissions"):
            EnvironmentAuthority(config)

        authority.root.chmod(0o755)
        authority.root.rmdir()
        target = self.root / "untrusted"
        target.mkdir()
        authority.root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(EnvironmentError, "ownership or permissions"):
            EnvironmentAuthority(config)

    def test_authority_policy_remains_readable_under_service_umask(self):
        key = Account.create()
        config = self.session_config(key)
        previous_umask = os.umask(0o077)
        try:
            server, handler, _ = self.authenticate(config, key)
            server.save_policy(handler, {"components": ["codex"]})
        finally:
            os.umask(previous_umask)
        authority = EnvironmentAuthority(config)
        self.assertEqual(authority.root.stat().st_mode & 0o777, 0o755)
        self.assertEqual(authority.policy_path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(authority.state_path.stat().st_mode & 0o777, 0o600)


    def test_live_api_accepts_unsigned_controls_with_a_session(self):
        key = Account.create()
        config = self.session_config(key)
        server = EnvironmentHTTPServer(replace(config, listen_host="127.0.0.1", listen_port=0))
        server._trigger_manual = lambda _: None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for _ in range(100):
            if server.httpd:
                break
            time.sleep(0.01)
        self.assertIsNotNone(server.httpd)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.httpd.shutdown)
        url = "http://127.0.0.1:" + str(server.httpd.server_port)
        token = None

        def request(path, method="GET", payload=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(url + path, method=method, headers=headers,
                data=json.dumps(payload).encode() if payload is not None else None)
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)

        _, challenge = request("/api/v1/auth/challenge?address=" + key.address)
        _, auth = request("/api/v1/auth/verify", "POST", {
            "address": key.address, "challenge_id": challenge["challenge_id"], "message": challenge["message"],
            "signature": "0x" + key.sign_message(encode_defunct(text=challenge["message"])).signature.hex()})
        token = auth["session"]
        with patch("ade.environment.verify_wallet_signature", side_effect=AssertionError("unexpected signature")):
            self.assertEqual(request("/api/v1/auth/session")[1]["address"], key.address.lower())
            self.assertEqual(request("/api/v1/update-runs", "POST", {"component_ids": ["codex"]})[0], 202)
            self.assertTrue(request("/api/v1/policy", "PUT", {"components": ["codex"]})[1]["valid"])
            self.assertTrue(request("/api/v1/schedule")[1]["policy_valid"])
            self.assertFalse(request("/api/v1/policy", "PUT", {"enabled": False})[1]["valid"])
            self.assertTrue(request("/api/v1/auth/logout", "POST", {})[1]["signed_out"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/v1/update-runs", "POST", {"component_ids": ["codex"]})
            self.assertEqual(error.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
