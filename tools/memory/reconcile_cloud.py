"""Session-free facade over the selected instance's signed IAM service (never the database)."""
from tools.reconcile_records import KINDS, MAX_BATCH, record_uuid, redaction_target, validate_project_envelope
from .client import MemoryClientError
from .reconcile_transport import TOKEN
from .validation import MemoryError, validate


def _request(client, action, body):
    route = '/v1/reconcile/' + action
    request = {**body, 'instance_id': client.config['instance_id'], 'recovery_epoch': client.config['recovery_epoch']}
    try:
        validate(route, request)
    except MemoryError as exc:
        raise MemoryClientError(exc.status, detail=exc.metadata.get('detail')) from None
    try:
        response = client.invoke(route, request)
    except MemoryClientError as exc:
        if exc.code in {'service_response_unavailable', 'invalid_service_response'}:
            raise MemoryClientError('outcome_unknown' if action == 'apply' else 'snapshot_incomplete') from None
        raise
    if not isinstance(response, dict) or response.get('status') != 'ready' or not isinstance(response.get('data'), dict):
        raise MemoryClientError('outcome_unknown' if action == 'apply' else 'snapshot_incomplete')
    return response['data']


def status(client):
    result = _request(client, 'status', {})
    if result != {'instance_id': client.config['instance_id'], 'recovery_epoch': client.config['recovery_epoch'],
                  'capability': 'project_reconcile_v1'}:
        raise MemoryClientError('context_stale')
    return result


def _token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _item(entry):
    required = {'id', 'written_at', 'digest', 'kind', 'summary'}
    if not isinstance(entry, dict) or not required <= entry.keys():
        return False
    try:
        record_uuid(entry['id'])
    except ValueError:
        return False
    if (not _token(entry['digest']) or not isinstance(entry['written_at'], str) or len(entry['written_at']) > 40
            or not isinstance(entry['kind'], str) or not isinstance(entry['summary'], str) or len(entry['summary']) > 240):
        return False
    if entry['kind'] == 'erased':
        return entry.keys() == required | {'erased'} and entry['erased'] is True and entry['summary'] == 'Erased'
    if entry['kind'] == 'skill':
        return (entry.keys() == required | {'name', 'when_to_use'}
                and isinstance(entry['name'], str) and len(entry['name']) <= 120
                and isinstance(entry['when_to_use'], str) and len(entry['when_to_use']) <= 400)
    return (entry['kind'] in KINDS and entry.keys() <= required | {'state'}
            and isinstance(entry.get('state', ''), str) and len(entry.get('state', '')) <= 80)


class CloudStore:
    def __init__(self, client, project_id):
        self.client = client
        self.project_id = record_uuid(project_id)

    def _call(self, action, **args):
        return _request(self.client, action, {'project_id': self.project_id, **args})

    def marker_pages(self, project_id=None):
        """Read the complete permanent set, even for projects barred from normal reads."""
        project_id = self.project_id if project_id is None else record_uuid(project_id)
        cursor = token = total = None
        seen = 0
        while True:
            request = {'after_kind': cursor[0], 'after_id': cursor[1], 'expected_token': token} if cursor else {}
            page = self._call('markers', project_id=project_id, **request)
            if set(page) != {'total', 'token', 'targets', 'more'}:
                raise MemoryClientError('snapshot_incomplete')
            if token is None:
                token, total = page['token'], page['total']
            entries = page['targets']
            if (not _token(token) or page['token'] != token or type(page['total']) is not int
                    or page['total'] != total or not isinstance(entries, list) or len(entries) > 64
                    or type(page['more']) is not bool):
                raise MemoryClientError('snapshot_incomplete')
            for entry in entries:
                try:
                    redaction_target(entry, project_id=project_id)
                except (TypeError, ValueError):
                    raise MemoryClientError('snapshot_incomplete') from None
                key = entry['kind'], entry['id']
                if cursor is not None and key <= cursor:
                    raise MemoryClientError('snapshot_incomplete')
                cursor = key
            seen += len(entries)
            if seen > total or page['more'] != (seen < total) or page['more'] and len(entries) != 64:
                raise MemoryClientError('snapshot_incomplete')
            yield entries
            if not page['more']:
                return

    def retired_bundles(self, hashes):
        result = self._call('markers', bundle_hashes=hashes)
        retired = result.get('retired')
        if (set(result) != {'retired'} or not isinstance(retired, list)
                or any(type(i) is not int or not 0 <= i < len(hashes) for i in retired)
                or retired != sorted(set(retired))):
            raise MemoryClientError('snapshot_incomplete')
        return set(retired)

    def inventory(self):
        items = []
        cursor = token = total = None
        while True:
            response = self._call('inventory', **({'after_id': cursor, 'expected_token': token} if cursor else {}))
            if not {'token', 'items', 'next_after_id', 'total'} <= response.keys():
                raise MemoryClientError('snapshot_incomplete')
            if token is None:
                token, total = response['token'], response['total']
            page = response['items']
            if (not _token(token) or response['token'] != token or type(total) is not int or total < 0
                    or type(response['total']) is not int or response['total'] != total
                    or not isinstance(page, list) or len(page) > 32 or not all(_item(entry) for entry in page)):
                raise MemoryClientError('snapshot_incomplete')
            for item in page:
                if cursor is not None and item['id'] <= cursor:
                    raise MemoryClientError('snapshot_incomplete')
                cursor = item['id']
            items.extend(page)
            if response['next_after_id'] is None:
                if len(items) != total:
                    raise MemoryClientError('snapshot_incomplete')
                return {'token': token, 'items': items}
            if not page or len(items) >= total or response['next_after_id'] != cursor:
                raise MemoryClientError('snapshot_incomplete')

    def read(self, ids, expected_token):
        # Read a contiguous prefix per page (byte bounded); partial lists are never returned.
        if not isinstance(ids, list) or len(ids) > MAX_BATCH:
            raise MemoryClientError('invalid_request')
        records = []
        while True:
            remaining = ids[len(records):]
            page = self._call('read', ids=remaining, expected_token=expected_token)
            data = page.get('records')
            if (page.get('token') != expected_token or not isinstance(data, list) or len(data) > len(remaining)
                    or (remaining and not data) or type(page.get('remaining')) is not int
                    or page['remaining'] != len(remaining)-len(data)):
                raise MemoryClientError('snapshot_incomplete')
            for rid, entry in zip(remaining, data):
                try:
                    validate_project_envelope(entry, self.project_id)
                except (ValueError, TypeError):
                    raise MemoryClientError('snapshot_incomplete') from None
                if entry['record']['id'] != rid:
                    raise MemoryClientError('snapshot_incomplete')
            records.extend(data)
            if not page['remaining']:
                return records

    def apply(self, changes, expected_token):
        response = self._call('apply', changes=changes, expected_token=expected_token)
        changed, token = response.get('changed'), response.get('token')
        ids = [env['record']['id'] for env in changes]
        if not isinstance(changed, list) or any(not isinstance(rid, str) for rid in changed):
            raise MemoryClientError('outcome_unknown')
        changed_ids = set(changed)
        if (changed != [rid for rid in ids if rid in changed_ids] or not _token(token)
                or bool(changed) != (token != expected_token)):
            raise MemoryClientError('outcome_unknown')
        return {'token': token, 'changed': changed}
