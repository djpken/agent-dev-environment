"""Prepare fixed datasets, workflow trees and PR bundles without changing source repos."""
from __future__ import annotations

import json
from pathlib import Path
import random
import re
import platform
import shutil
import subprocess
import time
import uuid

from .contracts import (VERSION, PACKAGE, BenchError, digest, encode, git, implementation_hash, model_config,
                        read, safe_path, seal, tree_inventory, write)
from .official import modules


def excluded(name: str) -> str | None:
    parts = Path(name).parts
    if any(p in ('.git', '.scratch', '.codex', '.claude', '.venv', 'node_modules', '__pycache__', 'benchmarks') for p in parts):
        return 'execution state or benchmark artifact'
    if any(p.startswith('.env') or p in ('auth.json', 'credentials.json', 'user.json') or
           p.endswith(('.pem', '.key', '.p12')) or 'secret' in p.lower() for p in parts):
        return 'credential path'
    return None


def snapshot(repo: Path, ref: str, dest: Path, untracked: list[str], required: list[str]) -> dict:
    commit = git(repo, 'rev-parse', '--verify', f'{"HEAD" if ref == "workspace" else ref}^{{commit}}')
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise BenchError('workflow ref must resolve to a full SHA')
    entries = git(repo, 'ls-tree', '-r', '-z', commit).split('\0')
    tracked = {}
    for row in entries:
        if row:
            meta, name = row.split('\t', 1)
            mode, kind, obj = meta.split()
            tracked[name] = (mode, kind, obj)
    available = set(filter(None, git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    if ref == 'workspace' and any(n not in available for n in untracked):
        raise BenchError('untracked files must be explicitly listed and non-ignored')
    index_names = set(filter(None, git(repo, 'ls-files', '-z').split('\0')))
    names = sorted(set(tracked) | (set(untracked) | index_names if ref == 'workspace' else set()))
    omitted = {name: 'untracked not selected' for name in available - set(untracked)} if ref == 'workspace' else {}
    if ref == 'workspace':
        ignored = filter(None, git(repo, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z').split('\0'))
        omitted.update({name: 'git ignored' for name in ignored})

    def collect():
        result = {}
        for name in names:
            safe_path(name)
            reason = excluded(name)
            if reason:
                omitted[name] = reason
                continue
            if ref == 'workspace':
                p = repo / name
                if p.is_symlink() or any(q.is_symlink() for q in p.parents if q != repo.parent):
                    raise BenchError(f'symlink rejected: {name}')
                if not p.exists():
                    omitted[name] = 'deleted'
                    continue
                if not p.is_file() or not p.resolve().is_relative_to(repo):
                    raise BenchError(f'unsafe workflow file: {name}')
                content, executable = p.read_bytes(), bool(p.stat().st_mode & 0o111)
            else:
                mode, kind, obj = tracked[name]
                if kind != 'blob' or mode not in ('100644', '100755'):
                    raise BenchError(f'unsupported workflow entry: {name}')
                content = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', obj])
                executable = mode == '100755'
            # Fail closed on common embedded credentials; path exclusions alone are insufficient.
            if re.search(rb'-----BEGIN .*PRIVATE KEY-----|(?:sk|ghp|github_pat)-[A-Za-z0-9_]{16,}', content):
                raise BenchError(f'possible embedded credential: {name}')
            result[name] = (content, executable)
        return result

    first = collect()
    for name in required:
        if name not in first:
            raise BenchError(f'required workflow dependency missing/excluded: {name}')
    dest.mkdir(parents=True)
    for name, (content, executable) in first.items():
        p = dest / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        p.chmod(0o755 if executable else 0o644)
    if ref == 'workspace' and (collect() != first or git(repo, 'rev-parse', 'HEAD') != commit or
                               set(filter(None, git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'))) != available or
                               set(filter(None, git(repo, 'ls-files', '-z').split('\0'))) != index_names):
        raise BenchError('workspace changed during snapshot; prepare again')
    inventory = tree_inventory(dest)
    return {'kind': 'workspace' if ref == 'workspace' else 'commit', 'commit': commit,
            'digest': digest(encode(inventory)), 'included': inventory, 'excluded': omitted}


def prepare(config_path: Path, root: Path, reviewer: dict | None = None) -> dict:
    started = time.monotonic()
    config = read(config_path)
    if config.get('mode') != 'fake':
        raise BenchError('live mode disabled: budget authorization and provider preflight required')
    credential_ref = config.get('credential_ref')
    if credential_ref is not None and (not isinstance(credential_ref, str) or
                                       not re.fullmatch(r'op://[^/\n]+/[^/\n]+/[^/\n]+', credential_ref)):
        raise BenchError('credential_ref must be an op://vault/item/field reference')
    if root.exists() or root.is_symlink() or root.absolute() != root.resolve():
        raise BenchError('output must be a new canonical directory')
    repo = Path(config['workflow_repo']).resolve()
    if root.is_relative_to(repo):
        raise BenchError('experiment artifacts must be outside the source worktree')
    settings = {'reviewer': model_config(config.get('reviewer') | reviewer if config.get('reviewer') and reviewer else reviewer or config.get('reviewer')),
                'judge': model_config(config.get('judge')), 'timeout': config.get('timeout', 60),
                'rounds': config.get('rounds', 1), 'max_attempts': config.get('max_attempts', 2),
                'concurrency': 1, 'cache': 'none', 'endpoint': 'offline', 'protocol': 'fake-codex-v1', 'line_k': 1}
    for key in ('rounds', 'max_attempts'):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 100:
            raise BenchError(f'invalid {key}')
    if not isinstance(settings['timeout'], (int, float)) or not 0 < settings['timeout'] <= 3600:
        raise BenchError('invalid timeout')
    schema, converter, _, _ = modules()
    dataset = config['dataset']
    source = Path(dataset['path']).resolve()
    raw_bytes = source.read_bytes()
    if digest(raw_bytes) != dataset['sha256']:
        raise BenchError('dataset checksum mismatch')
    upstream = read(PACKAGE / 'upstream.lock.json')
    if dataset.get('kind') not in ('fixture', 'official'):
        raise BenchError('dataset kind must be fixture or official')
    if dataset['kind'] == 'official' and digest(raw_bytes) != upstream['dataset']['sha256']:
        raise BenchError('official dataset checksum mismatch')
    records = json.loads(raw_bytes)
    if not isinstance(records, list) or not records:
        raise BenchError('dataset must be a nonempty array')
    converted = []
    ids = set()
    # Validate before sampling: invalid records must never silently disappear.
    for i, record in enumerate(records):
        instance = converter.convert_record(record)
        if instance is None:
            raise BenchError(f'invalid AACR record {i}')
        if len(instance.reference_comments) != len(record.get('comments', [])):
            raise BenchError(f'converter discarded reference comments in record {i}')
        instance = schema.ReviewInstance.from_dict(instance.to_dict(), f'record {i}')
        if instance.instance_id in ids:
            raise BenchError(f'duplicate case: {instance.instance_id}')
        ids.add(instance.instance_id)
        converted.append(instance)
    random.Random(config.get('seed', 0)).shuffle(converted)
    count = config.get('limit', len(converted))
    if type(count) is not int or not 1 <= count <= len(converted):
        raise BenchError('invalid sample limit')
    converted = converted[:count]
    for instance in converted:
        local = Path(dataset['repositories'][instance.repo]).resolve()
        if root.is_relative_to(local):
            raise BenchError('experiment artifacts must be outside every PR worktree')
    root.mkdir(parents=True, mode=0o700)
    (root / 'inputs').mkdir()
    versions = {}
    for side in ('baseline', 'candidate'):
        extra = config.get('required_files', [])
        if isinstance(extra, dict):
            extra = extra.get(side, [])
        required = ['skills/base/SKILL.md', 'skills/code-review/SKILL.md', *extra]
        versions[side] = snapshot(repo, config[side], root / 'workflows' / side,
                                  config.get('untracked', []), required)
    cases = []
    for index, instance in enumerate(converted):
        case = instance.to_dict()
        local = Path(dataset['repositories'][instance.repo]).resolve()
        for sha in (instance.base_commit, instance.head_commit):
            if not re.fullmatch('[0-9a-f]{40}', sha) or git(local, 'rev-parse', '--verify', f'{sha}^{{commit}}') != sha:
                raise BenchError('invalid PR SHA')
        git(local, 'merge-base', '--is-ancestor', instance.base_commit, instance.head_commit)
        diff = git(local, 'diff', '--no-ext-diff', '--no-textconv', f'{instance.base_commit}...{instance.head_commit}', '--', '.', ':(exclude).scratch/**')
        if not diff:
            raise BenchError('empty Review range')
        bundle = root / 'inputs' / f'{index}.bundle'
        git(local, 'bundle', 'create', str(bundle), '--all')
        case.update({'key': str(index), 'bundle': bundle.name, 'diff_hash': digest(diff.encode()),
                     'classification': 'unavailable'})
        cases.append(case)
    write(root / 'inputs' / 'cases.json', cases)
    manifest = {'schema_version': VERSION, 'experiment_id': uuid.uuid4().hex, 'mode': 'fake',
                'smoke_only': True, 'target_kind': 'host-workflow', 'host': 'codex',
                'host_contract': 'codex-cli 0.155.0 exec; offline fake-v1',
                'implementation_hash': implementation_hash(),
                'credential_ref': credential_ref,
                'environment': {'controller_python': platform.python_version(), 'system': platform.system(),
                                'fake_python': subprocess.check_output(['/usr/bin/python3', '--version'], text=True).strip(),
                                'bubblewrap': subprocess.check_output([shutil.which('bwrap') or 'bwrap', '--version'], text=True).strip()},
                'model_verified_live': False, 'task_source_mode': 'commit-subjects',
                'settings': settings, 'versions': versions, 'upstream': upstream,
                'dataset': {'sha256': digest(raw_bytes), 'kind': dataset['kind'],
                            'source': upstream['dataset']['source'] if dataset['kind'] == 'official' else 'local fixture',
                            'seed': config.get('seed', 0), 'cases_hash': digest(encode(cases))},
                'case_ids': [c['instance_id'] for c in cases], 'created_at': time.time(),
                'setup_seconds': time.monotonic() - started}
    seal(root, manifest)
    return {'experiment_id': manifest['experiment_id'], 'manifest_hash': digest(encode(manifest))}
