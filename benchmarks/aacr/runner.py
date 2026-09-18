"""Disposable Codex execution, OS isolation, attempt journal and strict output adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from .contracts import (PACKAGE, SIDES, VERSION, BenchError, digest, encode, git, load,
                        locked, read, safe_path, write)


def sandbox(work: Path) -> list[str]:
    binary = shutil.which('bwrap')
    if not binary:
        raise BenchError('isolation unavailable: bubblewrap required')
    argv = [binary, '--unshare-all', '--die-with-parent', '--new-session', '--ro-bind', '/usr', '/usr']
    for name in ('bin', 'lib', 'lib64'):
        p = Path('/') / name
        if p.is_symlink():
            argv += ['--symlink', os.readlink(p), str(p)]
        elif p.exists():
            argv += ['--ro-bind', str(p), str(p)]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--bind', str(work), '/work',
             '--chdir', '/work/repo', '--setenv', 'PATH', '/usr/bin:/bin',
             '--setenv', 'HOME', '/work/home', '--setenv', 'CODEX_HOME', '/work/home/.codex',
             '--setenv', 'GIT_CONFIG_NOSYSTEM', '1', '--setenv', 'GIT_CONFIG_GLOBAL', '/dev/null']
    return argv


def usage(events: list) -> dict:
    records = [e for e in events if e.get('type') == 'benchmark.usage']
    if len(records) != 1:
        return {'status': 'unknown', 'total_tokens': None}
    record = records[0]
    agents = record.get('agents', {})
    selected = ['main'] if record.get('scope') == 'inclusive' else ['main', 'Standards', 'Spec']
    if record.get('scope') not in ('self', 'inclusive'):
        return {'status': 'unknown', 'total_tokens': None}
    if any(a not in agents or any(type(agents[a].get(k)) is not int or agents[a][k] < 0
                                for k in ('input_tokens', 'output_tokens')) for a in selected):
        return {'status': 'unknown', 'total_tokens': None}
    return {'status': 'known', 'scope': record['scope'], 'agents': agents,
            'input_tokens': sum(agents[a]['input_tokens'] for a in selected),
            'output_tokens': sum(agents[a]['output_tokens'] for a in selected),
            'total_tokens': sum(agents[a]['input_tokens'] + agents[a]['output_tokens'] for a in selected)}


def findings(path: Path) -> dict:
    if not path.exists():
        raise BenchError('missing output file')
    result = read(path)
    if not isinstance(result, dict) or set(result) != {'Standards', 'Spec'}:
        raise BenchError('invalid two-axis output')
    for axis, items in result.items():
        if not isinstance(items, list):
            raise BenchError(f'invalid {axis} findings')
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('file'), str) or not item['file'].strip():
                raise BenchError('finding has no file')
            safe_path(item['file'])
            if not isinstance(item.get('summary'), str) or not item['summary'].strip() or not isinstance(item.get('description', ''), str):
                raise BenchError('finding has no summary or invalid description')
            for key in ('start_line', 'end_line'):
                n = item.get(key)
                if n is not None and (type(n) is not int or n < 1):
                    raise BenchError('invalid finding line')
    return result


def attempt(root: Path, manifest: dict, case: dict, side: str, round_id: int, number: int) -> dict:
    session = uuid.uuid4().hex
    folder = root / 'runs' / side / case['key'] / str(round_id) / str(number)
    folder.mkdir(parents=True)
    write(folder / 'started.json', {'session': session, 'manifest_hash': digest(encode(manifest)), 'started_at': time.time()})
    work = folder / 'sandbox'
    work.mkdir()
    (work / 'home' / '.codex').mkdir(parents=True)
    clone_started = time.monotonic()
    git(work, 'clone', '--no-checkout', str(root / 'inputs' / case['bundle']), str(work / 'repo'))
    git(work / 'repo', 'checkout', '--detach', case['head_commit'])
    git(work / 'repo', 'remote', 'remove', 'origin')
    scratch = work / 'repo' / '.scratch'
    if scratch.exists():
        raise BenchError('PR contains reserved .scratch state')
    scratch.mkdir()
    (scratch / 'base').write_text(f"base_sha: {case['base_commit']}\nsource: user\nsummary: \n")
    shutil.copytree(root / 'workflows' / side, work / 'workflow')
    shutil.copyfile(PACKAGE / 'fake_codex.py', work / 'fake_codex.py')
    config = manifest['settings']['reviewer']
    (work / 'home' / '.codex' / 'config.toml').write_text(
        'model = ' + json.dumps(config['model']) + '\nmodel_reasoning_effort = ' + json.dumps(config['reasoning_effort']) + '\n')
    request = {'model': config, 'base': case['base_commit'], 'head': case['head_commit'],
               'session': session, 'attempt': number,
               'denied_paths': [str(root / 'inputs' / 'cases.json'), str(root / 'judge-sentinel.txt'),
                                str(root / 'workflows' / ('candidate' if side == 'baseline' else 'baseline') / 'skills/code-review/SKILL.md')]}
    write(work / 'request.json', request)
    write(work / 'output-schema.json', {'type': 'object', 'required': ['Standards', 'Spec'],
                                      'properties': {'Standards': {'type': 'array'}, 'Spec': {'type': 'array'}}})
    clone_seconds = time.monotonic() - clone_started
    command = ['/usr/bin/python3', '/work/fake_codex.py', 'exec', '--json', '--ephemeral',
               '--sandbox', 'workspace-write', '--model', config['model'], '-c',
               'model_reasoning_effort=' + json.dumps(config['reasoning_effort']),
               '--output-schema', '/work/output-schema.json', '--output-last-message', '/work/output.json']
    if manifest.get('credential_ref'):
        shutil.copyfile(PACKAGE / 'fake_op.py', work / 'fake_op.py')
        (work / 'credential.refs').write_text('CODEX_API_KEY=' + manifest['credential_ref'] + '\n')
        command = ['/usr/bin/python3', '/work/fake_op.py', 'run', '--env-file', '/work/credential.refs', '--', *command]
    prompt = ('Use $code-review from /work/workflow/skills/code-review/SKILL.md. '
              'Execute both independent Standards and Spec axes with the configured model. '
              'Use .scratch/base and commit-subject fallback. Return both axes as JSON; '
              'preserve file and line provenance, use null for unavailable lines. Do not repair findings.\n')
    start = time.monotonic()
    status, reason = 'completed', None
    # Offline credential-contract probe only; never query a real vault in P0-P2.
    secret = os.environ.get('AACR_FAKE_SECRET')
    child_env = {'PATH': '/usr/bin:/bin'}
    if manifest.get('credential_ref'):
        if not secret:
            raise BenchError('fake credential reference requires AACR_FAKE_SECRET; real vault reads are disabled')
        child_env['AACR_FAKE_SECRET'] = secret
    proc = subprocess.Popen(sandbox(work) + command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=child_env, start_new_session=True)
    stdout, stderr = b'', b''
    try:
        stdout, stderr = proc.communicate(prompt.encode(), timeout=manifest['settings']['timeout'])
        if proc.returncode:
            status, reason = 'failed', f'exit {proc.returncode}'
    except subprocess.TimeoutExpired:
        status, reason = 'timed_out', 'workflow timeout'
    except KeyboardInterrupt:
        status, reason = 'failed', 'interrupted'
    finally:
        # The PID namespace also reaps children that changed their process group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining_out, remaining_err = proc.communicate()
        stdout, stderr = remaining_out or stdout, remaining_err or stderr
    duration = time.monotonic() - start
    if secret:
        stdout = stdout.replace(secret.encode(), b'[REDACTED]')
        stderr = stderr.replace(secret.encode(), b'[REDACTED]')
    (folder / 'stdout.jsonl').write_bytes(stdout)
    (folder / 'stderr.txt').write_bytes(stderr)
    if secret:
        for p in (work / 'output.json',):
            if p.exists():
                p.write_bytes(p.read_bytes().replace(secret.encode(), b'[REDACTED]'))
    events = []
    try:
        for line in (folder / 'stdout.jsonl').read_text().splitlines():
            if not line:
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('event must be an object')
            events.append(event)
    except (ValueError, UnicodeError):
        # A truncated final event must not erase an interrupt/timeout or earlier usage.
        if status == 'completed':
            status, reason = 'invalid_output', 'invalid event stream'
    parsed = None
    evidence = [e for e in events if e.get('type') == 'benchmark.evidence']
    axes = [e for e in events if e.get('type') == 'benchmark.axis']
    if status == 'completed':
        try:
            parsed = findings(work / 'output.json')
            expected = manifest['versions'][side]['included']['skills/code-review/SKILL.md']['sha256']
            if (len(evidence) != 1 or evidence[0].get('skill_hash') != expected or
                evidence[0].get('model') != config or evidence[0].get('base') != case['base_commit'] or
                evidence[0].get('head') != case['head_commit'] or evidence[0].get('isolation_probe') != 'passed' or
                {a.get('axis') for a in axes} != {'Standards', 'Spec'} or
                any(a.get('model') != config or a.get('status') != 'completed' for a in axes)):
                raise BenchError('missing/mismatched workflow or subagent evidence')
        except (ValueError, OSError, TypeError) as exc:
            status, reason = 'invalid_output', str(exc)
            parsed = None
    result = {'schema_version': VERSION, 'manifest_hash': digest(encode(manifest)), 'session': session,
              'case_id': case['instance_id'], 'side': side, 'round': round_id, 'attempt': number,
              'status': status, 'reason': reason, 'duration_seconds': duration, 'clone_seconds': clone_seconds,
              'usage': usage(events), 'axes': parsed, 'events': events, 'command': command,
              'findings_hash': digest(encode(parsed)) if parsed is not None else None, 'smoke_only': True}
    write(folder / 'result.json', result)
    write(folder / 'result.sha256.json', digest(encode(result)))
    return result


def results(root: Path, manifest: dict) -> dict:
    output = {}
    for case in read(root / 'inputs' / 'cases.json'):
        for side in SIDES:
            for round_id in range(manifest['settings']['rounds']):
                folder = root / 'runs' / side / case['key'] / str(round_id)
                attempts = []
                for p in sorted(folder.glob('*/started.json'), key=lambda p: int(p.parent.name)):
                    result_file = p.parent / 'result.json'
                    if result_file.exists() and (p.parent / 'result.sha256.json').exists():
                        result = read(result_file)
                        if digest(encode(result)) != read(p.parent / 'result.sha256.json') or result['manifest_hash'] != digest(encode(manifest)):
                            raise BenchError('result/manifest mismatch')
                        attempts.append(result)
                    else:
                        attempts.append({'status': 'failed', 'reason': 'interrupted attempt', 'usage': {'status': 'unknown', 'total_tokens': None}})
                completed = next((a for a in attempts if a['status'] == 'completed'), None)
                output[(side, case['key'], round_id)] = {'attempts': attempts, 'final': completed or (attempts[-1] if attempts else None)}
    return output


def run(root: Path, max_jobs: int | None = None) -> dict:
    with locked(root):
        manifest = load(root)
        if not (root / 'judge-sentinel.txt').exists():
            (root / 'judge-sentinel.txt').write_text('judge output must not reach reviewer')
        current = results(root, manifest)
        count = 0
        for case in read(root / 'inputs' / 'cases.json'):
            for round_id in range(manifest['settings']['rounds']):
                for side in SIDES:
                    previous = current[(side, case['key'], round_id)]
                    if previous['final'] and previous['final']['status'] == 'completed':
                        continue
                    n = len(previous['attempts']) + 1
                    if n > manifest['settings']['max_attempts']:
                        continue
                    if max_jobs is not None and count >= max_jobs:
                        return {'attempted': count, 'stopped': True}
                    result = attempt(root, manifest, case, side, round_id, n)
                    count += 1
                    if result['reason'] == 'interrupted':
                        return {'attempted': count, 'stopped': True, 'reason': 'interrupted'}
        return {'attempted': count, 'stopped': False}
