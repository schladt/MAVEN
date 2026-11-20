"""Exact-reviewed cloud-only project creation with original-request reconciliation."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

from tools.memory.client import MemoryClientError
from tools.memory.environment import configured_client
from tools.memory.validation import validate
from tools.setup_common import SetupPending, expected_account, verify_connection


def _root(root):
    path = Path(root)
    if not path.is_absolute() or not path.is_dir() or path != path.resolve():
        raise SetupPending('Cloud project creation requires an absolute existing root without symlinks.')


def _fields(record):
    return {key: deepcopy(record.get(key)) for key in
            ('kind', 'classification', 'data_scope', 'data', 'tags')}


def prepare_create(home, root, name, operation_id, binding):
    _root(root)
    # Creation is universal: the current folder may refer to an unavailable project.
    verify_connection(Path(home) / '.env', values=binding['environment'])
    project_id = str(uuid5(UUID(operation_id), 'cloud-project'))
    request = {'record_id': project_id, 'request_key': str(uuid5(UUID(operation_id), 'cloud-project-create')),
               'expected_revision': 0,
               'kind': 'entity', 'classification': 'Sensitive', 'data_scope': 'owner',
               'data': {'entity_type': 'project', 'name': name, 'aliases': []}, 'tags': [],
               'change_reason': 'Create the owner-selected cloud project without changing local association'}
    validate('/v1/memory/write', {**request, 'source': {
        'origin': 'create-review-validation', 'excerpt': 'Schema validation only; not submitted'}})
    return {'root': root, 'project_id': project_id, 'name': name,
            'instance_id': binding['connection']['instance_id'],
            'recovery_epoch': binding['connection']['recovery_epoch'],
            'region': binding['connection']['region'], 'account': expected_account(binding['environment']),
            'request': request,
            'local_binding_changed': False,
            'unchanged': 'Current root association, project config, Home and runtime remain unchanged. '
                         'Associating this project requires a separately reviewed switch.'}


def apply_create(home, candidate, binding, source, progress, persist):
    """Journal before effects; resume the same UUID, key, body and owner provenance."""
    _root(candidate['root'])
    if any(binding['connection'][key] != candidate[key] for key in ('instance_id', 'recovery_epoch', 'region')):
        raise SetupPending('Creation instance binding changed after review; restore its original connection.')
    verify_connection(Path(home) / '.env', values=binding['environment'])
    request = {**deepcopy(candidate['request']), 'source': deepcopy(source)}
    validate('/v1/memory/write', request)
    if progress.get('request') is not None and progress['request'] != request:
        raise SetupPending('Original creation request changed; preserve its request identity and approval.')
    client = configured_client(Path(home) / '.env', values=binding['environment'])
    try:
        result = None
        if progress.get('submitted'):
            try:
                result = client.invoke('/v1/operations/lookup', {'request_key': request['request_key']})['data']
            except MemoryClientError as error:
                if error.code != 'operation_unavailable':
                    raise
        else:
            progress.update(request=request, submitted=True)
            persist()
        if result is None:
            result = client.invoke('/v1/memory/write', request)['data']
        progress['receipt'] = result
        persist()
        if (result.get('request_key') != request['request_key']
                or result.get('record_id') != request['record_id']
                or type(result.get('revision')) is not int or result['revision'] != 1):
            raise SetupPending('Creation receipt differs from its original request; completion is not verified.')
        for selector in ({'record_id': request['record_id'], 'revision': 1},
                         {'record_id': request['record_id']}):
            record = client.invoke('/v1/memory/read', selector)['data']
            if (record.get('record_id') != request['record_id'] or record.get('revision') != 1
                    or record.get('project_id') is not None or _fields(record) != _fields(request)):
                raise SetupPending('Creation read-back differs from the exact approved content; completion is not verified.')
        progress['verified'] = True
        persist()
        return {'status': 'created', 'project_id': request['record_id'], 'name': candidate['name'],
                'instance_id': candidate['instance_id'], 'local_binding_changed': False,
                'request_key': request['request_key'], 'revision': result['revision']}
    finally:
        client.close()
