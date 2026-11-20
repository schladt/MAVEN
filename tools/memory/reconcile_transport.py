"""Service-only project request validation and transactional dispatch; no SDK credentials here."""
import re

from tools.reconcile_records import MAX_BATCH, accept_change, digest, summarize, validate_project_envelope
from .validation import MemoryError, canonical, fields, invalid, string_list, uuid_string


TOKEN = re.compile(r'[0-9a-f]{64}\Z')


def validate_request(route, request):
    action = route.rsplit('/', 1)[-1]
    required = {'instance_id', 'recovery_epoch'}
    optional = set()
    if action != 'status':
        required.add('project_id')
    if action == 'inventory':
        optional.update(('after_id', 'expected_token'))
    elif action in ('read', 'apply'):
        required.update(('expected_token', 'ids' if action == 'read' else 'changes'))
    fields(request, required, optional)
    for key in ('instance_id', 'recovery_epoch', 'project_id', 'after_id'):
        if key in request:
            uuid_string(request[key], name=key)
    if 'after_id' in request and 'expected_token' not in request:
        invalid('inventory continuation requires its observed token')
    if 'expected_token' in request and (not isinstance(request['expected_token'], str)
                                        or not TOKEN.fullmatch(request['expected_token'])):
        invalid('invalid cloud view token')
    if action == 'read':
        string_list(request['ids'], MAX_BATCH, 36, validator=uuid_string, name='ids')
    if action == 'apply':
        changes = request['changes']
        if not isinstance(changes, list) or len(changes) > MAX_BATCH:
            invalid('invalid atomic change set size')
        ids = set()
        for change in changes:
            try:
                validate_project_envelope(change, request['project_id'])
            except (TypeError, ValueError):
                invalid('invalid project record or history')
            rid = change['record']['id']
            if rid in ids:
                invalid('duplicate change ID')
            ids.add(rid)


def perform(store, route, request, expected):
    from .store import route_result
    action = route.rsplit('/', 1)[-1]

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
        ids = ids[len(records):]
    payload = [{'wire': canonical(env), 'item': summarize(env),
                'prior_digest': prior_digests.get(env['record']['id'])} for env in changes]
    with store.db.transaction(timeouts=False) as tx:
        result, generation = call('apply', {**header, 'changes': payload}, tx)
        if store.control() != expected:
            raise MemoryError('context_stale')
    return result, generation
