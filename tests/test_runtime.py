import argparse
import io
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ade import core, cli

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def test_checksum_is_actually_verified(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            response = io.BytesIO(b"corrupted artifact")
            response.url = "https://example.com/binary"
            with patch("urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(core.Error, "checksum mismatch"):
                    core.download(response.url, "0" * 64, Path(temp) / "binary")

    def test_health_rejects_wrong_version(self):
        p = core.read(ROOT / "ade.lock.json")["providers"][0]
        result = subprocess.CompletedProcess([], 0, "ocr version 1.11.40", "")
        with patch("subprocess.run", return_value=result):
            with self.assertRaisesRegex(core.Error, "version"):
                core.health(p, Path("/tmp/release"))

    def test_review_uses_resolved_range_and_explicit_endpoint(self):
        lock = core.read(ROOT / "ade.lock.json")
        config = core.compose(lock, {"grants": {"ocr": lock["providers"][0]["permissions"]},
                                    "llm_endpoint": "https://model.example/v1"}, {})
        args = argparse.Namespace(model="test-model", base="main", head="HEAD", workspace=Path("/tmp"),
                                  key_env="TEST_TOKEN", protocol="openai")
        resolved = [subprocess.CompletedProcess([], 0, "a" * 40, ""),
                    subprocess.CompletedProcess([], 0, "b" * 40, ""),
                    subprocess.CompletedProcess([], 0, "", "")]
        with patch("ade.core.health"), patch("subprocess.run", side_effect=resolved), \
             patch.dict("os.environ", {"TEST_TOKEN": "test-secret"}), \
             patch("subprocess.call", return_value=0) as call:
            self.assertEqual(cli.review(args, Path("/tmp/release"), config), 0)
        env = call.call_args.kwargs["env"]
        self.assertEqual(env["OCR_LLM_URL"], "https://model.example/v1")
        self.assertEqual(env["OCR_LLM_TOKEN"], "test-secret")
        self.assertNotIn("test-secret", call.call_args.args[0])
        self.assertIn("a" * 40, call.call_args.args[0])

    def test_shared_lifecycle_rejects_host_spawned(self):
        provider = core.read(ROOT / "ade.lock.json")["providers"][1]
        with self.assertRaisesRegex(core.Error, "not shared-local"):
            cli.supervise(provider, Path("/tmp"))

    def test_shared_endpoint_must_be_loopback(self):
        provider = core.read(ROOT / "ade.lock.json")["providers"][1]
        provider.update(lifecycle="shared-local", transport="http", url="http://0.0.0.0:1234/mcp")
        with self.assertRaises(core.Error):
            core.manifest(provider)

    def test_exported_stdio_runs_through_ade(self):
        lock = core.read(ROOT / "ade.lock.json")
        config = core.compose(lock, {"providers": {"headroom": True},
                                    "grants": {"headroom": ["local-state"]}}, {})
        result = core.exports(config, Path("/tmp/ade/releases/abc"))
        command = result["claude.mcp.json"]["mcpServers"]["ade-headroom"]
        self.assertIn("ade.cli", command["args"])
        self.assertIn("provider", command["args"])

    def test_shared_process_starts_and_is_reaped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "releases/test"
            release.mkdir(parents=True)
            (root / "current").symlink_to("releases/test")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            provider = {"id": "fixture", "version": "1.0.0", "capabilities": ["fixture"],
                        "lifecycle": "shared-local", "transport": "http",
                        "url": f"http://127.0.0.1:{port}/",
                        "command": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", temp],
                        "health": [sys.executable, "-c", "print('1.0.0')"],
                        "permissions": ["listen-loopback"]}
            core.manifest(provider)
            core.write(release / "config.json", {"providers": {"fixture": True}, "manifests": {"fixture": provider},
                       "grants": {"fixture": ["listen-loopback"]}, "llm_endpoint": None})
            child = subprocess.Popen([sys.executable, "-m", "ade.cli", "--root", temp, "provider", "fixture", "--shared"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                        break
                    except OSError:
                        self.assertIsNone(child.poll())
                        time.sleep(0.05)
                else:
                    self.fail("shared process did not start")
            finally:
                child.terminate()
                child.wait(timeout=10)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
