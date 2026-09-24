"""Generate and execute a credential-free, synthetic AACR-shaped demonstration."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .contracts import PACKAGE, git, write


def demo(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    repo = root / 'workflow-source'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'AACR offline demo')
    git(repo, 'config', 'user.email', 'offline@example.invalid')
    for skill in ('base', 'code-review'):
        target = repo / 'skills' / skill
        target.mkdir(parents=True)
        shutil.copyfile(PACKAGE.parents[1] / 'skills' / skill / 'SKILL.md', target / 'SKILL.md')
    (repo / 'app.py').write_text('value = 1\n')
    write(repo / 'fake-review.json', {'Standards': [], 'Spec': [], 'usage_mode': 'self'})
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Create synthetic baseline')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'app.py').write_text('value = input()\n')
    behavior = {'Standards': [{'file': 'app.py', 'start_line': 1, 'end_line': 1,
                              'summary': 'Validate user input', 'description': ''}],
                'Spec': [], 'usage_mode': 'self'}
    (repo / 'fake-review.json').write_text(json.dumps(behavior))
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Validate user input')
    head = git(repo, 'rev-parse', 'HEAD')
    raw = root / 'raw.json'
    write(raw, [{'githubPrUrl': 'https://github.com/example/synthetic/pull/1',
                 'source_commit': base, 'target_commit': head,
                 'comments': [{'path': 'app.py', 'note': 'Validate user input', 'side': 'right',
                               'from_line': 1, 'to_line': 1}]}])
    config = {'mode': 'fake', 'workflow_repo': str(repo), 'baseline': base, 'candidate': head,
              'dataset': {'path': str(raw), 'kind': 'fixture', 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
                          'repositories': {'example/synthetic': str(repo)}},
              'reviewer': {'model': 'gpt-5.6-luna', 'reasoning_effort': 'ultra'},
              'judge': {'model': 'gpt-5.6-luna', 'reasoning_effort': 'ultra'},
              'timeout': 10, 'rounds': 1, 'max_attempts': 2, 'seed': 7}

    def cli(command, out, *args):
        p = subprocess.run([sys.executable, '-m', 'benchmarks.aacr', command, '--out', str(out), *map(str, args)],
                           capture_output=True, text=True, check=True)
        return json.loads(p.stdout)

    outputs = {}
    for mode in ('commits', 'workspace'):
        cfg = root / f'{mode}.json'
        experiment = root / mode
        if mode == 'workspace':
            config['candidate'] = 'workspace'
            behavior['failure'] = 'once'
            (repo / 'fake-review.json').write_text(json.dumps(behavior))
        write(cfg, config)
        cli('prepare', experiment, '--config', cfg)
        cli('run', experiment)
        first = cli('evaluate', experiment)['evaluation_id']
        cli('compare', experiment, '--evaluation', first)
        if mode == 'workspace':
            cli('run', experiment)
        final = cli('evaluate', experiment, '--judge-model', 'fake-independent-judge', '--judge-reasoning-effort', 'high')['evaluation_id']
        cli('compare', experiment, '--evaluation', final)
        outputs[mode] = {'initial_report': str(experiment / 'evaluations' / first / 'report.md'),
                         'final_report': str(experiment / 'evaluations' / final / 'report.md')}
    return outputs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.out.absolute()), indent=2))
