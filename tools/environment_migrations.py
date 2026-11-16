"""One-time, owner-reviewed retirement of the local Google namespace locator.

No credential API is used. Imported files are never rewritten: an approved private
snapshot is used only while its exact source digest still matches.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from tools.memory.environment import ENV_KEYS, _load_environment
from tools.setup_common import SetupPending, atomic_private, private_directory, read_private, save_json, setup_lock

RETIRED_KEY = 'MAVEN_GOOGLE_PARAMETER_PREFIX'


def inspect(path):
    path = Path(path).expanduser().absolute()
    if path != path.resolve():
        raise SetupPending('Migration settings must not traverse symlinks.')
    values, raw = _load_environment(path, ENV_KEYS | {RETIRED_KEY}, with_raw=True)
    cleaned = b''.join(line for line in raw.splitlines(keepends=True)
                       if line.strip().partition(b'=')[0] != RETIRED_KEY.encode())
    return values, raw, cleaned


def preview(path):
    """Validation for an import choice only; never authorization to use its settings."""
    values, _, _ = inspect(path)
    values.pop(RETIRED_KEY, None)
    return values


def _directory(home):
    return Path(home) / 'runtime' / 'environment-migrations'


def _pointer(home, path):
    return _directory(home) / (hashlib.sha256(str(Path(path).absolute()).encode()).hexdigest() + '.json')


def load_import(home, path):
    values, raw, _ = inspect(path)
    if RETIRED_KEY not in values:
        return values
    pointer = _pointer(home, path)
    if not pointer.exists():
        raise SetupPending('The selected import needs its separate environment migration review.')
    from tools.administration import AdminOperations
    state = AdminOperations(home).status(read_private(pointer)['operation_id'])
    if (state['state'] != 'completed' or state['outcome']['source_sha256'] != hashlib.sha256(raw).hexdigest()):
        raise SetupPending('Imported settings changed or migration is not approved; preserve the original review.')
    imported, snapshot = _load_environment(Path(state['outcome']['destination']), ENV_KEYS, with_raw=True)
    if hashlib.sha256(snapshot).hexdigest() != state['outcome']['review']['result_sha256']:
        raise SetupPending('Approved import snapshot changed; preserve the migration receipt.')
    return imported


def ensure(controller, path=None):
    """Reuse unfinished evidence; changed completed imports receive a new exact-source review."""
    path = Path(path or controller.home / '.env').expanduser().absolute()
    if not path.exists() and not path.is_symlink():
        return None
    values, raw, _ = inspect(path)
    directory = _directory(controller.home)
    pointer = _pointer(controller.home, path)
    # A receipt written before a crash must still be reconciled under its original UUID.
    if RETIRED_KEY not in values and not pointer.exists():
        return None
    private_directory(directory)
    with setup_lock(directory):
        if pointer.exists():
            state = controller.status(read_private(pointer)['operation_id'])
        else:
            # Recover a crash after the admin record was saved but before this pointer.
            candidates = []
            for saved in controller.directory.glob('*.json'):
                candidate = read_private(saved)
                if (candidate.get('kind') == 'environment_migration'
                        and candidate.get('parameters', {}).get('path', str(controller.home / '.env')) == str(path)
                        and (candidate.get('state') != 'completed' or
                             candidate.get('outcome', {}).get('source_sha256') == hashlib.sha256(raw).hexdigest())):
                    candidates.append(candidate['operation_id'])
            if len(candidates) > 1:
                raise SetupPending('Multiple migration records name this source; preserve their original identities and select the exact operation.')
            state = controller.status(candidates[0]) if candidates else controller.begin('environment_migration', {'path': str(path)})
            save_json(pointer, {'operation_id': state['operation_id']})
        if state['state'] == 'completed':
            if RETIRED_KEY in values:
                if state['outcome']['source_sha256'] != hashlib.sha256(raw).hexdigest():
                    # Only completed evidence can be superseded; its UUID, review and snapshot stay immutable.
                    candidates = [read_private(saved) for saved in controller.directory.glob('*.json')]
                    pending = [candidate for candidate in candidates
                               if candidate.get('kind') == 'environment_migration'
                               and candidate.get('state') != 'completed'
                               and candidate.get('parameters', {}).get('path') == str(path)]
                    if len(pending) > 1:
                        raise SetupPending('Multiple unfinished migration records name this source; preserve their original identities.')
                    state = (controller.status(pending[0]['operation_id']) if pending else
                             controller.begin('environment_migration', {'path': str(path)}))
                    save_json(pointer, {'operation_id': state['operation_id']})
                    return state
                if path == controller.home / '.env':
                    raise SetupPending('Previously migrated settings changed; original migration evidence is retained.')
            return None
        return state


def prerequisite(controller, state):
    if state.get('kind') == 'environment_migration':
        return None
    paths = [controller.home / '.env']
    connection = state.get('answers', {}).get('selection', {}).get('connection', '')
    if connection.startswith('/'):
        paths.append(Path(connection))
    details = state.get('answers', {}).get('secrets_details', {})
    if state.get('answers', {}).get('secrets_source', {}).get('mode') == 'import' and details.get('path'):
        paths.append(Path(details['path']).expanduser())
    for path in paths:
        pending = ensure(controller, path)
        if pending:
            return pending
    return None


def advance(controller, state):
    path = Path(state['parameters'].get('path', str(controller.home / '.env'))).expanduser()
    if not path.is_absolute():
        raise SetupPending('Select an absolute private settings path.')
    destination = path if path == controller.home / '.env' else _directory(controller.home) / (state['operation_id'] + '.env')
    private_directory(_directory(controller.home))
    values, raw, cleaned = inspect(path)
    if state['step'] == 'start':
        if RETIRED_KEY not in values:
            state.update(state='completed', outcome={'changed': False, 'destination': str(path),
                         'source_sha256': hashlib.sha256(raw).hexdigest()})
            return
        review = {'source': str(path), 'destination': str(destination),
                  'source_sha256': hashlib.sha256(raw).hexdigest(),
                  'result_sha256': hashlib.sha256(cleaned).hexdigest(),
                  'removed_key': RETIRED_KEY, 'preserved_locator': values[RETIRED_KEY],
                  'effect': 'Remove only the retired locator from this home, or create a sanitized import snapshot. Preserve every other byte and locator. Imported source files remain untouched.',
                  'retained': 'Stored Google credentials remain ordinary untouched SSM values. No credentials are read, changed or deleted. Magic Workspace is independent and untouched. No authority is activated or revoked.'}
        controller._review(state, 'environment_migration', 'Retire obsolete local Google locator', review, 'environment_apply')
        return
    review = state['approvals'][-1]['review']
    receipt_path = _directory(controller.home) / (state['operation_id'] + '.receipt.json')
    current_digest = hashlib.sha256(raw).hexdigest()
    if receipt_path.exists():
        receipt = read_private(receipt_path)
        if receipt['review'] != review:
            raise SetupPending('Migration receipt differs from the approved review.')
        if path == destination and current_digest == review['result_sha256']:
            state.update(state='completed', outcome=receipt)
            return
    if current_digest != review['source_sha256']:
        raise SetupPending('Settings changed after migration review; nothing was overwritten. Restore the reviewed source to resume this exact operation.')
    if hashlib.sha256(cleaned).hexdigest() != review['result_sha256']:
        raise SetupPending('Migration output differs from its approved review.')
    if path != destination and (destination.exists() or destination.is_symlink()):
        _, existing = _load_environment(destination, ENV_KEYS, with_raw=True)
        if not receipt_path.exists() or hashlib.sha256(existing).hexdigest() != review['result_sha256']:
            raise SetupPending('Import snapshot destination changed; nothing was overwritten.')
    original = _directory(controller.home) / (state['operation_id'] + '.source.env')
    if original.exists() or original.is_symlink():
        _, retained = _load_environment(original, ENV_KEYS | {RETIRED_KEY}, with_raw=True)
        if hashlib.sha256(retained).hexdigest() != review['source_sha256']:
            raise SetupPending('Original migration source snapshot changed; preserve its exact receipt.')
    else:
        atomic_private(original, raw.decode('utf-8'))
    receipt = {'operation_id': state['operation_id'], 'source_sha256': current_digest,
               'destination': str(destination), 'source_snapshot': str(original),
               'preserved_locator': values[RETIRED_KEY],
               'review': review, 'changed': True}
    # Persist provenance before the replacement; interrupted writes reconcile this receipt.
    save_json(receipt_path, receipt)
    atomic_private(destination, cleaned.decode('utf-8'))
    state.update(state='completed', outcome=receipt)


def present(ui, home, path=None):
    from tools.administration import AdminOperations
    from tools.tui.administration import present as present_operation
    controller = AdminOperations(home)
    controller._prepare()
    pending = ensure(controller, path)
    if pending:
        return present_operation(ui, home, operation_id=pending['operation_id'])
    return 0
