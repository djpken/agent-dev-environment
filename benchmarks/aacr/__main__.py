"""Development-only CLI. P0-P2 deliberately has no live execution path."""
import argparse
import json
from pathlib import Path
import sys

from .contracts import BenchError
from .prepare import prepare
from .runner import run
from .evaluate import evaluate
from .compare import compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'evaluate', 'compare'])
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--reasoning-effort')
    parser.add_argument('--judge-model')
    parser.add_argument('--judge-reasoning-effort')
    parser.add_argument('--judge-rounds', type=int, default=1)
    parser.add_argument('--max-jobs', type=int)
    parser.add_argument('--evaluation')
    args = parser.parse_args()
    try:
        root = args.out.absolute()
        if args.command != 'prepare' and (args.model or args.reasoning_effort or args.config):
            raise BenchError('reviewer/config overrides require prepare; manifest is immutable')
        if args.command != 'evaluate' and (args.judge_model or args.judge_reasoning_effort or args.judge_rounds != 1):
            raise BenchError('Judge overrides require evaluate')
        if args.max_jobs is not None and (args.command != 'run' or args.max_jobs < 1):
            raise BenchError('--max-jobs must be a positive integer for run')
        if args.command == 'prepare':
            if not args.config:
                raise BenchError('prepare requires --config')
            override = {k: v for k, v in {'model': args.model, 'reasoning_effort': args.reasoning_effort}.items() if v}
            result = prepare(args.config, root, override or None)
        elif args.command == 'run':
            result = run(root, args.max_jobs)
        elif args.command == 'evaluate':
            override = {k: v for k, v in {'model': args.judge_model, 'reasoning_effort': args.judge_reasoning_effort}.items() if v}
            result = evaluate(root, override or None, args.judge_rounds)
        else:
            if not args.evaluation:
                raise BenchError('compare requires --evaluation')
            value = compare(root, args.evaluation)
            result = {'evaluation_id': args.evaluation, 'status': value['status'], 'smoke_only': True}
        print(json.dumps(result))
        return 0
    except (BenchError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'aacr: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
