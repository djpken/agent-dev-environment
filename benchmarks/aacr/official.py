"""Load byte-for-byte pinned upstream modules. No upstream CLI/network bootstrap."""
import importlib
import os
import sys
from .contracts import PACKAGE, BenchError, digest, read


def modules():
    lock = read(PACKAGE / 'upstream.lock.json')
    vendor = PACKAGE / 'vendor'
    for name, expected in lock['files'].items():
        if digest((vendor / name).read_bytes()) != expected:
            raise BenchError(f'upstream checksum mismatch: {name}')
    # Upstream imports top-level config/schema/judge; isolated benchmark process only.
    sys.path.insert(0, str(vendor))
    os.environ['JUDGE_USE_MOCK'] = 'true'
    schema = importlib.import_module('schema')
    converter = importlib.import_module('converters.aacr_bench')
    judge = importlib.import_module('judge')
    evaluator = importlib.import_module('evaluate')
    if not judge.USE_MOCK_LLM:
        raise BenchError('live Judge disabled')
    return schema, converter, judge, evaluator
