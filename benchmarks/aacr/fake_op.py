"""Offline fixture for the op run process boundary; never opens a vault."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run'])
    parser.add_argument('--env-file', required=True)
    split = sys.argv.index('--')
    args = parser.parse_args(sys.argv[1:split])
    argv = sys.argv[split + 1:]
    reference = Path(args.env_file).read_text().strip()
    if not reference.startswith('CODEX_API_KEY=op://') or '\n' in reference:
        raise ValueError('invalid credential reference file')
    env = dict(os.environ)
    env['CODEX_API_KEY'] = env.pop('AACR_FAKE_SECRET')
    os.execvpe(argv[0], argv, env)


if __name__ == '__main__':
    main()
