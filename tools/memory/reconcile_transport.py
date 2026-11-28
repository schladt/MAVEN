"""Service-only project request validation and transactional dispatch; no SDK credentials here."""
import re

from tools.reconcile_records import (MAX_BATCH, REDACTION_KINDS, TaxonomyError,
                                     accept_change, digest, summarize, validate_project_envelope)
from .validation import MemoryError, canonical, fields, invalid, string_list, uuid_string


TOKEN = re.compile(r'[0-9a-f]{64}\Z')
TAXONOMY_VERSION = 2


def validate_request(route, request):
    action = route.rsplit('/', 1)[-1]
    required = {'instance_id', 'recovery_epoch'}
    optional = set()
    if action != 'status':
        required.add('project_id')
    if action == 'inventory':
        optional.update(('after_id', 'expected_token'))
    elif action == 'markers':
        optional.update(('after_id', 'after_kind', 'expected_token', 'bundle_hashes'))
    elif action in ('read', 'apply'):
        required.update(('expected_token', 'ids' if action == 'read' else 'changes'))
        if action == 'apply':
            version = request.get('taxonomy_version') if isinstance(request, dict) else None
            if type(version) is not int or version != TAXONOMY_VERSION:
                raise MemoryError('client_incompatible', 409, detail=f'Update MAVEN before reconciling: apply requires '
                                  f'taxonomy_version {TAXONOMY_VERSION}. Offline records are unchanged.')
            required.add('taxonomy_version')
    fields(request, required, optional)
    for key in ('instance_id', 'recovery_epoch', 'project_id', 'after_id'):
        if key in request:
            uuid_string(request[key], name=key)
    if 'after_id' in request and 'expected_token' not in request:
        invalid('inventory continuation requires its observed token')
    if 'expected_token' in request and (not isinstance(request['expected_token'], str)
                                        or not TOKEN.fullmatch(request['expected_token'])):
        invalid('invalid cloud view token')
    if action == 'markers':
        paging = {'after_id', 'after_kind', 'expected_token'} & request.keys()
        if paging and (len(paging) != 3 or 'bundle_hashes' in request
                       or not isinstance(request['after_kind'], str) or request['after_kind'] not in REDACTION_KINDS):
            invalid('incomplete marker cursor')
        if 'bundle_hashes' in request:
            hashes = request['bundle_hashes']
            if (not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_BATCH
                    or any(not isinstance(s, str) or not TOKEN.fullmatch(s) for s in hashes)
                    or len(hashes) != len(set(hashes))):
                invalid('invalid bundle membership query')
    if action == 'read':
        string_list(request['ids'], MAX_BATCH, 36, validator=uuid_string, name='ids')
    if action == 'apply':
        changes = request['changes']
        if not isinstance(changes, list) or len(changes) > MAX_BATCH:
            invalid('invalid atomic change set size')
        ids = set()
        for change in changes:
            try:
                validate_project_envelope(change, request['project_id'], strict_current=True)
            except TaxonomyError as error:
                invalid(str(error))
            except (TypeError, ValueError):
                invalid('invalid project record or history')
            rid = change['record']['id']
            if rid in ids:
                invalid('duplicate change ID')
            ids.add(rid)


def perform(store, route, request, expected):
    from .store import route_result
    action = route.rsplit('/', 1)[-1]

    if action == 'markers':
        raw = store.db.json_scalar('maven.redaction_delivery(CAST(:request AS jsonb))',
                                   {'request': canonical(request)}, overflow_status='snapshot_incomplete')
        value, generation = store.envelope(raw, expected)
        return route_result(value.get('result')), generation

    def call(name, body, tx=None):
        raw = store.db.json_scalar('maven.reconcile_request(:action, CAST(:request AS jsonb))',
                                   {'action': name, 'request': canonical(body)}, tx,
                                   overflow_status='snapshot_incomplete')
        value, generation = store.envelope(raw, expected)
        return route_result(value.get('result')), generation

    if action != 'apply':
        result, generation = call(action, request)
        if action == 'read' and any(entry is None for entry in result['records']):
            raise MemoryError('record_unavailable', 404)
        return result, generation

    header = {key: value for key, value in request.items() if key != 'changes'}
    changes = request['changes']
    by_id = {env['record']['id']: env for env in changes if not env['record'].get('erased')}
    ids = list(by_id)
    prior_digests = {}
    retyped_tasks = []
    # There is no need to retrieve forgotten wording to propagate an erasure. Validate
    # other replacements against the exact envelopes. The same full-project token is
    # checked *again under the write lock* in apply; a separate precheck alone is unsafe.
    while ids:
        page, _ = call('prior', {**header, 'ids': ids})
        records = page['records']
        if (page['token'] != request['expected_token'] or not records or len(records) > len(ids)
                or page['remaining'] != len(ids)-len(records)):
            raise MemoryError('snapshot_incomplete', 409)
        for rid, previous in zip(ids, records):
            if previous is not None:
                if previous['record']['id'] != rid:
                    raise MemoryError('snapshot_incomplete', 409)
                if previous['record'].get('erased'):
                    raise MemoryError('erased_record', 409)
                prior_digests[rid] = digest(previous)
            try:
                accept_change(previous, by_id[rid])
            except ValueError:
                invalid('replacement must preserve prior wording and history')
            old = previous['record'] if previous is not None else {}
            new = by_id[rid]['record']
            if old.get('kind') == 'task' and new['kind'] != 'task':
                retyped_tasks.append(rid)
        ids = ids[len(records):]
    payload = [{'wire': canonical(env), 'item': summarize(env),
                'prior_digest': prior_digests.get(env['record']['id'])} for env in changes]
    with store.db.transaction(timeouts=False) as tx:
        result, generation = call('apply', {**header, 'changes': payload}, tx)
        changed = set(result['changed'])
        task_ids = {by_id[rid]['record']['data']['task_id'] for rid in changed
                    if rid in by_id and by_id[rid]['record']['kind'] == 'plan'}
        if task_ids or retyped_tasks:
            # apply has checked the caller's original token and holds the exclusive
            # project lock. Check its resulting rows before any erase fence or commit.
            query = {**header, 'expected_token': result['token'], 'task_ids': sorted(task_ids),
                     'retyped_tasks': retyped_tasks}
            raw = store.db.json_scalar('maven.reconcile_taxonomy(CAST(:request AS jsonb))',
                                       {'request': canonical(query)}, tx)
            checked, _ = store.envelope(raw, expected)
            facts = route_result(checked['result'])
            if facts['missing_task']:
                invalid('plan.task_id must name an existing task in this project')
            if facts['linked_task']:
                invalid('task kind cannot change while a plan names it in task_id')
        erased = [e['record']['id'] for e in changes if e['record'].get('erased') and e['record']['id'] in changed]
        if erased:
            from .erase_fence import record_project_erases
            record_project_erases(store.s3, store.config, request['project_id'], erased)
        if store.control() != expected:
            raise MemoryError('context_stale')
    return result, generation
