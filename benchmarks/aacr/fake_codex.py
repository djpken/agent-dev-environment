#!/usr/bin/python3
"""Offline Codex exec fixture. Never imports or calls a model SDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['exec', 'axis'])
    parser.add_argument('prompt', nargs='?')
    parser.add_argument('--model')
    parser.add_argument('-c', action='append', default=[])
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--ephemeral', action='store_true')
    parser.add_argument('--output-last-message')
    parser.add_argument('--output-schema')
    parser.add_argument('--sandbox')
    args = parser.parse_args()
    request = json.loads(Path('/work/request.json').read_text())
    workflow = Path('/work/workflow')
    behavior_path = workflow / 'fake-review.json'
    behavior = json.loads(behavior_path.read_text()) if behavior_path.exists() else {'Standards': [], 'Spec': []}
    if args.command == 'axis':
        axis = args.prompt
        if behavior.get('failure') == 'axis-failure' and axis == 'Spec':
            return 8
        emit({'axis': axis, 'findings': behavior.get(axis, []),
              'model': request['model'], 'usage': {'input_tokens': 50, 'output_tokens': 10}})
        return
    prompt = sys.stdin.read()
    assert '$code-review' in prompt and 'skills/code-review/SKILL.md' in prompt
    assert args.model == request['model']['model']
    assert args.c == ['model_reasoning_effort=' + json.dumps(request['model']['reasoning_effort'])]
    assert Path('.scratch/base').read_text() == f"base_sha: {request['base']}\nsource: user\nsummary: \n"
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == request['head']
    evidence = {'skill_hash': hashlib.sha256((workflow / 'skills/code-review/SKILL.md').read_bytes()).hexdigest(),
                'base_hash': hashlib.sha256((workflow / 'skills/base/SKILL.md').read_bytes()).hexdigest(),
                'model': request['model'], 'base': request['base'], 'head': request['head'],
                'session': request['session'], 'task_source_mode': 'commit-subjects'}
    for denied in request['denied_paths']:
        try:
            Path(denied).read_bytes()
        except (OSError, PermissionError):
            continue
        raise RuntimeError('isolation failed')
    # The network namespace must contain no usable network interface other than loopback.
    assert set(os.listdir('/sys/class/net')) <= {'lo'} if Path('/sys/class/net').exists() else True
    evidence['isolation_probe'] = 'passed'
    evidence['credential_present'] = bool(os.environ.get('CODEX_API_KEY'))
    emit({'type': 'benchmark.evidence', **evidence})
    if behavior.get('echo_secret'):
        emit({'type': 'diagnostic', 'value': os.environ.get('CODEX_API_KEY')})
    mode = behavior.get('failure', '')
    if mode == 'once' and request['attempt'] == 1:
        emit({'type': 'benchmark.usage', 'scope': 'inclusive', 'agents': {'main': {'input_tokens': 5, 'output_tokens': 2}}})
        return 9
    if mode == 'timeout':
        child = subprocess.Popen(['/usr/bin/python3', '-c',
                                  'import time\nwhile True:\n with open("/work/heartbeat", "a") as f: f.write("alive\\n")\n time.sleep(.05)'])
        emit({'type': 'benchmark.child', 'pid': child.pid})
        time.sleep(120)
    if mode == 'exit':
        return 7
    if mode == 'missing':
        return 0
    if mode == 'invalid':
        Path(args.output_last_message).write_text('not JSON')
        return 0
    if mode == 'events':
        emit([])
        return 0
    axes = {}
    for axis in ('Standards', 'Spec'):
        axes[axis] = subprocess.Popen(['/usr/bin/python3', __file__, 'axis', axis], stdout=subprocess.PIPE, text=True)
    reports = {}
    agents = {'main': {'input_tokens': 50, 'output_tokens': 10}}
    for axis, proc in axes.items():
        stdout, _ = proc.communicate()
        if proc.returncode:
            return 8
        result = json.loads(stdout)
        assert result['model'] == request['model']
        reports[axis] = result['findings']
        agents[axis] = result['usage']
        emit({'type': 'benchmark.axis', 'axis': axis, 'model': result['model'], 'status': 'completed'})
    usage_mode = behavior.get('usage_mode', 'self')
    if usage_mode == 'inclusive':
        agents['main'] = {'input_tokens': 150, 'output_tokens': 30, 'cached_input_tokens': 40, 'reasoning_output_tokens': 15}
    if usage_mode != 'missing':
        emit({'type': 'benchmark.usage', 'scope': usage_mode, 'agents': agents})
    Path(args.output_last_message).write_text(json.dumps({'Standards': reports['Standards'], 'Spec': reports['Spec']}))
    emit({'type': 'turn.completed', 'usage': agents['main']})
    return 0


if __name__ == '__main__':
    sys.exit(main())
