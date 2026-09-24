"""Versioned artifacts and immutable experiment verification."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

VERSION = 1
SIDES = ('baseline', 'candidate')
DEFAULT_MODEL = {'model': 'gpt-5.6-luna', 'reasoning_effort': 'ultra'}
PACKAGE = Path(__file__).resolve().parent


class BenchError(ValueError):
    pass


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Link a fully flushed temporary file into place without replacing old artifacts.
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def model_config(value: dict | None) -> dict:
    config = DEFAULT_MODEL | (value or {})
    if set(config) != set(DEFAULT_MODEL) or not isinstance(config['model'], str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+', config['model']):
        raise BenchError('invalid model configuration')
    if config['reasoning_effort'] not in ('minimal', 'low', 'medium', 'high', 'xhigh', 'ultra'):
        raise BenchError('unsupported reasoning_effort')
    return config


def git(repo: Path, *args: str) -> str:
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    p = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
                       env=env, text=True, capture_output=True)
    if p.returncode:
        raise BenchError(f'git {args[0]} failed: {p.stderr.strip()}')
    return p.stdout.strip()


def safe_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise BenchError(f'unsafe path: {name}')
    return path


def tree_inventory(root: Path) -> dict:
    files = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise BenchError(f'symlink rejected: {p}')
        if p.is_file():
            files[p.relative_to(root).as_posix()] = {'sha256': digest(p.read_bytes()),
                                                    'executable': bool(p.stat().st_mode & 0o111)}
    return files


def implementation_hash() -> str:
    return digest(encode({p.name: digest(p.read_bytes()) for p in sorted(PACKAGE.glob('*.py'))}))


def seal(root: Path, manifest: dict) -> None:
    manifest['inventory'] = {p: tree_inventory(root / p) for p in ('workflows', 'inputs')}
    write(root / 'manifest.json', manifest)
    write(root / '.aacr-owner.json', {'schema_version': VERSION, 'root': str(root),
                                    'manifest_hash': digest(encode(manifest))})


def load(root: Path) -> dict:
    if root.is_symlink() or root.absolute() != root.resolve():
        raise BenchError('experiment path must be canonical and not a symlink')
    owner = read(root / '.aacr-owner.json')
    manifest = read(root / 'manifest.json')
    if owner.get('root') != str(root) or owner.get('manifest_hash') != digest(encode(manifest)):
        raise BenchError('manifest/ownership mismatch; resume refused')
    if manifest.get('schema_version') != VERSION or manifest.get('mode') != 'fake':
        raise BenchError('unsupported manifest or live mode disabled')
    if manifest.get('implementation_hash') != implementation_hash():
        raise BenchError('benchmark implementation changed; use the original version or prepare again')
    for directory, inventory in manifest['inventory'].items():
        if tree_inventory(root / directory) != inventory:
            raise BenchError(f'immutable input changed: {directory}')
    # Reject redirected result directories before any write or deletion.
    for p in root.rglob('*'):
        if p.is_symlink():
            raise BenchError(f'artifact symlink rejected: {p}')
    return manifest


@contextmanager
def locked(root: Path):
    load(root)
    with (root / '.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchError('experiment is busy') from exc
        yield
