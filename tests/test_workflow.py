"""Shared versioned cloud fixture for tests of separately governed operations."""
from tools.memory.client import MemoryClientError


class MemoryCloud:
    """Deterministic cloud fixture with versioned records and exact request receipts."""
    def __init__(self):
        self.records, self.history, self.requests = {}, {}, {}
        self.calls = []
        self.context_loads = 0
        self.generation = 1
        self.last_generation = None
        self.sequence = {}
        self.stale = set()  # Session IDs the fixture reports as stale peers.
        self.lose_next = False
        self.closed = False

    def close(self):
        self.closed = True

    def load_context(self, scope):
        self.context_loads += 1
        self.last_generation = self.generation
        return {'status': 'ready', 'authority_generation': self.generation, 'data': {
            'authority_generation': self.generation, 'instructions': [
                {'slug': 'core', 'revision': 1, 'content_hash': 'a' * 64,
                 'page_kind': 'directive', 'body_markdown': 'Complete current governing instructions.'}]}}

    def invoke(self, route, body):
        response = self._invoke(route, body)
        self.last_generation = self.generation
        return {**response, 'authority_generation': self.generation}

    @staticmethod
    def _owner(record):
        return record.get('data_scope') == 'owner'

    def _project_context(self, body):
        import json as _json
        project, requester = body['project_id'], body['session_id']
        sessions = {identifier: record for identifier, record in self.records.items()
                    if record['kind'] == 'session' and record.get('project_id') == project}
        checkpoints = sorted((record for record in self.records.values()
                              if record['kind'] == 'checkpoint' and record['data'].get('session_id') in sessions),
                             key=lambda record: self.sequence.get(record['record_id'], 0))
        latest = {}
        for record in checkpoints:
            latest[record['data']['session_id']] = record
        handover = None
        if checkpoints:
            record = checkpoints[-1]
            handover = {'session_id': record['data']['session_id'], 'checkpoint_record_id': record['record_id'],
                        'revision': record['revision'], 'recorded_at': None,
                        'session_state': sessions[record['data']['session_id']]['data']['state'],
                        'summary': record['data'].get('summary')}
        peers = [{'session_id': identifier,
                  'harness': _json.loads(record['data']['summary']).get('harness'), 'started_at': None,
                  'latest_checkpoint_at': None,
                  'latest_summary': latest[identifier]['data'].get('summary') if identifier in latest else None,
                  'machine': _json.loads(record['data']['summary']).get('machine'), 'last_activity': None,
                  'stale': identifier in self.stale, 'revision': record['revision']}
                 for identifier, record in sessions.items()
                 if identifier != requester and record['data']['state'] == 'active']

        def items(kind, states):
            return [{'record_id': record['record_id'], 'revision': record['revision'],
                     'title': record['data'].get('title'), 'state': record['data'].get('state'),
                     'tags': record.get('tags', [])}
                    for record in sorted(self.records.values(), key=lambda value: value['record_id'])
                    if record['kind'] == kind and self._owner(record) and record['data'].get('state') in states
                    and record.get('project_id') in (None, project)]

        preferences = [{'record_id': record['record_id'], 'revision': record['revision'],
                        'title': record['data']['title'], 'body_markdown': record['data']['body_markdown'][:1000]}
                       for record in self.records.values()
                       if record['kind'] == 'decision' and self._owner(record) and record.get('project_id') is None
                       and record['data'].get('state') == 'accepted'][:20]
        return {'latest_handover': handover, 'active_peers': peers,
                'tasks': items('task', ('open', 'in_progress', 'blocked')), 'blockers': items('risk', ('open',)),
                'owner_preferences': preferences}

    def _search(self, body):
        import copy
        import json as _json
        import re as _re
        records = [record for record in self.records.values()
                   if (body.get('kind') is None or record['kind'] == body['kind'])
                   and (body.get('state') is None or record['data'].get('state') == body['state'])
                   and (body.get('include_archived') or ('archived' not in record.get('tags', [])
                                                         and record['data'].get('state') != 'archived'))]
        scope = body.get('scope', 'project')
        records = [record for record in records if scope == 'all'
                   or scope == 'context' and record.get('project_id') in (None, body.get('project_id'))
                   or scope == 'universal' and record.get('project_id') is None
                   or scope == 'project' and record.get('project_id') == body.get('project_id')]
        limit = body.get('limit', 20)
        if body.get('query'):
            ranked = []
            for record in records:
                title = str(record['data'].get('title') or record['data'].get('name') or '')
                fields = (title, ' '.join(record.get('tags', [])), _json.dumps(record['data']))
                rank = 0
                for token in body['query'].lower().split():
                    pattern = r'\b' + _re.escape(token) + r'\b'
                    weights = [weight for weight, text in zip((3, 2, 1), fields) if _re.search(pattern, text.lower())]
                    if not weights:
                        break
                    rank += sum(weights)
                else:
                    ranked.append(dict(copy.deepcopy(record), rank=rank, snippet=None))
            ranked.sort(key=lambda record: (-record['rank'], record['record_id']))
            offset = body.get('offset', 0)
            page = ranked[offset:offset + limit]
            return {'records': page, 'next_offset': offset + limit if len(ranked) > offset + limit else None}
        records = sorted((record for record in records
                          if record['record_id'] > body.get('after_id', '')), key=lambda record: record['record_id'])
        page = [dict(copy.deepcopy(record), snippet=None) for record in records[:limit]]
        return {'records': page, 'next_after_id': page[-1]['record_id'] if len(records) > len(page) else None}

    def _invoke(self, route, body):
        import copy
        from tools.memory.validation import validate
        validate(route, body)
        self.calls.append((route, copy.deepcopy(body)))
        action = route.rsplit('/', 1)[1]
        if action == 'lookup':
            if body['request_key'] not in self.requests:
                raise MemoryClientError('operation_unavailable')
            return {'status': 'ready', 'data': copy.deepcopy(self.requests[body['request_key']][1])}
        if action == 'read':
            record = self.history.get((body['record_id'], body['revision'])) if 'revision' in body else self.records.get(body['record_id'])
            if record is None:
                raise MemoryClientError('record_unavailable')
            record = copy.deepcopy(record)
            if 'revision' in body:
                record['claim'] = None  # Real historical reads exclude mutable leases.
            return {'status': 'ready', 'data': record}
        if action == 'search':
            return {'status': 'ready', 'data': self._search(body)}
        if action == 'project_context':
            return {'status': 'ready', 'data': self._project_context(body)}
        if action == 'revisions':
            return {'status': 'ready', 'data': {'revisions': {
                identifier: self.records[identifier]['revision'] for identifier in body['record_ids']
                if identifier in self.records and self._owner(self.records[identifier])}}}
        key = body['request_key']
        if key in self.requests:
            original, result = self.requests[key]
            if original != body:
                raise MemoryClientError('invalid_request')
            return {'status': 'ready', 'data': dict(copy.deepcopy(result), replayed=True)}
        if body.get('authority_generation', self.generation) != self.generation:
            raise MemoryClientError('context_stale')
        current = self.records.get(body['record_id'])
        if (current['revision'] if current else 0) != body['expected_revision']:
            raise MemoryClientError('revision_conflict')
        revision = body['expected_revision'] + 1
        if action == 'write' and body['kind'] == 'session' and current is not None:
            if current['data']['state'] in ('closed', 'abandoned'):
                raise MemoryClientError('revision_conflict')
            if body['data']['state'] != 'active' and any(
                    (record.get('claim') or {}).get('session_id') == body['record_id']
                    for record in self.records.values()):
                raise MemoryClientError('claim_conflict')
        if action == 'write':
            record = {key: copy.deepcopy(body[key]) for key in ('record_id', 'kind', 'data', 'classification', 'data_scope')}
            record.update(revision=revision, project_id=body.get('project_id'), tags=body.get('tags', []))
            result = {'record_id': body['record_id'], 'revision': revision, 'request_key': key}
        else:
            record = copy.deepcopy(current)
            record['revision'] = revision
            if body['action'] == 'claim':
                record['claim'] = {'session_id': body['session_id'], 'fence': revision, 'expires_at': '2099-01-01T00:00:00Z'}
                record['data']['state'] = 'in_progress'
                result = dict(record['claim'])
            else:
                if current['claim']['fence'] != body['fence']:
                    raise MemoryClientError('revision_conflict')
                record['claim'] = None
                record['data']['state'] = 'completed' if body['action'] == 'complete' else 'open'
                result = {'session_id': None, 'fence': body['fence']}
            result.update(record_id=body['record_id'], revision=revision, request_key=key, state=record['data']['state'])
        self.records[body['record_id']] = record
        self.history[(body['record_id'], revision)] = copy.deepcopy(record)
        self.requests[key] = (copy.deepcopy(body), copy.deepcopy(result))
        self.sequence[body['record_id']] = len(self.requests)
        if self.lose_next:
            self.lose_next = False
            raise MemoryClientError('outcome_unknown', request_key=key)
        return {'status': 'ready', 'data': result}
