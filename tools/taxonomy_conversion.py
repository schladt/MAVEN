"""Convert a bound project workspace or a private SQLite copy without syncing."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import sys
from uuid import UUID, uuid5

from tools.memory.boundary import BoundaryError
from tools.memory.client import MemoryClientError
from tools.memory.reconcile_transport import TAXONOMY_VERSION
from tools.memory.validation import request_limit
from tools.project import ProjectError
from tools.reconcile_local import LocalStore, StaleView
from tools.reconcile_records import (
    KINDS, MAX_BATCH, V2_KINDS, TaxonomyError, brief_id, canonical,
    make_change, record_uuid, tombstone, validate_current_data, validate_project_envelope,
)
from tools.setup_common import SetupPending
from tools.turn_memory import LocalApplication


class ConversionError(ValueError):
    """Only fixed diagnostics, row numbers, canonical IDs and counts belong here."""


def _id(value):
    try:
        return record_uuid(value)
    except ValueError:
        raise ConversionError('id must be a canonical UUID') from None


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() <= required.union(optional):
        raise ConversionError('expected fields: ' + ', '.join(sorted(required)) +
                              ('; optional: ' + ', '.join(sorted(optional)) if optional else ''))


def _snapshot(store, expected_token=None):
    view = store.inventory()
    token = view['token']
    if expected_token is not None and token != expected_token:
        raise StaleView('local project changed; inspect the current inventory')
    ids = [item['id'] for item in view['items']]
    records = {}
    for start in range(0, len(ids), MAX_BATCH):
        page = ids[start:start + MAX_BATCH]
        for key, env in zip(page, store.read(page, token), strict=True):
            if env['record']['id'] != key or key in records:
                raise ConversionError('stored record IDs do not match the project inventory')
            records[key] = env
    return token, records


def _brief_reference_links(record, references):
    # Recognize record pointers only, not prose, paths, URLs, or the read_first list.
    entries = record.get('data', {}).get('consult_when_relevant')
    if not isinstance(entries, list):
        return set()
    links = set()
    for entry in entries:
        values = (entry.get(k) for k in ('id', 'record_id', 'link')) if isinstance(entry, dict) else [entry]
        for value in values:
            if isinstance(value, str) and value.removeprefix('record:') in references:
                links.add(value.removeprefix('record:'))
    return links


def scan(store):
    token, records = _snapshot(store)
    current = {key: env['record'] for key, env in records.items()}
    references = {key for key, rec in current.items() if rec.get('kind') == 'reference'}
    findings = []
    for key, env in records.items():
        rec = env['record']
        kind = rec.get('kind')
        issues = []
        try:
            validate_project_envelope(env, store.project_id, strict_current=True)
        except TaxonomyError as exc:
            issues.append(str(exc))
        except (ValueError, TypeError, OverflowError, RecursionError):
            issues.append('invalid stored envelope, history, or credential material')
        if kind == 'plan':
            target = rec['data'].get('task_id')
            if isinstance(target, str) and current.get(target, {}).get('kind') != 'task':
                issues.append('plan.task_id must name an existing task in this project')
        if kind == 'brief' and _brief_reference_links(rec, references):
            issues.append('brief.consult_when_relevant must not list reference records')
        if issues:
            findings.append({'id': key, 'kind': kind if kind in (KINDS | V2_KINDS) else 'invalid',
                             'issues': issues})
    return {'project_id': store.project_id, 'token': token, 'total': len(records), 'findings': findings}


def split_id(source, key):
    return str(uuid5(UUID(_id(source)), 'taxonomy-split:' + key))


def _linked_child(source, child):
    kind, data = child['kind'], child['data']
    validate_current_data(kind, data)
    # Keep closed schemas closed; these links count toward the normal field limit.
    if kind in ('reference', 'skill'):
        field = 'body' if kind == 'reference' else 'when_to_use'
        suffix = ('\n\n' if kind == 'reference' else ' ') + 'Source record: ' + source
        return {**data, field: data[field] + suffix}
    if 'source_id' in data and data['source_id'] != source:
        raise ConversionError('child source_id conflicts with its split source')
    return {**data, 'source_id': source}


@dataclass(frozen=True)
class Target:
    row: int
    role: str
    kind: str | None
    data: dict | None


def _erase_id(action):
    if not isinstance(action, str) or not action.startswith('ERASE '):
        raise ConversionError('erasure must be exactly ERASE <ID>')
    return _id(action[6:])


def _targets(mapping, project_id):
    _fields(mapping, {'project_id', 'rows'})
    if _id(mapping['project_id']) != project_id:
        raise ConversionError('mapping project_id does not match selected project')
    if not isinstance(mapping['rows'], list):
        raise ConversionError('rows must be an array')
    targets, survivors = {}, {}

    def add(key, role, kind, data):
        if kind == 'profile':
            raise ConversionError('profile is not authored by conversion; use the configured Home')
        if kind is not None:
            validate_current_data(kind, data)
        elif role != 'erase':
            raise ConversionError('kind must name a strict current record kind')
        if key in targets:
            prev = targets[key]
            if (role == prev.role == 'survivor' and kind == prev.kind
                    and canonical(data) == canonical(prev.data)):
                return
            raise ConversionError(f'conflicting or duplicate destination ID {key}')
        targets[key] = Target(number, role, kind, data)

    for number, row in enumerate(mapping['rows'], 1):
        try:
            if isinstance(row, str):
                add(_erase_id(row), 'erase', None, None)
                continue
            op = row.get('op') if isinstance(row, dict) else None
            if op in ('retype', 'split'):
                _fields(row, {'op', 'id', 'kind', 'data'} | ({'children'} if op == 'split' else set()))
                source = _id(row['id'])
                add(source, 'source', row['kind'], row['data'])
                if op == 'split':
                    if not isinstance(row['children'], list) or not row['children']:
                        raise ConversionError('split.children must be a nonempty array')
                    for child in row['children']:
                        _fields(child, {'key', 'kind', 'data'})
                        key = child['key']
                        if not isinstance(key, str) or not key.strip():
                            raise ConversionError('child key must be nonempty text')
                        add(split_id(source, key), 'child', child['kind'], _linked_child(source, child))
            elif op == 'merge':
                _fields(row, {'op', 'id', 'survivor'}, {'source'})
                source = _id(row['id'])
                dest = row['survivor']
                _fields(dest, {'id', 'kind', 'data'})
                survivor = _id(dest['id'])
                add(survivor, 'survivor', dest['kind'], dest['data'])
                disposition = row.get('source', {'kind': 'entity', 'data': {
                    'type': 'cross-reference', 'name': 'Merged record', 'state': 'retired', 'survivor_id': survivor}})
                if isinstance(disposition, str):
                    if _erase_id(disposition) != source:
                        raise ConversionError('merge erasure must name its exact source ID')
                    add(source, 'erase', None, None)
                else:
                    _fields(disposition, {'kind', 'data'})
                    add(source, 'source', disposition['kind'], disposition['data'])
                survivors[source] = survivor
            else:
                raise ConversionError('expected retype, split, merge, or literal ERASE <ID>')
        except (ConversionError, TaxonomyError) as exc:
            raise ConversionError(f'row {number}: {exc}') from None
    return targets, survivors


def _changes(store, records, targets):
    changes, errors = {}, []
    for key, target in targets.items():
        try:
            prior = records.get(key)
            if prior is None and target.role != 'child':
                raise ConversionError('existing record required')
            if target.role == 'erase':
                change = tombstone(prior, record_id=key)
            else:
                if prior and prior['record'].get('erased'):
                    raise ConversionError('an erased record cannot be restored')
                change = make_change(prior, target.kind, target.data, 'Taxonomy conversion', record_id=key)
                if prior is not None and change is not prior:
                    # A previous run already installed this payload and a writer subsequently
                    # replaced it. Never replay the old mapping over that writer's version.
                    desired = canonical(target.data)
                    if any(entry['record']['kind'] == target.kind and canonical(entry['record']['data']) == desired
                           for entry in prior['history']):
                        raise ConversionError('mapped data is historical; review the intervening edit')
                if target.role == 'child' and prior is not None and change is not prior:
                    raise ConversionError('split child ID exists with different content; not overwriting it')
            validate_project_envelope(change, store.project_id, strict_current=True)
            if change is not prior:
                changes[key] = change
        except (ConversionError, TaxonomyError) as exc:
            errors.append(f'row {target.row}, {key}: {exc}')
        except (ValueError, TypeError, OverflowError, RecursionError):
            errors.append(f'row {target.row}, {key}: invalid envelope, history, or credential material')
    if errors:
        raise ConversionError('; '.join(errors))
    return changes


def _dependencies(project_id, records, changes, survivors):
    old = {key: env['record'] for key, env in records.items()}
    final = {**old, **{key: env['record'] for key, env in changes.items()}}
    needs = {key: set() for key in changes}
    for source, survivor in survivors.items():
        if source in changes and survivor in changes:
            needs[source].add(survivor)
        if source not in changes and old[source].get('erased') and survivor in changes:
            raise ConversionError(f'{source}: source already erased; not overwriting a different survivor')
    for key in changes:
        new = final[key]
        if new.get('kind') == 'plan':
            task = new['data']['task_id']
            if final.get(task, {}).get('kind') != 'task':
                raise ConversionError(f'{key}: plan.task_id must name an existing task in this project')
            if old.get(task, {}).get('kind') != 'task':
                needs[key].add(task)
    for plan, rec in old.items():
        if rec.get('kind') == 'plan':
            task = rec['data'].get('task_id')
            if (isinstance(task, str) and task in changes and old.get(task, {}).get('kind') == 'task'
                    and final[task].get('kind') not in (None, 'task')):
                if final[plan].get('kind') == 'plan' and final[plan]['data'].get('task_id') == task:
                    raise ConversionError(f'{task}: task kind cannot change while plan {plan} names it')
                needs[task].add(plan)
    brief = brief_id(project_id)
    new_refs = {key for key in changes if final[key].get('kind') == 'reference'}
    refs = {key for key, rec in final.items() if rec.get('kind') == 'reference'} if brief in changes else new_refs
    if _brief_reference_links(final.get(brief, {}), refs):
        raise ConversionError(f'{brief}: include corrected brief.consult_when_relevant without reference pointers')
    return needs


def _batches(store, records, changes, survivors):
    needs = _dependencies(store.project_id, records, changes, survivors)
    # Measure UTF-8 transport envelopes including commas and the full apply header.
    header = {'project_id': store.project_id, 'instance_id': store.project_id, 'recovery_epoch': store.project_id,
              'expected_token': '0' * 64, 'taxonomy_version': TAXONOMY_VERSION, 'changes': []}
    capacity = request_limit('/v1/reconcile/apply') - len(canonical(header).encode()) + 1
    size = {k: len(canonical(env).encode()) + 1 for k, env in changes.items()}

    def prerequisites(keys, pending):
        result, stack = set(), list(keys)
        while stack:
            key = stack.pop()
            if key in pending and key not in result:
                result.add(key)
                stack.extend(needs[key])
        return result

    remaining = dict.fromkeys(changes)
    batches, batch, used = [], [], 0
    while remaining:
        # Most records are independent or can follow a predecessor in an earlier iteration.
        group = next(([k] for k in remaining if not (needs[k] & remaining.keys())), None)
        if group is None:
            # A task retype and erasure of its linked plan can depend on each other.
            # Commit the cycle together, never an invalid intermediate state.
            for key in remaining:
                closure = prerequisites([key], remaining)
                if len(closure) <= MAX_BATCH and sum(size[k] for k in closure) <= capacity:
                    # Insertion order places each merge survivor before all of its sources.
                    group = [k for k in changes if k in closure]
                    break
            if group is None:
                raise ConversionError('dependent changes cannot fit within the transaction count and byte limits')
        weight = sum(size[k] for k in group)
        if len(batch) + len(group) > MAX_BATCH or used + weight > capacity:
            batches.append(batch)
            batch, used = [], 0
        batch.extend(group)
        used += weight
        for k in group:
            del remaining[k]
    if batch:
        batches.append(batch)
    return batches


def apply_mapping(store, mapping, *, dry_run=False, expected_token=None):
    targets, survivors = _targets(mapping, store.project_id)
    token, records = _snapshot(store, expected_token)
    changes = _changes(store, records, targets)
    batches = _batches(store, records, changes, survivors)
    store.read([], token)  # Guard dry runs and no-op retries too.
    if not dry_run:
        for batch in batches:
            result = store.apply([changes[key] for key in batch], token, strict_current=True)
            token = result['token']  # Never refresh inventory to paper over an interleaved writer.
    return {'project_id': store.project_id, 'token': token, 'dry_run': dry_run, 'batches': len(batches),
            'changes': list(changes), 'unchanged': [k for k in targets if k not in changes]}


def _json_mapping(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConversionError('mapping repeats a JSON field')
            result[key] = value
        return result

    def nonfinite(_):
        raise ConversionError('mapping contains a non-finite number')

    with path.open(encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=unique, parse_constant=nonfinite)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--database', type=Path, help='existing database copy in a private directory')
    target.add_argument('--workspace', type=Path, help='directory in the bound project to convert locally')
    parser.add_argument('--project', required=True, help='UUID of the selected project')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('scan', help='list IDs and structural issues, never record contents')
    commands.add_parser('check', help='scan and exit 1 if any current record fails')
    apply = commands.add_parser('apply', help='preflight and apply a JSON mapping')
    apply.add_argument('mapping', type=Path)
    apply.add_argument('--expected-token', help='ordinary local-store stale-write guard')
    apply.add_argument('--dry-run', action='store_true', help='report the same preflight without writes')
    args = parser.parse_args(argv)
    try:
        project = _id(args.project)
        selected = (LocalApplication().local(args.workspace.expanduser().absolute(), project_id=project)
                    if args.workspace is not None else nullcontext(LocalStore.open_copy(args.database, project)))
        with selected as store:
            if args.command == 'apply':
                report = apply_mapping(store, _json_mapping(args.mapping), dry_run=args.dry_run,
                                       expected_token=args.expected_token)
            else:
                report = scan(store)
        print(json.dumps(report, sort_keys=True))
        return int(args.command == 'check' and bool(report['findings']))
    except (StaleView, ConversionError, TaxonomyError) as exc:
        message = str(exc)
    except (ProjectError, SetupPending, BoundaryError, MemoryClientError) as exc:
        message = f'{exc}. Check the selected workspace, its binding and installation before retrying.'
    except json.JSONDecodeError as exc:
        message = f'mapping is not valid JSON at line {exc.lineno}, column {exc.colno}'
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        message = ('invalid mapping or unavailable project store; check JSON, project UUID, '
                   'file permissions and existing store schema')
    print(json.dumps({'error': message}), file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
