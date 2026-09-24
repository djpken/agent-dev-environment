"""Protect the efficiency gate from missing usage, cache double counts and failed trials."""

import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, call, patch

spec = importlib.util.spec_from_file_location(
    "codex_efficiency", Path(__file__).resolve().parents[1] / "scripts/codex_efficiency.py")
efficiency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(efficiency)


class EfficiencyTests(unittest.TestCase):
    def run_runner(self, action=None, quality_action=None, pretrusted=False):
        """Exercise real orchestration; replace only Git/Codex and unrelated quality checks."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        repo.mkdir()
        home = root / "home"
        (home / ".codex").mkdir(parents=True)
        self.runner_output = root / "output"
        self.runner_config = home / ".codex/config.toml"
        self.runner_original_config = 'model = "example"\n\n[projects."/unrelated"]\ntrust_level = "trusted"\n\n'
        if pretrusted:
            name = str(self.runner_output / "1-baseline/workspace")
            self.runner_original_config += f'[projects.{json.dumps(name)}]\ntrust_level = "trusted"\n\n'
        self.runner_config.write_text(self.runner_original_config)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name in (*efficiency.OVERLAY, "protected.txt"):
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original\n")
                tar.add(target, arcname=name)

        def command_output(command, **kwargs):
            if command[:2] == ["git", "rev-parse"]:
                return "fixed-revision\n"
            if command[:2] == ["git", "archive"]:
                return archive.getvalue()
            if command == ["codex", "--version"]:
                return "codex-cli fixture\n"
            self.fail(f"unexpected external command: {command}")

        self.runner_calls = []
        def codex(command, workspace, timeout, stem, prompt):
            turn = int(stem.name.removeprefix("turn-"))
            self.runner_calls.append((workspace, turn))
            config = tomllib.loads(self.runner_config.read_text())
            if str(workspace) not in config.get("projects", {}):
                with self.runner_config.open("a") as handle:
                    handle.write(f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n\n')
            if turn > 1:
                (workspace / "_efficiency_task/events.py").write_text("# authorized edit\n")
            if action:
                action(workspace, turn)
            stem.with_suffix(".stdout").write_text(self.stream(
                {"input_tokens": turn * 100, "cached_input_tokens": 0, "output_tokens": turn * 10}))
            return 0, 1.0

        def check_code(workspace, log):
            if quality_action:
                quality_action(workspace)
            return True

        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(efficiency, "ROOT", repo))
                stack.enter_context(patch.object(efficiency.Path, "home", return_value=home))
                stack.enter_context(patch.object(efficiency.os, "environ",
                                                {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}))
                stack.enter_context(patch("sys.argv", ["codex_efficiency.py", "--output", str(self.runner_output),
                                                       "--model", "fixture", "--repeats", "1"]))
                stack.enter_context(patch.object(efficiency.subprocess, "check_output", side_effect=command_output))
                stack.enter_context(patch.object(efficiency, "run_process", side_effect=codex))
                stack.enter_context(patch.object(efficiency, "check_answer", return_value={"answer": True}))
                stack.enter_context(patch.object(efficiency, "check_code", side_effect=check_code))
                stack.enter_context(redirect_stdout(io.StringIO()))
                return efficiency.main()
        finally:
            os.umask(previous_umask)

    def test_runner_rejects_new_files_and_log_changes(self):
        for relative in ("unexpected.py", "_efficiency_task/suite.log"):
            with self.subTest(relative=relative):
                def mutate(workspace, turn):
                    if turn == 2:
                        (workspace / relative).write_text("unauthorized change\n")
                self.run_runner(mutate)
                report = json.loads((self.runner_output / "summary.json").read_text())
                self.assertTrue(all(not trial["quality_pass"] for trial in report["trials"]))
                self.assertTrue(all(not trial["scope_pass"] for trial in report["trials"]))

    def test_runner_checks_read_only_turn_before_later_edits(self):
        def mutate(workspace, turn):
            if turn == 1:
                (workspace / "_efficiency_task/events.py").write_text("# too early\n")
        self.run_runner(mutate)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(all(not trial["quality_pass"] for trial in report["trials"]))
        self.assertTrue(all(turn == 1 for _, turn in self.runner_calls))

    def test_runner_cleans_trust_after_keyboard_interrupt(self):
        def interrupt(workspace, turn):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_runner(interrupt)
        self.assertEqual(self.runner_config.read_text(), self.runner_original_config)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["trial_trust_entries_removed"], 1)
        self.assertIsNone(report["decision"]["eligible_variant"])

    def test_runner_rejects_deleted_files_symlinks_and_mode_changes(self):
        for action in ("delete", "symlink", "directory", "mode", "cache_symlink", "special"):
            with self.subTest(action=action):
                def mutate(workspace, turn):
                    if turn != 2:
                        return
                    target = workspace / "protected.txt"
                    if action == "delete":
                        target.unlink()
                    elif action == "symlink":
                        other = workspace.parent / "copy.txt"
                        other.write_bytes(target.read_bytes())
                        target.unlink()
                        target.symlink_to(other)
                    elif action == "directory":
                        (workspace / "unexpected-directory").mkdir()
                    elif action == "mode":
                        target.chmod(target.stat().st_mode ^ 0o100)
                    elif action == "cache_symlink":
                        (workspace / "_efficiency_task/__pycache__").symlink_to(workspace.parent, target_is_directory=True)
                    else:
                        os.mkfifo(workspace / "unexpected-pipe")
                self.run_runner(mutate)
                report = json.loads((self.runner_output / "summary.json").read_text())
                self.assertTrue(all(not t["scope_pass"] for t in report["trials"]))
                self.assertTrue(all(turn <= 2 for _, turn in self.runner_calls))

    def test_runner_allows_authorized_edits_and_only_expected_cache(self):
        def cache(workspace, turn):
            if turn > 1:
                directory = workspace / "_efficiency_task/__pycache__"
                directory.mkdir(exist_ok=True)
                (directory / "events.cpython-313.pyc").write_bytes(b"bytecode")
        self.assertEqual(self.run_runner(cache, pretrusted=True), 0)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(all(t["quality_pass"] for t in report["trials"]))
        self.assertTrue(all(len(t["scope_checks"]) == 4 for t in report["trials"]))
        self.assertEqual(report["trial_trust_entries_removed"], 3)
        self.assertEqual(self.runner_config.read_text(), self.runner_original_config)

        def other_cache_file(workspace, turn):
            if turn == 2:
                directory = workspace / "_efficiency_task/__pycache__"
                directory.mkdir()
                (directory / "unexpected.py").write_text("not bytecode")
        self.run_runner(other_cache_file)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(all(not t["scope_pass"] for t in report["trials"]))

    def test_runner_rechecks_scope_after_independent_validation(self):
        def mutate(workspace):
            (workspace / "unexpected.py").write_text("validation side effect")
        self.run_runner(quality_action=mutate)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(all(not t["quality_pass"] for t in report["trials"]))
        self.assertTrue(all(t["scope_checks"][-1]["stage"] == "quality" for t in report["trials"]))

    def test_runner_cleans_trust_after_unexpected_exception(self):
        def fail(workspace, turn):
            raise RuntimeError("unexpected runner failure")
        with self.assertRaisesRegex(RuntimeError, "unexpected runner failure"):
            self.run_runner(fail)
        self.assertEqual(self.runner_config.read_text(), self.runner_original_config)

    def test_interrupted_cleanup_preserves_concurrent_changes(self):
        def interrupt(workspace, turn):
            text = self.runner_config.read_text()
            marker = f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"'
            self.runner_config.write_text(text.replace(marker, marker.replace('"trusted"', '"untrusted"')))
            raise KeyboardInterrupt
        with self.assertRaisesRegex(ValueError, "trial trust entry changed"):
            self.run_runner(interrupt)
        report = json.loads((self.runner_output / "summary.json").read_text())
        self.assertTrue(report["interrupted"])
        self.assertIn("trial trust entry changed", report["trial_trust_cleanup_error"])
        config = tomllib.loads(self.runner_config.read_text())
        self.assertEqual(config["projects"][str(self.runner_output / "1-baseline/workspace")]["trust_level"], "untrusted")
        self.assertEqual(config["projects"]["/unrelated"]["trust_level"], "trusted")

    def test_interrupt_kills_an_unresponsive_child_before_cleanup(self):
        process = Mock(pid=123)
        process.communicate.side_effect = KeyboardInterrupt
        process.wait.side_effect = [efficiency.subprocess.TimeoutExpired("child", 5), 0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(efficiency.subprocess, "Popen", return_value=process), \
                    patch.object(efficiency.os, "killpg") as kill:
                with self.assertRaises(KeyboardInterrupt):
                    efficiency.run_process(["stub"], root, 30, root / "interrupted")
            self.assertEqual(kill.call_args_list, [call(123, efficiency.signal.SIGTERM),
                                                 call(123, efficiency.signal.SIGKILL)])
            self.assertEqual(process.wait.call_args_list, [call(timeout=5), call()])

    def stream(self, usage):
        return "\n".join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": "sample"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": usage}])

    def test_actual_usage_and_cached_subset(self):
        result = efficiency.events_result(self.stream(
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}))
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertEqual(result["thread"], "sample")
        self.assertEqual(result["answer"], "ok")

    def test_missing_or_invalid_usage_is_not_zero_cost(self):
        for usage in ({}, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
                      {"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0},
                      {"input_tokens": True, "cached_input_tokens": 0, "output_tokens": 1},
                      {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": -1}):
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                efficiency.events_result(self.stream(usage))

    def test_incomplete_and_error_streams_rejected(self):
        with self.assertRaises(ValueError):
            efficiency.events_result('{"type":"turn.started"}')
        with self.assertRaises(ValueError):
            efficiency.events_result(self.stream({"input_tokens": 1, "cached_input_tokens": 0,
                                                  "output_tokens": 1}) + '\n{"type":"turn.failed"}')

    def test_resume_uses_final_cumulative_usage_without_double_counting(self):
        turns = [{"usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}},
                 {"usage": {"input_tokens": 250, "cached_input_tokens": 150, "output_tokens": 25}}]
        self.assertEqual(efficiency.session_usage(turns), turns[-1]["usage"])
        with self.assertRaises(ValueError):
            efficiency.session_usage(list(reversed(turns)))
        with self.assertRaises(ValueError):
            efficiency.session_usage([])

    def trials(self):
        return [{"variant": v, "repeat": r, "quality_pass": True,
                 "metrics_complete": True, "seconds": 10,
                 "usage": {"input_tokens": 1000 if v == "baseline" else 700,
                           "cached_input_tokens": 500, "output_tokens": 100}}
                for v in efficiency.VARIANTS for r in range(3)]

    def test_missing_baseline_blocks_adoption(self):
        rows = [r for r in self.trials() if r["variant"] != "baseline"]
        self.assertIsNone(efficiency.decision(rows, 3)["eligible_variant"])

    def test_failed_quality_or_missing_metrics_blocks_candidate(self):
        for field in ("quality_pass", "metrics_complete"):
            rows = self.trials()
            for row in rows:
                if row["variant"] != "baseline":
                    row[field] = False
            self.assertIsNone(efficiency.decision(rows, 3)["eligible_variant"])

    def test_slow_or_low_savings_candidates_rejected(self):
        rows = self.trials()
        for row in rows:
            if row["variant"] == "context":
                row["seconds"] = 12
            elif row["variant"] in ("instructions", "combined"):
                row["usage"]["input_tokens"] = 950
        self.assertIsNone(efficiency.decision(rows, 3)["eligible_variant"])

    def test_cache_is_not_added_and_tie_uses_latency(self):
        rows = self.trials()
        for row in rows:
            if row["variant"] == "combined":
                row["seconds"] = 9
        result = efficiency.decision(rows, 3)
        self.assertEqual(result["variants"]["baseline"]["median_tokens"], 1100)
        self.assertEqual(result["eligible_variant"], "combined")
        self.assertFalse(result["personal_defaults_changed"])

    def test_three_distinct_repeats_required(self):
        self.assertIsNone(efficiency.decision(self.trials(), 1)["eligible_variant"])
        rows = self.trials()
        for row in rows:
            row["repeat"] = 0
        self.assertIsNone(efficiency.decision(rows, 3)["eligible_variant"])

    def test_timeout_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rc, seconds = efficiency.run_process(
                ["python3", "-c", "import time; time.sleep(30)"], root, 0.1, root / "timeout")
            self.assertEqual(rc, 124)
            self.assertLess(seconds, 5)

    def test_independent_code_checks_catch_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            efficiency.fixtures(root)
            # The seeded implementation drops the error counters and must fail.
            self.assertFalse(efficiency.check_code(root, root / "quality"))
            (root / "_efficiency_task/events.py").write_text(
                'def summarize(records):\n'
                '    return {"count": len(records),\n'
                '            "failed": sum(r.get("status") == "failed" for r in records),\n'
                '            "error": sum(r.get("status") == "error" for r in records)}\n')
            self.assertTrue(efficiency.check_code(root, root / "quality"))

    def test_trust_cleanup_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            workspace = root / "trials/one/workspace"
            before = '# keep comment\nmodel = "example"\n\n[projects."/unrelated"]\ntrust_level = "trusted"\n\n'
            config.write_text(before)
            fingerprint = efficiency.config_fingerprint(config, root / "trials")
            config.write_text(before + f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n\n')
            self.assertEqual(efficiency.config_fingerprint(config, root / "trials"), fingerprint)
            self.assertEqual(efficiency.remove_trial_trust(config, [workspace]), 1)
            self.assertEqual(config.read_text(), before)
            self.assertEqual(efficiency.remove_trial_trust(config, [workspace]), 0)

    def test_trust_cleanup_refuses_a_modified_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            workspace = root / "workspace"
            original = f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "untrusted"\n'
            config.write_text(original)
            with self.assertRaises(ValueError):
                efficiency.remove_trial_trust(config, [workspace])
            self.assertEqual(config.read_text(), original)


if __name__ == "__main__":
    unittest.main()
