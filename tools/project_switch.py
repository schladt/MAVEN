"""Reviewed local project rebinding; cloud records and old runtime are never rewritten.

The instance registry lock serializes participating writers. A durable root barrier
survives interruption until the exact approved operation finishes. The old .maven
is renamed to a private sibling archive, never copied into the new active runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat

from tools.coordination import SessionRegistry, project_binding_lock, session_uuid
from tools.home import load_home, validate_root
from tools.project import _validate, load_project
from tools.setup_common import SetupPending, private_directory, read_private, save_json


def barrier_path(home, root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
    return Path(home) / 'runtime' / 'project-switches' / (key + '.barrier.json')


def _directory(path):
    if not path.exists():
        raise SetupPending('Pinned runtime directory is missing; preserve the switch evidence.')
    private_directory(path)
    info = path.stat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename(source, destination):
    if destination.exists() or destination.is_symlink():
        raise SetupPending('Switch destination already exists; preserve the original switch evidence.')
    os.rename(source, destination)
    _sync(source.parent)
    _sync(destination.parent)


def _root(root):
    root = Path(root).expanduser().absolute()
    if root != root.resolve(strict=True) or root == Path(root.anchor) or not root.is_dir():
        raise SetupPending('Switch requires an existing absolute project directory without symlinks.')
    if root.stat().st_uid != os.getuid():
        raise SetupPending('Switch requires an owner-owned project directory.')
    return root


def _home_record(home):
    record = load_home(home)
    if record and 'invalid' in record:
        raise SetupPending('Repair the existing Home selection before reviewing a folder switch.')
    return record


def _raw_home(home):
    path = Path(home) / 'home.json'
    if path.is_symlink():
        raise SetupPending('Home selection must not be a symlink.')
    return read_private(path) if path.exists() else None


def _cloud(home, project_id, values):
    from tools.memory.environment import configured_client
    client = configured_client(Path(home) / '.env', values=values)
    try:
        record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
    finally:
        client.close()
    if (record.get('record_id') != project_id or record.get('kind') != 'entity'
            or record.get('data_scope') != 'owner' or record.get('project_id') is not None
            or record.get('data', {}).get('entity_type') != 'project'):
        raise SetupPending('Destination must be an existing universal owner project entity.')
    return record


def _idle(root, registry):
    key = str(root)
    if any(item.get('root') == key for item in registry['sessions'].values()):
        raise SetupPending('Folder has open session registrations. Finish them cleanly or explicitly recover and finish them; age is not proof of quiescence.')
    if key in registry['reservations']:
        raise SetupPending('Folder has an unresolved Git reservation; recover it explicitly first.')
    private_directory(root / '.maven')
    runtime = root / '.maven' / 'runtime'
    if runtime.is_symlink():
        raise SetupPending('Runtime evidence must not be a symlink.')
    if not runtime.exists():
        return
    # The private .maven ancestor protects intermediate directories created with
    # the owner's umask. Preserve them as-is; reject redirected/foreign evidence.
    for directory, directories, files in os.walk(runtime, followlinks=False):
        info = Path(directory).lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SetupPending('Runtime directories must remain owner-owned.')
        for name in directories + files:
            path = Path(directory) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_mode & 0o077)):
                raise SetupPending('Runtime evidence must remain owner-private without symlinks or special files.')
    sessions = runtime / 'sessions'
    if sessions.exists():
        for directory in sessions.iterdir():
            if not directory.is_dir():
                raise SetupPending('Unexpected session evidence; recover it explicitly before switching.')
            session_uuid(directory.name)
            finish = directory / 'finish.json'
            if not finish.exists() or read_private(finish).get('status') != 'closed':
                raise SetupPending('Local session lacks a verified finish receipt; recover and finish it before switching.')
            if (directory / 'pending-memory.json').exists():
                raise SetupPending('Local session has an unresolved cloud request; recover it before switching.')
            maintenance = directory / 'maintenance.json'
            if maintenance.exists():
                state = read_private(maintenance)
                if (any(item.get('state') not in ('completed', 'superseded', 'cancelled')
                        for item in state.get('assignments', {}).values())
                        or any(item.get('state') != 'saved' for item in state.get('checkpoints', {}).values())):
                    raise SetupPending('Local session has unfinished maintenance or checkpoints; recover them before switching.')
            approval = directory / 'approval.json'
            if approval.exists() and read_private(approval).get('status') not in (
                    'closed', 'checkpointed', 'cancelled', 'approval_cancelled'):
                raise SetupPending('Local session has unresolved Git publication; recover it before switching.')
    setup = runtime / 'project-setup' / 'state.json'
    if setup.exists() and read_private(setup).get('complete') is not True:
        raise SetupPending('Original project setup is unfinished; finish its original operation before switching.')


def prepare_switch(home, root, project_id, operation_id, binding, *, home_selection=None):
    """Prepare the exact candidate under the root gate and registry lock."""
    home, root = Path(home), _root(root)
    session_uuid(project_id)
    session_uuid(operation_id)
    with project_binding_lock(home, root), SessionRegistry(home).transaction() as registry:
        SessionRegistry(home)._allow_work(root)
        project = load_project(root)
        if project['project_id'] == project_id:
            raise SetupPending('Folder already selects this project; no switch is needed.')
        _idle(root, registry)
        old_home = _home_record(home)
        archive = root.parent / ('.maven-preserved-' + operation_id)
        if archive.exists() or archive.is_symlink():
            raise SetupPending('Preservation path already exists; do not reuse another operation archive.')
        instance_id = binding['connection']['instance_id']
        update_home = home_selection is not None or bool(old_home and old_home['root'] == str(root))
        if update_home:
            validate_root(home, root)
        return {'operation_id': operation_id, 'root': str(root), 'instance_id': instance_id,
                'old_project': project, 'new_project': {**project, 'project_id': project_id},
                'old_directory': _directory(root / '.maven'), 'preservation_path': str(archive),
                'destination': _cloud(home, project_id, binding['environment']),
                'previous_home': old_home, 'update_home': update_home,
                'home_enabled_at': datetime.now(timezone.utc).isoformat() if update_home else None,
                'home_label': home_selection['label'] if home_selection else (old_home or {}).get('label'),
                'unchanged': 'Old cloud project, tags and memory; project files and Git configuration. Git publication settings are retained exactly. Old sessions are archived, never reused.'}


def apply_switch(home, candidate, binding, source):
    """Apply or resume only this pinned review, under the participating-writer lock."""
    home, root = Path(home), _root(candidate['root'])
    operation_id = session_uuid(candidate['operation_id'])
    if candidate['instance_id'] != binding['connection']['instance_id']:
        raise SetupPending('Reviewed MAVEN instance changed; switch approval is invalid.')
    if candidate['new_project']['project_id'] == candidate['old_project']['project_id']:
        raise SetupPending('A folder switch requires distinct old and new project IDs.')
    archive = root.parent / ('.maven-preserved-' + operation_id)
    if candidate['preservation_path'] != str(archive):
        raise SetupPending('Preservation path differs from the reviewed operation.')
    directory = home / 'runtime' / 'project-switches'
    for parent in (home, home / 'runtime', directory):
        private_directory(parent)
    journal_path = directory / (operation_id + '.json')
    barrier = barrier_path(home, root)
    with project_binding_lock(home, root, shared=False), SessionRegistry(home).transaction() as registry:
        journal = read_private(journal_path) if journal_path.exists() else None
        intent = {'candidate': candidate, 'binding': binding, 'source': source}
        if journal is not None and journal.get('intent') != intent:
            raise SetupPending('Switch intent changed; resume the original reviewed operation only.')
        marker = {'schema': 1, 'root': str(root), 'operation_id': operation_id}
        if barrier.exists() and read_private(barrier) != marker:
            raise SetupPending('Another interrupted switch owns this folder; resume its exact operation.')
        if journal is None:
            SessionRegistry(home)._allow_work(root)
            _idle(root, registry)
            if (load_project(root) != candidate['old_project']
                    or _directory(root / '.maven') != candidate['old_directory']
                    or _home_record(home) != candidate['previous_home']):
                raise SetupPending('Reviewed folder or Home selection changed; approval is invalid.')
            if archive.exists() or archive.is_symlink():
                raise SetupPending('Reviewed preservation path is no longer empty.')
        destination = _cloud(home, candidate['new_project']['project_id'], binding['environment'])
        if not (journal and journal.get('phase') == 'completed') and destination != candidate['destination']:
            raise SetupPending('Destination cloud project changed after review; approval is invalid.')
        new_home = None
        if candidate['update_home']:
            validate_root(home, root)
            new_home = {'schema': 1, 'root': str(root), 'project_id': candidate['new_project']['project_id'],
                        'instance_id': candidate['instance_id'], 'label': candidate['home_label'],
                        'enabled_at': candidate['home_enabled_at'], 'consent': source}
        current_home = _raw_home(home)
        if current_home != candidate['previous_home'] and current_home != new_home:
            raise SetupPending('Home selection changed during switch; preserve and recover the original operation.')
        if journal and journal.get('phase') == 'completed':
            if (load_project(root) != candidate['new_project']
                    or (new_home is not None and current_home != new_home)):
                raise SetupPending('Completed switch no longer matches this folder; no old operation was replayed.')
            barrier.unlink(missing_ok=True)
            _sync(directory)
            return journal['result']
        if any(item.get('root') == str(root) for item in registry['sessions'].values()) or str(root) in registry['reservations']:
            raise SetupPending('New registration appeared; finish or recover it before resuming this switch.')
        if not (archive / '.maven').exists():
            _idle(root, registry)
        if journal is None:
            journal = {'schema': 1, 'intent': intent, 'phase': 'prepared'}
            save_json(journal_path, journal)
        # Persist the barrier before any rename. It also blocks startup after a crash.
        save_json(barrier, marker)
        private_directory(archive)
        old, staged, active = archive / '.maven', archive / 'new', root / '.maven'
        if not journal.get('new_directory'):
            if old.exists() or not active.exists():
                raise SetupPending('Unexpected switch boundary; original runtime must still be active.')
            _idle(root, registry)
            if load_project(root) != candidate['old_project'] or _directory(active) != candidate['old_directory']:
                raise SetupPending('Original project changed before preservation; approval is invalid.')
            private_directory(staged)
            if any(path.name != 'project.json' for path in staged.iterdir()):
                raise SetupPending('Unexpected staged runtime; preserve it for recovery.')
            if (staged / 'project.json').exists() and read_private(staged / 'project.json') != candidate['new_project']:
                raise SetupPending('Staged project differs from the exact reviewed destination.')
            save_json(staged / 'project.json', candidate['new_project'])
            journal['new_directory'] = _directory(staged)
            save_json(journal_path, journal)
        if not old.exists():
            if _directory(active) != candidate['old_directory'] or load_project(root) != candidate['old_project']:
                raise SetupPending('Original runtime identity changed; no directory was replaced.')
            _idle(root, registry)
            _rename(active, old)
        if (_directory(old) != candidate['old_directory']
                or _validate(read_private(old / 'project.json'), root) != candidate['old_project']):
            raise SetupPending('Preserved original runtime differs from the reviewed identity.')
        if not active.exists():
            if _directory(staged) != journal['new_directory'] or read_private(staged / 'project.json') != candidate['new_project']:
                raise SetupPending('Staged replacement changed; preserve the exact switch receipt.')
            _rename(staged, active)
        if _directory(active) != journal['new_directory'] or load_project(root) != candidate['new_project']:
            raise SetupPending('Active replacement differs from the pinned switch receipt.')
        if new_home is not None:
            save_json(home / 'home.json', new_home)
        result = {'status': 'ready', 'project': candidate['new_project'], 'preservation_path': str(archive),
                  'previous_project_id': candidate['old_project']['project_id'], 'cloud_changed': False,
                  'home_updated': new_home is not None,
                  'next': 'Start a new MAVEN session for this project; old sessions remain preserved outside the active runtime.'}
        journal.update(phase='completed', result=result)
        save_json(journal_path, journal)
        barrier.unlink(missing_ok=True)
        _sync(directory)
        return result
