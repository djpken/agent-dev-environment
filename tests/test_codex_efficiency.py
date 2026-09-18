"""Protect the efficiency gate from missing usage, cache double counts and failed trials."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "codex_efficiency", Path(__file__).resolve().parents[1] / "scripts/codex_efficiency.py")
efficiency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(efficiency)


class EfficiencyTests(unittest.TestCase):
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
