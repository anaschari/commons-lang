#!/usr/bin/env python3
"""Validate the v2 Commons Lang shadow-pilot JSONL contract; Python standard library only."""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re

FEATURES = ('last1 last5 last20 n rate sincefailure transitions decisionage c_est '
            'history_missing lastfailure_missing last_execution_missing duration_missing').split()
COSTS = 'acquisition features refit_selection_calibration inference ordering explanations finalization'.split()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def number(value, name):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            f'{name}: expected a finite nonnegative number')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'invalid SHA-256')


def ids(items, expected=None):
    require(isinstance(items, list) and all(isinstance(x, str) and x.strip() for x in items),
            'IDs must be nonempty strings in a list')
    require(len(set(items)) == len(items), 'duplicate ID in inventory/order/reference list')
    if expected is not None:
        require(set(items) == set(expected), 'missing/extra inventory IDs')
    return items


def evidence(value):
    require(isinstance(value, str) and bool(value.strip()), 'missing evidence reference')


def release(row):
    """Unknown publication stays unknown; never replace it by start + duration."""
    published = row['published_at']
    ingested = number(row['ingested_at'], 'ingested_at')
    require(row.get('finished_at', row.get('diagnosed_at')) <= ingested <= row['recorded_at'],
            'completion/ingestion/record inversion')
    evidence(row['ingestion_evidence'])
    if published is None:
        require(row['publication_evidence'] is None and row['available_at'] is None,
                'unknown publication must have unknown availability')
        evidence(row['publication_missing_reason'])
        return None
    published = number(published, 'published_at')
    available = number(row['available_at'], 'available_at')
    require(published <= ingested <= row['recorded_at'], 'publication/ingestion/record inversion')
    require(available == max(published, ingested), 'availability must be actual publication/ingestion')
    evidence(row['publication_evidence'])
    return available


def validate(rows):
    decisions, results, diagnoses, completed, seen, runs = {}, {}, {}, set(), set(), set()
    previous_recorded = 0
    synthetic = None
    for line, row in enumerate(rows, 1):
        require(isinstance(row, dict), f'line {line}: expected an event object')
        try:
            require(type(row['schema_version']) is int and row['schema_version'] == 2, 'unsupported schema_version')
            require(type(row['synthetic']) is bool, 'synthetic must be a boolean')
            if synthetic is None:
                synthetic = row['synthetic']
            require(row['synthetic'] == synthetic, 'mixed synthetic and collected records')
            evidence(row['id'])
            require(row['id'] not in seen, 'duplicate event ID')
            recorded = number(row['recorded_at'], 'recorded_at')
            require(recorded >= previous_recorded, 'file is not append chronological')
            previous_recorded = recorded
            kind = row['type']
            if kind == 'decision':
                for key in ('project', 'environment', 'commit', 'run_id', 'inventory_evidence', 'commit_evidence'):
                    evidence(row[key])
                require(row['run_id'] not in runs, 'retried/duplicate run needs a distinct run_id')
                runs.add(row['run_id'])
                d = number(row['decision_at'], 'decision_at')
                start = number(row['preparation_started_at'], 'preparation_started_at')
                inventory_at = number(row['inventory_captured_at'], 'inventory_captured_at')
                commit_at = number(row['commit_captured_at'], 'commit_captured_at')
                frozen = number(row['features_frozen_at'], 'features_frozen_at')
                require(inventory_at <= frozen <= d <= recorded and commit_at <= frozen and start <= d,
                        'inventory/commit/features must be captured before the decision')
                inventory = ids(row['inventory'])
                require(inventory, 'empty inventory')
                require(row['inventory_sha256'] == digest(inventory), 'inventory hash mismatch')
                for value in row['frozen_hashes'].values():
                    sha(value)
                require(set(row['frozen_hashes']) == {'collector', 'config', 'environment'}, 'missing frozen hashes')
                planned = row['chosen_ci_order']
                if planned is None:
                    evidence(row['chosen_ci_order_missing_reason'])
                else:
                    ids(planned, inventory)
                resource = row['resources']
                require(type(resource['runner_slots']) is int and resource['runner_slots'] > 0, 'invalid slots')
                evidence(resource['description'])
                dependencies = resource['dependencies']
                require(isinstance(dependencies, list), 'dependencies must be a list')
                position = {test: i for i, test in enumerate(planned if planned is not None else inventory)}
                for edge in dependencies:
                    require(isinstance(edge, list) and len(edge) == 2 and all(t in position for t in edge),
                            'dependency outside inventory')
                    require(planned is None or position[edge[0]] < position[edge[1]], 'chosen order violates dependency')
                require(row['mode'] == 'shadow' and row['ci_order_changed'] is False,
                        'this contract permits shadow measurement only')
                require(row['feature_target'] in ('raw_failed', 'reproduced_under_protocol'), 'unknown feature target')
                ids([x['test_id'] for x in row['features']], inventory)
                require(row['features_sha256'] == digest(row['features']), 'feature snapshot hash mismatch')
                def available_reference(ref, cutoff, target):
                    source = target.get(ref)
                    require(source is not None, 'unknown or future history reference')
                    original = decisions[source['decision_id']]
                    require((original['project'], original['environment']) == (row['project'], row['environment']),
                            'history crosses project/environment')
                    require(original['decision_at'] < d, 'current/future outcome in history')
                    require(source['available_at'] is not None and source['available_at'] <= cutoff,
                            'history lacks real publication or was unavailable at cutoff')
                    return source
                for feature in row['features']:
                    require(set(feature['values']) == set(FEATURES), 'feature names differ from frozen model')
                    for name, value in feature['values'].items():
                        if value is not None:
                            number(value, name)
                            if name == 'rate':
                                require(value <= 1, 'failure rate above one')
                            if name.endswith('_missing'):
                                require(value in (0, 1), 'missing indicator must be binary')
                    for ref in ids(feature['history_result_ids']):
                        source = available_reference(ref, frozen, results)
                        require(source['test_id'] == feature['test_id'], 'history test identity mismatch')
                    for ref in ids(feature['history_diagnosis_ids']):
                        source = available_reference(ref, frozen, diagnoses)
                        require(results[source['result_id']]['test_id'] == feature['test_id'], 'diagnosis test mismatch')
                    require(row['feature_target'] != 'reproduced_under_protocol' or
                            not feature['history_result_ids'] or feature['history_diagnosis_ids'],
                            'diagnosed history cannot silently substitute raw labels')
                require(row['policies'], 'no shadow policy snapshot')
                panel = ids([p['policy_id'] for p in row['policies']])
                require('B5' in panel and any(p == 'LOGIT' or p.startswith('LOGIT-') for p in panel),
                        'missing principal B5/LOGIT comparison snapshot')
                for policy in row['policies']:
                    ids(policy['order'], inventory)
                    ids([x['test_id'] for x in policy['scores']], inventory)
                    for score in policy['scores']:
                        number(score['score'], 'policy score')
                    scored = number(policy['scored_at'], 'scored_at')
                    require(frozen <= scored <= d, 'policy was not frozen by decision')
                    for key in ('code_sha256', 'config_sha256'):
                        sha(policy[key])
                    if policy['model_sha256'] is not None:
                        sha(policy['model_sha256'])
                    costs = policy['costs_s']
                    require(set(costs) == set(COSTS + ['total']), 'missing cost component')
                    for key, value in costs.items():
                        number(value, f'cost {key}')
                    require(costs['total'] + 1e-9 >= sum(costs[k] for k in COSTS), 'total omits component cost')
                    require(costs['total'] <= d - start + 1e-9, 'cost exceeds observed collection wall interval')
                    reserve=number(policy.get('callback_tail_reserved_s',0), 'callback tail reserve')
                    charged=number(policy.get('preparation_charged_s',costs['total']+reserve), 'charged preparation')
                    require(math.isclose(charged,costs['total']+reserve,abs_tol=1e-8), 'charged preparation omits measured cost or reserve')
                    cutoff = number(policy['training_cutoff_at'], 'training_cutoff_at')
                    require(cutoff <= scored, 'future training cutoff')
                    for train in policy['training_inputs']:
                        prior = decisions.get(train['decision_id'])
                        require(prior is not None and prior['decision_at'] < d, 'unknown/current training decision')
                        require(prior['feature_target'] == row['feature_target'], 'training crosses target definitions')
                        require(prior['features_sha256'] == train['features_sha256'], 'training rewrites earlier features')
                        target = results if row['feature_target'] == 'raw_failed' else diagnoses
                        for ref in ids(train['label_ids']):
                            label = available_reference(ref, cutoff, target)
                            require(label['decision_id'] == train['decision_id'], 'training label/decision mismatch')
                decisions[row['id']] = row
            elif kind in ('result', 'diagnosis', 'suite_end'):
                decision = decisions.get(row['decision_id'])
                require(decision is not None, 'event has unknown/future decision')
                require(row['commit'] == decision['commit'] and row['run_id'] == decision['run_id'],
                        'run/commit identity mismatch')
                if kind == 'result':
                    require(row['decision_id'] not in completed, 'primary result appended after suite closure')
                    require(row['test_id'] in decision['inventory'], 'result outside frozen inventory')
                    require(not any(x['decision_id'] == row['decision_id'] and x['test_id'] == row['test_id']
                                    for x in results.values()), 'duplicate primary test result; reruns belong in diagnosis')
                    started = row['started_at']
                    finished = number(row['finished_at'], 'finished_at')
                    elapsed = row['duration_s']
                    if started is None:
                        require(row['raw_outcome']=='skipped' and elapsed is None and row['slot'] is None, 'only unexecuted skipped class has unknown runtime')
                        require(decision['decision_at'] <= finished <= recorded, 'skip time inversion')
                    else:
                        number(started, 'started_at'); number(elapsed, 'duration_s')
                        require(decision['decision_at'] <= started <= finished <= recorded, 'test time inversion')
                        require(math.isclose(elapsed, finished - started, abs_tol=1e-6), 'duration/timestamps disagree')
                    require(row['raw_outcome'] in ('passed', 'failed', 'error', 'timed_out', 'skipped', 'aborted'), 'invalid raw outcome')
                    require(started is None or (type(row['slot']) is int and 1 <= row['slot'] <= decision['resources']['runner_slots']), 'invalid slot')
                    released = release(row)
                    require(released is None or finished <= row['published_at'], 'published before test finished')
                    evidence(row['result_evidence'])
                    results[row['id']] = row
                elif kind == 'diagnosis':
                    result = results.get(row['result_id'])
                    require(result is not None and result['decision_id'] == row['decision_id'], 'diagnosis/result mismatch')
                    require(result['raw_outcome'] == 'failed', 'diagnosis is for a raw failure')
                    require(row['status'] in ('reproduced_under_protocol', 'unstable_observed', 'inconclusive', 'not_run'), 'invalid diagnosis status')
                    diagnosed = number(row['diagnosed_at'], 'diagnosed_at')
                    require(result['finished_at'] <= diagnosed <= recorded, 'diagnosis time inversion')
                    released = release(row)
                    require(released is None or diagnosed <= row['published_at'], 'diagnosis published before completion')
                    evidence(row['cause_evidence'])
                    if row['protocol_sha256'] is not None:
                        sha(row['protocol_sha256'])
                    ids([x['attempt_id'] for x in row['reruns']])
                    for attempt in row['reruns']:
                        require(attempt['commit'] == decision['commit'], 'rerun on a different commit')
                        sha(attempt['environment_sha256'])
                        for key in ('independence_evidence', 'environment_comparison', 'result_evidence'):
                            evidence(attempt[key])
                        require(type(attempt['independent_attempt']) is bool, 'rerun independence must be explicit')
                        started = number(attempt['started_at'], 'rerun started_at')
                        finished = number(attempt['finished_at'], 'rerun finished_at')
                        elapsed = number(attempt['duration_s'], 'rerun duration_s')
                        require(result['finished_at'] <= started <= finished <= diagnosed, 'rerun time inversion')
                        require(math.isclose(elapsed, finished-started, abs_tol=1e-6), 'rerun duration mismatch')
                        require(attempt['raw_outcome'] in ('passed', 'failed', 'error', 'timed_out'), 'invalid rerun outcome')
                        require(type(attempt['same_failure_cause']) is bool, 'cause comparison must be explicit')
                    if row['status'] == 'reproduced_under_protocol':
                        require(row['protocol_sha256'] is not None and any(
                            x['raw_outcome'] == 'failed' and x['same_failure_cause'] and x['independent_attempt']
                            for x in row['reruns']), 'reproduced label lacks a frozen protocol and independent matching evidence')
                    if row['status'] == 'not_run':
                        require(not row['reruns'], 'not_run diagnosis contains reruns')
                    diagnoses[row['id']] = row
                else:
                    require(row['decision_id'] not in completed, 'duplicate suite_end')
                    selected = [x for x in results.values() if x['decision_id'] == row['decision_id']]
                    ids(row['result_ids'], [x['id'] for x in selected])
                    require(selected, 'suite_end without observed results')
                    end = number(row['finished_at'], 'suite finished_at')
                    require(max(x['finished_at'] for x in selected) <= end <= recorded, 'suite end inversion')
                    executed = [x for x in selected if x['started_at'] is not None]
                    ids(row['actual_start_order'], [x['test_id'] for x in executed])
                    expected = [x['test_id'] for x in sorted(executed, key=lambda x: (x['started_at'], x['test_id']))]
                    require(row['actual_start_order'] == expected, 'actual order disagrees with start timestamps')
                    require(row['status'] in ('complete', 'incomplete'), 'invalid suite status')
                    if row['status'] == 'complete':
                        ids([x['test_id'] for x in selected], decision['inventory'])
                        require(all(x['raw_outcome'] in ('passed','failed') for x in selected), 'skipped test prevents complete oracle')
                    else:
                        evidence(row['incomplete_reason'])
                    require(row['normal_ci_full_suite'] is True, 'full normal CI stream must remain enabled')
                    evidence(row['execution_deviations'])
                    by_test = {x['test_id']: x for x in selected}
                    for before, after in decision['resources']['dependencies']:
                        if before in by_test and after in by_test and by_test[before]['started_at'] is not None and by_test[after]['started_at'] is not None:
                            require(by_test[before]['finished_at'] <= by_test[after]['started_at'], 'actual dependency violation')
                    for slot in range(1, decision['resources']['runner_slots']+1):
                        lane = sorted((x for x in selected if x['slot'] == slot and x['started_at'] is not None), key=lambda x: x['started_at'])
                        require(all(a['finished_at'] <= b['started_at'] for a, b in zip(lane, lane[1:])), 'overlapping tests in one slot')
                    completed.add(row['decision_id'])
            else:
                raise ValueError('unknown event type')
            seen.add(row['id'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'line {line} ({row.get("id", "?")}): {exc}') from exc
    require(decisions and set(decisions) == completed, 'missing suite_end for a decision')
    return {'events': len(rows), 'decisions': len(decisions), 'synthetic': synthetic,
            'complete_suites': sum(x['type'] == 'suite_end' and x['status'] == 'complete' for x in rows)}


def read(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'duplicate JSON key {key}')
            result[key] = value
        return result
    return [json.loads(line, object_pairs_hook=unique_object) for line in Path(path).read_text().splitlines() if line.strip()]


def self_check():
    rows = read(Path(__file__).with_name('synthetic-example.jsonl'))
    validate(rows)
    mutants = []
    def mutant(change):
        value = copy.deepcopy(rows)
        change(value)
        mutants.append(value)
    mutant(lambda x: x[0]['inventory'].append(x[0]['inventory'][0]))
    mutant(lambda x: x[0]['chosen_ci_order'].pop())
    mutant(lambda x: x[1].update(duration_s=-1))
    mutant(lambda x: x[1].update(started_at=float('nan')))
    mutant(lambda x: x[1].update(published_at=x[1]['finished_at']-1))
    mutant(lambda x: x[1].update(commit='another-commit'))
    mutant(lambda x: x[1].update(run_id='another-run'))
    mutant(lambda x: x[1].update(test_id='not-in-inventory'))
    mutant(lambda x: x[1].update(available_at=x[1]['available_at']+1))
    mutant(lambda x: x[1].update(published_at=None, publication_evidence=None, available_at=None,
                                publication_missing_reason='not captured', ingested_at=x[1]['recorded_at']+1))
    mutant(lambda x: x[1].update(published_at=None, publication_evidence=None, available_at=None,
                                publication_missing_reason='not captured', ingested_at=x[1]['finished_at']-1))
    mutant(lambda x: x[0]['policies'][0]['costs_s'].update(inference=float('inf')))
    mutant(lambda x: x[0]['policies'][0]['costs_s'].update(total=0))
    mutant(lambda x: x[5]['policies'].pop(0))
    mutant(lambda x: x[-1]['actual_start_order'].reverse())
    mutant(lambda x: x[4]['reruns'][0].update(commit='different-commit'))
    mutant(lambda x: x[4].update(status='reproduced_under_protocol'))
    mutant(lambda x: x[0].update(synthetic=False))
    mutant(lambda x: x.pop(-1))
    # Keep the snapshot hash valid: reject the leaking support itself, not just a bad hash.
    def leak(value):
        decision = value[5]
        decision['features'][0]['history_result_ids'] = ['result-B-alpha']
        decision['features_sha256'] = digest(decision['features'])
    mutant(leak)
    def unknown_publication(value):
        value[1].update(published_at=None, publication_evidence=None, available_at=None,
                        publication_missing_reason='not captured')
    mutant(unknown_publication)
    def late_diagnosis(value):
        value[5]['features_frozen_at'] = value[4]['available_at'] - 1
    mutant(late_diagnosis)
    def cross_target(value):
        value[5]['feature_target'] = 'reproduced_under_protocol'
        value[5]['features'][1]['history_result_ids'] = []
        value[5]['features_sha256'] = digest(value[5]['features'])
        value[5]['policies'][0]['training_inputs'] = [{
            'decision_id': 'decision-A', 'features_sha256': value[0]['features_sha256'],
            'label_ids': ['diagnosis-A-alpha']}]
    mutant(cross_target)
    def result_after_close(value):
        late = value.pop(2)
        value[2].update(status='incomplete', incomplete_reason='beta still pending',
                        result_ids=['result-A-alpha'], actual_start_order=['suite.AlphaTest'])
        late['recorded_at'] = 136
        value.insert(3, late)
    mutant(result_after_close)
    for index, value in enumerate(mutants, 1):
        try:
            validate(value)
        except ValueError:
            continue
        raise AssertionError(f'mutant {index} was accepted')
    print(f'PASS: synthetic example and {len(mutants)} rejected mutants')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?')
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        elif args.path:
            print(json.dumps(validate(read(args.path)), sort_keys=True))
        else:
            parser.error('provide JSONL path or --self-check')
    except (ValueError, AssertionError) as error:
        parser.exit(1, f'INVALID: {error}\n')
