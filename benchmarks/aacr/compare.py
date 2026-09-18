"""Paired, report-only diagnostics with requested denominators and PR cluster bootstrap."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import random
import statistics

from .contracts import SIDES, BenchError, digest, encode, load, locked, read, safe_path, write
from .official import modules

METRICS = ('semantic_f1', 'precision', 'recall', 'avg_time', 'avg_tokens')


def aggregate(scores: list) -> dict:
    _, _, _, scorer = modules()
    stats = {k: 0 for k in ('expected_notes', 'generated_notes', 'matched_semantic_notes', 'matched_line_notes',
                             'total_instances', 'candidate_instances', 'evaluated_instances', 'missing_instances')}
    for score in scores:
        for key in stats:
            stats[key] += score['summary'][key]
    result = scorer._compute_summary(stats)
    return {'semantic_f1': result['semantic_f1'], 'precision': result['semantic_match_rate'],
            'recall': result['semantic_recall_rate'], 'line_f1': result['line_f1'],
            'line_precision': result['line_match_rate'], 'line_recall': result['line_recall_rate'],
            'official_summary': result}


def side_summary(evaluation: dict, side: str) -> dict:
    runs = [r for r in evaluation['run_snapshot'] if r['side'] == side]
    finals = [r['final'] for r in runs if r['final'] is not None]
    successes = [r for r in finals if r['status'] == 'completed']
    known = [r['usage']['total_tokens'] for r in successes if r['usage']['status'] == 'known']
    attempts = [a for r in runs for a in r['attempts']]
    costs = [a['usage']['total_tokens'] for a in attempts if a['usage']['status'] == 'known']
    counts = Counter(r['status'] for r in finals)
    return {**aggregate([s for s in evaluation['scores'] if s['side'] == side]),
            'requested': len(runs), 'completed': len(successes),
            'missing': len(runs) - len(finals), 'failed': counts['failed'],
            'timed_out': counts['timed_out'], 'invalid_output': counts['invalid_output'],
            'avg_time': statistics.mean(r['duration_seconds'] for r in successes) if successes else None,
            'avg_tokens': statistics.mean(known) if known else None,
            'usage_coverage': {'known': len(known), 'completed': len(successes), 'requested': len(runs)},
            'all_attempts': {'count': len(attempts), 'known_usage_count': len(costs),
                             'known_tokens': sum(costs) if costs else None,
                             'unknown_usage_count': len(attempts) - len(costs),
                             'known_wall_seconds': sum(a.get('duration_seconds', 0) for a in attempts),
                             'unknown_duration_count': sum('duration_seconds' not in a for a in attempts)},
            'clone_seconds': sum(a.get('clone_seconds', 0) for a in attempts)}


def bootstrap(evaluation: dict, seed: int, samples: int = 1000) -> dict:
    by_side: dict[str, dict[str, list]] = {side: {} for side in SIDES}
    for score in evaluation['scores']:
        by_side[score['side']].setdefault(score['case_id'], []).append(score)
    keys = sorted(set(by_side['baseline']) & set(by_side['candidate']))
    if len(keys) < 2:
        return {'unit': 'PR', 'seed': seed, 'interval': None, 'reason': 'fewer than two paired PRs'}
    # All repeated reviewer/Judge measurements of a PR stay together in each resample.
    rng = random.Random(seed)
    deltas = []
    for _ in range(samples):
        chosen = rng.choices(keys, k=len(keys))
        values = {side: aggregate([s for key in chosen for s in by_side[side][key]]) for side in SIDES}
        deltas.append((values['candidate']['semantic_f1'] - values['baseline']['semantic_f1']) * 100)
    deltas.sort()
    return {'unit': 'PR', 'seed': seed, 'samples': samples, 'paired_prs': len(keys),
            'interval': [deltas[int(samples * .025)], deltas[int(samples * .975)]],
            'metric': 'semantic_f1_percentage_points', 'diagnostic_only': True}


def compare(root: Path, evaluation_id: str) -> dict:
    safe_path(evaluation_id)
    if '/' in evaluation_id or evaluation_id in ('.', '..'):
        raise BenchError('invalid evaluation ID')
    with locked(root):
        manifest = load(root)
        dest = root / 'evaluations' / evaluation_id
        evaluation = read(dest / 'evaluation.json')
        if (digest(encode(evaluation)) != read(dest / 'evaluation.sha256.json') or
            evaluation['manifest_hash'] != digest(encode(manifest))):
            raise BenchError('evaluation integrity or manifest mismatch')
        sides = {side: side_summary(evaluation, side) for side in SIDES}
        complete = all(s['requested'] == s['completed'] for s in sides.values())
        delta = {}
        for metric in METRICS:
            a, b = sides['baseline'][metric], sides['candidate'][metric]
            delta[metric] = {'absolute': b - a if a is not None and b is not None else None}
            if metric in ('semantic_f1', 'precision', 'recall'):
                delta[metric]['percentage_points'] = round((b - a) * 100, 6)
            else:
                delta[metric]['ratio'] = b / a if a and b is not None else None
        paired = []
        for case_id in manifest['case_ids']:
            scores = {side: [s for s in evaluation['scores'] if s['case_id'] == case_id and s['side'] == side] for side in SIDES}
            if all(scores.values()):
                values = {side: aggregate(v) for side, v in scores.items()}
                paired.append({'case_id': case_id, 'baseline': values['baseline'], 'candidate': values['candidate'],
                               'regression': values['candidate']['semantic_f1'] < values['baseline']['semantic_f1']})
        report = {'schema_version': 1, 'evaluation_id': evaluation_id, 'experiment_id': manifest['experiment_id'],
                  'smoke_only': True, 'report_only': True, 'status': 'complete' if complete else 'incomplete',
                  'conclusion': 'offline pipeline evidence only; no model quality or merge verdict',
                  **sides, 'delta': delta, 'versions': manifest['versions'], 'dataset': manifest['dataset'],
                  'reviewer': manifest['settings']['reviewer'], 'judge': evaluation['judge'],
                  'judge_usage': evaluation['judge_usage'], 'judge_seconds': evaluation['judge_seconds'],
                  'experiment_elapsed_seconds': evaluation['experiment_elapsed_seconds'],
                  'setup_seconds': manifest['setup_seconds'], 'paired': paired, 'details': evaluation['scores'],
                  'bootstrap': bootstrap(evaluation, manifest['dataset']['seed']),
                  'limitations': ['Mock output is smoke-only; real model, skill loading and all child usage need pilot validation.',
                                  'Spec uses commit subjects; original requirements are limited.',
                                  'Same-model reviewer/Judge may share bias. Tokens are not billing amounts.',
                                  'Small samples and single rounds cannot establish significance.',
                                  'Unknown usage forbids complete cost-improvement claims.',
                                  'Incomplete runs are diagnostic only; official summaries omit missing results.',
                                  'Classification unavailable in the official converted schema.']}
        lines = ['# AACR workflow comparison', '', '**smoke-only · report-only**', '',
                 f'Status: {report["status"]}. {report["conclusion"]}', '',
                 '| Metric | Baseline | Candidate | Difference |', '| --- | ---: | ---: | ---: |']
        for metric in METRICS:
            change = delta[metric].get('percentage_points', delta[metric]['absolute'])
            lines.append(f'| {metric} | {sides["baseline"][metric]} | {sides["candidate"][metric]} | {change} |')
        lines += ['', 'Quality differences are percentage points. Full ratios, usage coverage, attempts,',
                  'line metrics, matches and two-axis provenance are in report.json.', '',
                  f'Requested/completed: baseline {sides["baseline"]["requested"]}/{sides["baseline"]["completed"]}; '
                  f'candidate {sides["candidate"]["requested"]}/{sides["candidate"]["completed"]}.', '',
                  *[f'- {v}' for v in report['limitations']]]
        if not (dest / 'report.json').exists():
            write(dest / 'report.json', report)
            (dest / 'report.md').write_text('\n'.join(lines) + '\n')
        return report
