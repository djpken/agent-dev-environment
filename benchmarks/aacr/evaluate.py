"""New immutable evaluations over saved findings; reviewer is never invoked here."""
from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path
import time
import uuid

from .contracts import SIDES, VERSION, BenchError, digest, encode, load, locked, model_config, read, write
from .official import modules
from .runner import results


class OfflineJudge:
    """Versioned semantic boundary: explicit model/reasoning, deterministic upstream mock."""
    def __init__(self, judge, config):
        self.judge = judge
        self.config = model_config(config)
        self.requests: list[dict] = []

    async def match(self, reference_note: str, generated_note: str) -> dict:
        request = {'model': self.config['model'], 'reasoning_effort': self.config['reasoning_effort'],
                   'reference': reference_note, 'generated': generated_note}
        self.requests.append(request)
        return {'is_similar': self.judge._mock_semantic_match(request['reference'], request['generated']),
                'reason': 'smoke-only: upstream deterministic mock; model not called'}


def evaluate(root: Path, judge_config: dict | None = None, judge_rounds: int = 1) -> dict:
    if type(judge_rounds) is not int or not 1 <= judge_rounds <= 100:
        raise BenchError('invalid Judge rounds')
    with locked(root):
        manifest = load(root)
        settings = model_config(manifest['settings']['judge'] | (judge_config or {}))
        schema, _, judge, scorer = modules()
        adapter = OfflineJudge(judge, settings)
        judge.match_semantic = adapter.match
        current = results(root, manifest)
        cases = read(root / 'inputs' / 'cases.json')
        evaluation_id = uuid.uuid4().hex
        dest = root / 'evaluations' / evaluation_id
        dest.mkdir(parents=True)
        started = time.monotonic()
        scores = []
        findings_hashes = {}
        for round_id in range(manifest['settings']['rounds']):
            for case in cases:
                for side in SIDES:
                    result = current[(side, case['key'], round_id)]['final']
                    if result is None or result['status'] != 'completed':
                        continue
                    key = f'{side}/{case["key"]}/{round_id}'
                    findings_hashes[key] = result['findings_hash']
                    target = dest / 'targets' / side / str(round_id)
                    target.mkdir(parents=True, exist_ok=True)
                    write(target / (case['instance_id'].replace('/', '__') + '.json'),
                          {'review_output': [f for axis in ('Standards', 'Spec') for f in result['axes'][axis]],
                           'duration_seconds': result['duration_seconds'],
                           'token_usage': {k: result['usage'][k] for k in ('input_tokens', 'output_tokens')}
                                          if result['usage']['status'] == 'known' else {}})
                    for j in range(judge_rounds):
                        instance = schema.ReviewInstance.from_dict(case, case['instance_id'])
                        # Preserve upstream scorer's exact path/side/line/matching and rounding.
                        with contextlib.redirect_stdout(io.StringIO()):
                            official = asyncio.run(scorer.evaluate([instance], target, 'codex',
                                                   dest / 'official' / side / str(round_id) / case['key'],
                                                   line_k=manifest['settings']['line_k'], round_label=f'_{j}'))
                        scores.append({'side': side, 'key': case['key'], 'case_id': case['instance_id'],
                                       'reviewer_round': round_id, 'judge_round': j,
                                       'summary': official['summary'], 'matches': official['eval_res'],
                                       'axes': result['axes'], 'findings_hash': result['findings_hash'],
                                       'classification': case['classification']})
        value = {'schema_version': VERSION, 'evaluation_id': evaluation_id,
                 'experiment_id': manifest['experiment_id'], 'manifest_hash': digest(encode(manifest)),
                 'smoke_only': True, 'judge': settings, 'judge_rounds': judge_rounds,
                 'scorer': manifest['upstream'], 'findings_hashes': findings_hashes, 'scores': scores,
                 'judge_usage': {'status': 'mock-no-model', 'total_tokens': None, 'requests': len(adapter.requests)},
                 'judge_requests': adapter.requests, 'judge_seconds': time.monotonic() - started,
                 'experiment_elapsed_seconds': time.time() - manifest['created_at'] + manifest['setup_seconds'],
                 'run_snapshot': [{'side': s, 'key': k, 'round': r, **value} for (s, k, r), value in current.items()]}
        write(dest / 'evaluation.json', value)
        write(dest / 'evaluation.sha256.json', digest(encode(value)))
        return {'evaluation_id': evaluation_id}
