"""Offline acceptance tests at the benchmark CLI / artifact boundary."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        for name in ('base', 'code-review'):
            dest = self.repo / 'skills' / name
            dest.mkdir(parents=True)
            (dest / 'SKILL.md').write_text((ROOT / 'skills' / name / 'SKILL.md').read_text())
        (self.repo / 'app.py').write_text('value = 1\n')
        self.finding = {'file': 'app.py', 'start_line': 1, 'end_line': 1,
                        'summary': 'Validate user input', 'description': ''}
        self.behavior = {'Standards': [], 'Spec': [], 'usage_mode': 'self'}
        self.write_behavior()
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.base = self.git('rev-parse', 'HEAD')
        (self.repo / 'app.py').write_text('value = input()\n')
        self.behavior['Standards'] = [self.finding]
        self.write_behavior()
        self.git('add', '.')
        self.git('commit', '-qm', 'Validate user input')
        self.head = self.git('rev-parse', 'HEAD')
        self.data = self.root / 'raw.json'
        self.raw = [{'githubPrUrl': 'https://github.com/example/project/pull/1',
                     'source_commit': self.base, 'target_commit': self.head,
                     'comments': [{'path': 'app.py', 'note': 'Validate user input',
                                   'from_line': 1, 'to_line': 1, 'side': 'right'}]}]
        self.data.write_text(json.dumps(self.raw))
        self.config = {'mode': 'fake', 'workflow_repo': str(self.repo),
                       'baseline': self.base, 'candidate': self.head,
                       'dataset': {'path': str(self.data), 'sha256': self.sha(self.data),
                                   'kind': 'fixture', 'repositories': {'example/project': str(self.repo)}},
                       'seed': 7, 'timeout': 5, 'rounds': 1, 'max_attempts': 2}
        self.config_path = self.root / 'config.json'
        self.out = self.root / 'experiment'
        self.save_config()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_behavior(self):
        (self.repo / 'fake-review.json').write_text(json.dumps(self.behavior))

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def cli(self, command, *args, ok=True):
        p = subprocess.run([sys.executable, '-m', 'benchmarks.aacr', command,
                            '--out', str(self.out), *map(str, args)], cwd=ROOT,
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def prepare(self, *args):
        return self.cli('prepare', '--config', self.config_path, *args)

    def report(self):
        evaluation = json.loads(self.cli('evaluate').stdout)['evaluation_id']
        self.cli('compare', '--evaluation', evaluation)
        return json.loads((self.out / 'evaluations' / evaluation / 'report.json').read_text())

    def test_two_commits_complete_public_workflow(self):
        before = self.git('status', '--porcelain')
        self.prepare()
        self.cli('run')
        report = self.report()
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['smoke_only'])
        self.assertTrue(report['report_only'])
        self.assertEqual(report['baseline']['requested'], 1)
        self.assertEqual(report['candidate']['completed'], 1)
        self.assertEqual(report['baseline']['semantic_f1'], 0)
        self.assertEqual(report['candidate']['semantic_f1'], 1)
        self.assertEqual(report['delta']['semantic_f1']['percentage_points'], 100)
        self.assertEqual(report['candidate']['avg_tokens'], 180)
        self.assertGreater(report['candidate']['avg_time'], 0)
        self.assertEqual(report['versions']['baseline']['commit'], self.base)
        self.assertEqual(report['versions']['candidate']['commit'], self.head)
        self.assertEqual(self.git('status', '--porcelain'), before)

    def test_workspace_snapshot_survives_source_changes_and_preserves_modes(self):
        self.config['candidate'] = 'workspace'
        self.config['untracked'] = ['extra.txt']
        (self.repo / 'extra.txt').write_text('new dependency')
        (self.repo / 'app.py').chmod(0o755)
        (self.repo / 'credentials.json').write_text('excluded-secret')
        self.git('add', 'credentials.json')
        self.config['required_files'] = {'candidate': ['extra.txt']}
        self.save_config()
        before = self.git('status', '--porcelain')
        self.prepare()
        manifest = json.loads((self.out / 'manifest.json').read_text())
        candidate = manifest['versions']['candidate']
        self.assertEqual(candidate['kind'], 'workspace')
        self.assertTrue(candidate['included']['app.py']['executable'])
        self.assertEqual(candidate['excluded']['credentials.json'], 'credential path')
        self.assertEqual(self.git('status', '--porcelain'), before)
        (self.repo / 'extra.txt').write_text('changed after prepare')
        self.behavior['Standards'] = []
        self.write_behavior()
        self.cli('run')
        self.assertEqual(self.report()['candidate']['semantic_f1'], 1)

    def test_resume_preserves_success_and_failed_attempt_cost(self):
        self.config['candidate'] = 'workspace'
        self.behavior['failure'] = 'once'
        self.write_behavior()
        self.save_config()
        self.prepare()
        self.cli('run')
        first = self.report()
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['candidate']['failed'], 1)
        baseline = next((self.out / 'runs' / 'baseline').glob('*/*/*/result.json'))
        original = baseline.read_bytes()
        self.cli('run')
        second = self.report()
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(second['candidate']['all_attempts']['known_tokens'], 187)
        self.assertEqual(second['candidate']['all_attempts']['count'], 2)
        self.assertEqual(baseline.read_bytes(), original)
        self.assertEqual(json.loads(self.cli('run').stdout)['attempted'], 0)

    def test_reevaluation_uses_new_judge_without_new_reviewer_runs(self):
        self.prepare()
        self.cli('run')
        before = {str(p): self.sha(p) for p in (self.out / 'runs').rglob('result.json')}
        first = self.report()
        eid = json.loads(self.cli('evaluate', '--judge-model', 'fake-judge-v2', '--judge-reasoning-effort', 'high', '--judge-rounds', 2).stdout)['evaluation_id']
        evaluation = json.loads((self.out / 'evaluations' / eid / 'evaluation.json').read_text())
        self.assertNotEqual(first['evaluation_id'], eid)
        self.assertEqual(evaluation['judge']['model'], 'fake-judge-v2')
        self.assertTrue(evaluation['judge_requests'])
        self.assertTrue(all(r['reasoning_effort'] == 'high' for r in evaluation['judge_requests']))
        self.assertEqual(before, {str(p): self.sha(p) for p in (self.out / 'runs').rglob('result.json')})

    def test_unknown_usage_and_inclusive_parent_are_not_double_counted(self):
        for mode, expected in [('missing', None), ('inclusive', 180)]:
            with self.subTest(mode=mode):
                self.out = self.root / mode
                self.config['candidate'] = 'workspace'
                self.behavior['usage_mode'] = mode
                self.write_behavior()
                self.save_config()
                self.prepare()
                self.cli('run')
                report = self.report()
                self.assertEqual(report['candidate']['avg_tokens'], expected)
                self.assertEqual(report['candidate']['usage_coverage']['known'], 0 if mode == 'missing' else 1)

    def test_failed_missing_invalid_and_timeout_are_distinct(self):
        for mode, expected in [('exit', 'failed'), ('axis-failure', 'failed'), ('events', 'invalid_output'), ('missing', 'invalid_output'), ('invalid', 'invalid_output'), ('timeout', 'timed_out')]:
            with self.subTest(mode=mode):
                self.out = self.root / mode
                self.config.update(candidate='workspace', timeout=0.8)
                self.behavior['failure'] = mode
                self.write_behavior()
                self.save_config()
                self.prepare()
                self.cli('run')
                report = self.report()
                self.assertEqual(report['status'], 'incomplete')
                self.assertEqual(report['candidate'][expected], 1)
                self.assertEqual(report['candidate']['completed'], 0)

    def test_manifest_tampering_and_live_mode_are_rejected(self):
        self.config['mode'] = 'live'
        self.save_config()
        self.assertIn('live mode disabled', self.cli('prepare', '--config', self.config_path, ok=False).stderr)
        self.config['mode'] = 'fake'
        self.save_config()
        self.prepare()
        path = self.out / 'manifest.json'
        value = json.loads(path.read_text())
        value['settings']['reviewer']['model'] = 'different'
        path.write_text(json.dumps(value))
        self.assertIn('manifest/ownership mismatch', self.cli('run', ok=False).stderr)

    def test_dataset_and_range_validation(self):
        for mode in ('checksum', 'duplicate', 'reverse', 'empty', 'sha'):
            with self.subTest(mode=mode):
                self.out = self.root / mode
                raw = json.loads(json.dumps(self.raw))
                if mode == 'duplicate':
                    raw += raw
                if mode == 'reverse':
                    raw[0]['source_commit'], raw[0]['target_commit'] = self.head, self.base
                if mode == 'empty':
                    raw[0]['source_commit'] = self.head
                if mode == 'sha':
                    raw[0]['target_commit'] = 'not-a-sha'
                self.data.write_text(json.dumps(raw))
                self.config['dataset']['sha256'] = 'wrong' if mode == 'checksum' else self.sha(self.data)
                self.save_config()
                self.cli('prepare', '--config', self.config_path, ok=False)

    def test_symlink_and_missing_dependency_are_rejected(self):
        self.config['candidate'] = 'workspace'
        self.config['untracked'] = ['escape']
        (self.repo / 'escape').symlink_to(self.data)
        self.save_config()
        self.assertIn('symlink', self.cli('prepare', '--config', self.config_path, ok=False).stderr)
        self.out = self.root / 'missing'
        self.config['untracked'] = []
        self.config['required_files'] = ['does-not-exist']
        self.save_config()
        self.assertIn('dependency missing', self.cli('prepare', '--config', self.config_path, ok=False).stderr)

    def test_timeout_kills_descendant_and_secret_is_redacted(self):
        self.config.update(candidate='workspace', timeout=0.8)
        self.config['credential_ref'] = 'op://fake-vault/fake-item/token'
        self.behavior.update(failure='timeout', echo_secret=True)
        self.write_behavior()
        self.save_config()
        self.prepare()
        secret = 'fake-sensitive-value-for-contract-test'
        p = subprocess.run([sys.executable, '-m', 'benchmarks.aacr', 'run', '--out', str(self.out)],
                           cwd=ROOT, env={**os.environ, 'AACR_FAKE_SECRET': secret}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        heartbeat = next((self.out / 'runs' / 'candidate').rglob('heartbeat'))
        content = heartbeat.read_bytes()
        self.assertTrue(content)
        time.sleep(.2)
        self.assertEqual(heartbeat.read_bytes(), content)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
        result = json.loads(next((self.out / 'runs' / 'candidate').rglob('result.json')).read_text())
        evidence = next(e for e in result['events'] if e['type'] == 'benchmark.evidence')
        self.assertTrue(evidence['credential_present'])
        self.assertEqual(evidence['isolation_probe'], 'passed')

    def test_official_scorer_duplicate_missing_line_and_empty_reference_cases(self):
        for name, reference_count, generated_count, missing_lines, expected in [
            ('duplicate-generated', 1, 2, False, (0.667, 0.5, 1)),
            ('duplicate-reference', 2, 1, False, (0.667, 1, 0.5)),
            ('missing-line', 1, 1, True, (1, 1, 1)),
            ('empty-reference', 0, 1, False, (0, 0, 0)),
            ('empty-findings', 1, 0, False, (0, 0, 0)),
        ]:
            with self.subTest(name=name):
                self.out = self.root / name
                raw = json.loads(json.dumps(self.raw))
                raw[0]['comments'] *= reference_count
                self.data.write_text(json.dumps(raw))
                self.config['dataset']['sha256'] = self.sha(self.data)
                self.config['candidate'] = 'workspace'
                finding = dict(self.finding)
                if missing_lines:
                    finding.update(start_line=None, end_line=None)
                self.behavior['Standards'] = [finding] * generated_count
                self.write_behavior()
                self.save_config()
                self.prepare()
                self.cli('run')
                report = self.report()['candidate']
                self.assertEqual(tuple(report[k] for k in ('semantic_f1', 'precision', 'recall')), expected)

    def test_partial_run_repeated_rounds_and_pr_cluster_bootstrap(self):
        raw = json.loads(json.dumps(self.raw))
        second = json.loads(json.dumps(raw[0]))
        second['githubPrUrl'] = 'https://github.com/example/second/pull/2'
        raw.append(second)
        self.data.write_text(json.dumps(raw))
        self.config['dataset']['sha256'] = self.sha(self.data)
        self.config['dataset']['repositories']['example/second'] = str(self.repo)
        self.config['rounds'] = 2
        self.save_config()
        self.prepare()
        self.cli('run', '--max-jobs', 1)
        self.assertEqual(self.report()['status'], 'incomplete')
        self.cli('run')
        report = self.report()
        self.assertEqual(report['candidate']['requested'], 4)
        self.assertEqual(report['bootstrap']['paired_prs'], 2)
        self.assertEqual(report['bootstrap']['interval'], [100, 100])
        self.assertEqual(self.report()['bootstrap'], report['bootstrap'])


if __name__ == '__main__':
    unittest.main()
