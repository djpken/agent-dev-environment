import tempfile
import tomllib
import unittest
from pathlib import Path

from ade import core, hosts


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "ade"
        self.release = self.root / "releases" / "fixture"
        self.release.mkdir(parents=True)
        (self.root / "current").symlink_to("releases/fixture")
        pack = self.release / "workflow"
        core.write(pack / ".claude-plugin/plugin.json", {"skills": ["skills/engineering/example"]})
        for name in ("engineering/example", "base", "implement", "code-review", "wait-what"):
            target = pack / "skills" / name / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("test skill")
        exports = self.release / "exports"
        core.write(exports / "claude.mcp.json", {"mcpServers": {"ade-test": {"command": "test"}}})
        core.write(exports / "opencode.json", {"mcp": {"ade-test": {"type": "local", "command": ["test"]}}})
        (exports / "codex.toml").write_text('[mcp_servers.ade-test]\ncommand = "test"\n')
        self.workspace = Path(self.temp.name) / "workspace"

    def test_preview_does_not_modify_workspace(self):
        result = hosts.attach(self.root, self.workspace, "codex")
        self.assertFalse(result["applied"])
        self.assertFalse(self.workspace.exists())

    def test_three_hosts_attach_and_remain_idempotent(self):
        for host in hosts.HOSTS:
            with self.subTest(host=host):
                hosts.attach(self.root, self.workspace, host, True)
                result = hosts.attach(self.root, self.workspace, host, True)
                self.assertEqual(result["skills_to_link"], [])
        config = tomllib.loads((self.workspace / ".codex/config.toml").read_text())
        self.assertEqual(config["mcp_servers"]["ade-test"]["command"], "test")

    def test_unrelated_config_and_comments_survive(self):
        path = self.workspace / ".codex/config.toml"
        path.parent.mkdir(parents=True)
        path.write_text('# personal comment\nmodel = "my-model"\n[mcp_servers.personal]\ncommand = "own-tool"\n')
        hosts.attach(self.root, self.workspace, "codex", True)
        self.assertIn("# personal comment", path.read_text())
        content = tomllib.loads(path.read_text())
        self.assertEqual(content["model"], "my-model")
        self.assertIn("personal", content["mcp_servers"])

    def test_conflict_is_detected_before_writing_anything(self):
        core.write(self.workspace / ".mcp.json", {"mcpServers": {"ade-test": {"command": "other"}}})
        with self.assertRaisesRegex(core.Error, "conflicts"):
            hosts.attach(self.root, self.workspace, "claude", True)
        self.assertFalse((self.workspace / ".claude").exists())

    def test_existing_skill_is_not_replaced(self):
        skill = self.workspace / ".agents/skills/base"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("owned by user")
        with self.assertRaisesRegex(core.Error, "skill conflicts"):
            hosts.attach(self.root, self.workspace, "codex", True)
        self.assertEqual((skill / "SKILL.md").read_text(), "owned by user")
        self.assertFalse((self.workspace / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
