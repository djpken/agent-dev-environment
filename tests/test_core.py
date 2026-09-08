import copy
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from ade import core

REPO = Path(__file__).resolve().parents[1]


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.lock = core.read(REPO / "ade.lock.json")

    def test_workspace_cannot_grant_or_redirect(self):
        for field in ("grants", "llm_endpoint", "extensions", "command"):
            with self.subTest(field=field), self.assertRaises(core.Error):
                core.compose(self.lock, {}, {field: {}})

    def test_precedence_and_disabled_default(self):
        config = core.compose(self.lock, {"providers": {"ocr": False}}, {"providers": {"ocr": True}})
        self.assertTrue(config["providers"]["ocr"])
        self.assertFalse(config["providers"]["headroom"])
        with self.assertRaises(core.Error):
            core.authorize(config, "ocr")

    def test_endpoint_required_even_with_grants(self):
        config = core.compose(self.lock, {"grants": {"ocr": self.lock["providers"][0]["permissions"]}}, {})
        with self.assertRaisesRegex(core.Error, "llm_endpoint"):
            core.authorize(config, "ocr")

    def test_malformed_and_duplicate_manifests(self):
        for change in ({"lifecycle": "daemon"}, {"command": "sh -c x"}, {"surprise": True}, {"id": "../escape"}):
            value = dict(self.lock["providers"][0], **change)
            with self.subTest(change=change), self.assertRaises(core.Error):
                core.manifest(value)
        with self.assertRaisesRegex(core.Error, "duplicate"):
            core.compose(self.lock, {"extensions": [self.lock["providers"][0]]}, {})

    def test_prompt_traversal_and_endpoint_secrets(self):
        for value in ({"prompts": ["../secret.md"]}, {"llm_endpoint": "https://key:secret@example.com"}):
            with self.assertRaises(core.Error):
                core.compose(self.lock, value, {})

    def test_exports_all_three_hosts(self):
        config = core.compose(self.lock, {"providers": {"headroom": True},
                             "grants": {"headroom": ["local-state"]}}, {})
        out = core.exports(config, Path("/tmp/ade/release"))
        self.assertIn("ade-headroom", out["claude.mcp.json"]["mcpServers"])
        self.assertEqual(out["opencode.json"]["mcp"]["ade-headroom"]["type"], "local")
        self.assertIn("ade-headroom", tomllib.loads(out["codex.toml"])["mcp_servers"])

    def test_clean_env_does_not_forward_api_keys(self):
        with patch.dict("os.environ", {"SECRET_KEY": "never-log", "OPENAI_API_KEY": "never-log"}):
            env = core.clean_env(self.lock["providers"][0])
        self.assertNotIn("SECRET_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "pack"
        self.repo.mkdir()
        for command in (["init", "-q"], ["config", "user.email", "tests@example.invalid"],
                        ["config", "user.name", "Tests"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True)
        (self.repo / "prompt").mkdir()
        (self.repo / "prompt" / "sample.md").write_text("sample prompt")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.lock = core.read(REPO / "ade.lock.json")
        self.lock["workflow"]["revision"] = revision
        self.lock["workflow"]["source"] = "git"
        self.lock["defaults"]["providers"]["ocr"] = False
        self.config = core.compose(self.lock, {"prompts": ["sample.md"]}, {})
        self.root = self.base / "managed"

    def test_install_and_rollback_keep_previous_generation(self):
        first = core.install(self.lock, self.config, self.root, self.repo)
        self.assertEqual(core.generation(self.root) / "personal-prompt.md",
                         Path(first["generation"]) / "personal-prompt.md")
        self.assertEqual((core.generation(self.root) / "personal-prompt.md").read_text(), "sample prompt")
        second = core.install(self.lock, self.config, self.root, self.repo)
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(core.rollback(self.root)["generation"], first["generation"])
        self.assertTrue(Path(second["generation"]).exists())

    def test_stale_plan_does_not_change_current(self):
        plan = core.plan(self.lock, self.config, self.root)
        core.install(self.lock, self.config, self.root, self.repo, plan["plan_id"])
        current = core.generation(self.root)
        with self.assertRaisesRegex(core.Error, "stale plan"):
            core.install(self.lock, self.config, self.root, self.repo, plan["plan_id"])
        self.assertEqual(core.generation(self.root), current)

    def test_failed_artifact_preserves_active_installation(self):
        core.install(self.lock, self.config, self.root, self.repo)
        current = core.generation(self.root)
        enabled = copy.deepcopy(self.config)
        enabled["providers"]["ocr"] = True
        with patch("ade.core.download", side_effect=core.Error("artifact checksum mismatch")):
            with self.assertRaisesRegex(core.Error, "checksum"):
                core.install(self.lock, enabled, self.root, self.repo)
        self.assertEqual(core.generation(self.root), current)
        self.assertFalse(list((self.root / "releases").glob(".stage-*")))

    def test_invalid_revision_and_unknown_prompt_abort(self):
        bad = copy.deepcopy(self.lock)
        bad["workflow"]["revision"] = "HEAD"
        with self.assertRaises(core.Error):
            core.install(bad, self.config, self.root, self.repo)
        config = copy.deepcopy(self.config)
        config["prompts"] = ["unknown.md"]
        with self.assertRaisesRegex(core.Error, "unknown Workflow Pack"):
            core.install(self.lock, config, self.root, self.repo)
        self.assertIsNone(core.generation(self.root))

    def test_monorepo_bundle_and_stale_content(self):
        self.lock["workflow"]["source"] = "bundled"
        (self.repo / "skills/example").mkdir(parents=True)
        (self.repo / "skills/example/SKILL.md").write_text("first")
        core.write(self.repo / ".claude-plugin/plugin.json", {"name": "skills", "skills": []})
        (self.repo / "LICENSE").write_text("test license")
        (self.repo / "private.txt").write_text("must not ship")
        proposed = core.plan(self.lock, self.config, self.root, self.repo)
        (self.repo / "skills/example/SKILL.md").write_text("changed")
        with self.assertRaisesRegex(core.Error, "stale plan"):
            core.install(self.lock, self.config, self.root, self.repo, proposed["plan_id"])
        new = core.plan(self.lock, self.config, self.root, self.repo)
        core.install(self.lock, self.config, self.root, self.repo, new["plan_id"])
        pack = core.generation(self.root) / "workflow"
        self.assertEqual((pack / "skills/example/SKILL.md").read_text(), "changed")
        self.assertFalse((pack / "private.txt").exists())


if __name__ == "__main__":
    unittest.main()
