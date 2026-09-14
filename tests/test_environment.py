import json
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ade.environment import (
    EnvironmentConfig,
    EnvironmentError,
    EnvironmentHTTPServer,
    RunStore,
    UpdateCoordinator,
    base58_decode,
    base58_encode,
    environment_health,
    policy_message,
    render_snapshot,
    verify_policy,
    verify_solana_signature,
)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

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

    def test_base58_round_trip_and_ed25519_signature(self):
        for value in (b"", b"hello", b"\x00", b"\x00\x00\x01"):
            self.assertEqual(base58_decode(base58_encode(value)), value)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        address = base58_encode(public)
        message = b"ADE-ENVIRONMENT-CONTROL-V1\n{}"
        signature = base58_encode(key.sign(message))
        self.assertTrue(verify_solana_signature(address, message, signature))
        self.assertFalse(verify_solana_signature(address, b"changed", signature))

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
        run = UpdateCoordinator(config, store).create_run("test", ["dependent"], "test")
        result = UpdateCoordinator(config, store).execute(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["id"] for item in result["components"]], ["base", "dependent"])

    def test_signed_policy_is_bound_to_vm_and_authorized_wallet(self):
        key = Ed25519PrivateKey.generate()
        address = base58_encode(key.public_key().public_bytes_raw())
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
        policy["signature"] = base58_encode(key.sign(policy_message(policy).encode()))
        self.assertEqual(verify_policy(config, policy), (True, "ok"))
        policy["vm_id"] = "other-vm"
        self.assertFalse(verify_policy(config, policy)[0])

    def test_wallet_session_and_control_request_are_bound_to_challenges(self):
        key = Ed25519PrivateKey.generate()
        address = base58_encode(key.public_key().public_bytes_raw())
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
                "signature": base58_encode(key.sign(auth_challenge["message"].encode())),
            }
        )

        class Handler:
            def __init__(self, token):
                self.headers = {"Authorization": "Bearer " + token}

        handler = Handler(auth["session"])
        control = server.create_control_challenge(
            handler, {"action": "update", "component_ids": ["codex"], "target_versions": {"codex": "0.154.0"}}
        )
        server._trigger_manual = lambda _run_id: None
        run = server.start_update(
            handler,
            {
                "challenge_id": control["challenge_id"],
                "message": control["message"],
                "signature": base58_encode(key.sign(control["message"].encode())),
            },
        )
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
        run = UpdateCoordinator(config, store).create_run(
            "test", ["codex"], "test", target_versions={"codex": "0.154.0"}
        )
        result = UpdateCoordinator(config, store).execute(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(arguments.read_text(encoding="utf-8"), "--release 0.154.0")


if __name__ == "__main__":
    unittest.main()
