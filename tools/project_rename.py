"""Exact-reviewed project entity renames with durable original-request reconciliation."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

from tools.memory.client import MemoryClientError
from tools.memory.environment import configured_client
from tools.memory.validation import validate
from tools.project import load_project
from tools.setup_common import SetupPending, expected_account, verify_authority


def _local(root, project_id):
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root != root.resolve():
        raise SetupPending('Rename requires an absolute existing project root without symlinks.')
    project = load_project(root)
    if project['project_id'] != project_id:
        raise SetupPending('Rename must select this folder\'s existing project UUID; no binding was changed.')
    return project


def _record(client, project_id, revision=None):
    request = {'record_id': project_id}
    if revision is not None:
        request['revision'] = revision
    record = client.invoke('/v1/memory/read', request)['data']
    if (record.get('record_id') != project_id or record.get('kind') != 'entity'
            or record.get('classification') != 'Sensitive' or record.get('data_scope') != 'owner'
            or record.get('project_id') is not None
            or record.get('data', {}).get('entity_type') != 'project'
            or type(record.get('revision')) is not int or record['revision'] < 1):
        raise SetupPending('Rename requires the existing universal owner project entity; nothing was created.')
    return record


def _fields(record):
    return {key: deepcopy(record[key]) for key in
            ('kind', 'classification', 'data_scope', 'data', 'tags')}


def prepare_rename(home, root, project_id, name, operation_id, binding):
    local = _local(root, project_id)
    generation = verify_authority(Path(home) / '.env', project_id, values=binding['environment'])
    client = configured_client(Path(home) / '.env', values=binding['environment'])
    try:
        record = _record(client, project_id)
    finally:
        client.close()
    if record['data']['name'] == name:
        raise SetupPending('The project already has that exact name; no rename is needed.')
    request = {**_fields(record), 'record_id': project_id, 'expected_revision': record['revision'],
               'request_key': str(uuid5(UUID(operation_id), 'project-rename')),
               'authority_generation': generation,
               'change_reason': 'Rename the owner-selected cloud project; retain its identity and history'}
    request['data']['name'] = name
    # Validate the actual entity schema before presenting a review. Approval supplies real provenance.
    validate('/v1/memory/write', {**request, 'source': {
        'origin': 'rename-review-validation', 'excerpt': 'Schema validation only; not submitted'}})
    return {'root': root, 'project_id': project_id, 'instance_id': binding['connection']['instance_id'],
            'recovery_epoch': binding['connection']['recovery_epoch'],
            'account': expected_account(binding['environment']), 'region': binding['connection']['region'],
            'old_name': record['data']['name'], 'new_name': name,
            'expected_revision': record['revision'], 'authority_generation': generation,
            'affects_all_machines': True, 'local_project': local, 'original': _fields(record),
            'request': request,
            'unchanged': 'Project UUID, folder, local binding, aliases, tags, memory/history, Home selection, Git and cloud infrastructure.'}


def apply_rename(home, candidate, binding, source, progress, persist):
    """Reconcile before retrying; never substitute a new key, revision, body or provenance."""
    if _local(candidate['root'], candidate['project_id']) != candidate['local_project']:
        raise SetupPending('Local project selection changed after review; rename was not submitted.')
    if any(binding['connection'][key] != candidate[key] for key in ('instance_id', 'recovery_epoch', 'region')):
        raise SetupPending('Rename instance binding changed after review; restore its original connection.')
    generation = verify_authority(Path(home) / '.env', candidate['project_id'], values=binding['environment'])
    request = {**deepcopy(candidate['request']), 'source': deepcopy(source)}
    validate('/v1/memory/write', request)
    if progress.get('request') is not None and progress['request'] != request:
        raise SetupPending('Original rename request changed; preserve its request identity and approval.')
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
            if generation != candidate['authority_generation']:
                raise SetupPending('Authority changed after review; a fresh exact rename review is required.')
            current = _record(client, candidate['project_id'])
            if (current['revision'] != candidate['expected_revision']
                    or _fields(current) != candidate['original']):
                raise SetupPending('Project changed after review; a fresh exact rename review is required.')
            progress.update(request=request, submitted=True)
            persist()  # Save before the first possible cloud effect, including a lost response.
        if result is None:
            result = client.invoke('/v1/memory/write', request)['data']
        progress['receipt'] = result
        persist()
        if (result.get('request_key') != request['request_key']
                or result.get('record_id') != request['record_id']
                or type(result.get('revision')) is not int
                or result['revision'] != request['expected_revision'] + 1):
            raise SetupPending('Rename receipt differs from its original request; completion is not verified.')
        record = _record(client, request['record_id'], result['revision'])
        if record['revision'] != result['revision'] or _fields(record) != _fields(request):
            raise SetupPending('Rename read-back differs from the exact approved content; completion is not verified.')
        current = _record(client, request['record_id'])
        if current['revision'] != result['revision'] or _fields(current) != _fields(request):
            raise SetupPending('The rename committed, but the project changed again; inspect the retained receipt and current record.')
        progress['verified'] = True
        persist()
        return {'status': 'renamed', 'project_id': request['record_id'], 'record_id': request['record_id'],
                'revision': result['revision'], 'request_key': request['request_key'],
                'old_name': candidate['old_name'], 'name': current['data']['name'],
                'instance_id': candidate['instance_id'], 'affects_all_machines': True,
                'local_binding_changed': False, 'home_selection_changed': False,
                'cloud_infrastructure_changed': False}
    finally:
        client.close()
