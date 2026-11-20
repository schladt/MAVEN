"""Reviewed folder rebinding: preserve the entire old .maven before changing identity."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from tools.coordination import ensure_available, project_binding_lock
from tools.home import load_home, validate_root
from tools.project import _uuid, _validate, load_project
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
    root = Path(root).expanduser()
    if not root.is_absolute() or root != root.resolve(strict=True) or root == Path(root.anchor) or not root.is_dir():
        raise SetupPending('Switch requires an existing absolute project directory without symlinks.')
    if root.stat().st_uid != os.getuid():
        raise SetupPending('Switch requires an owner-owned project directory.')
    return root


def _preservation_safe(archive):
    """A private mode alone does not exclude evidence from an ancestor worktree."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    for parent in archive.parents:
        marker = parent / '.git'
        if not marker.exists() and not marker.is_symlink():
            continue
        try:
            result = subprocess.run(
                ['git', '-C', str(parent), 'check-ignore', '--no-index', '--quiet', '--',
                 archive.relative_to(parent).as_posix() + '/'],
                env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise SetupPending('Cannot verify preservation exclusion in an ancestor Git worktree; no runtime was moved.') from None
        if result.returncode:
            raise SetupPending('Private preservation would be exposed in an ancestor Git worktree. '
                               'Separately review and exclude this exact preservation directory there, then retry; '
                               'MAVEN never edits an unrelated parent repository.')


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
    from tools.memory.reconcile_cloud import status
    client = configured_client(Path(home) / '.env', values=values)
    try:
        status(client)
        record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
    finally:
        client.close()
    if (record.get('record_id') != project_id or record.get('kind') != 'entity'
            or record.get('data_scope') != 'owner' or record.get('project_id') is not None
            or record.get('data', {}).get('entity_type') != 'project'
            or 'archived' in record.get('tags', []) or record.get('data', {}).get('state') == 'archived'):
        raise SetupPending('Destination must be an active universal owner project entity.')
    return record


def _idle(root):
    # The exclusive binding gate prevents any participating Git publisher or
    # record writer from entering while this directory is being preserved.
    from tools.git_workflow import require_no_pending
    require_no_pending(root)
    evidence = root / '.maven'
    private_directory(evidence)
    # The private .maven ancestor protects intermediate directories created with
    # the owner's umask. Preserve them as-is; reject redirected/foreign evidence.
    for directory, directories, files in os.walk(evidence, followlinks=False):
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
    setup = evidence / 'runtime' / 'project-setup' / 'state.json'
    if setup.exists() and read_private(setup).get('complete') is not True:
        raise SetupPending('Original project setup is unfinished; finish its original operation before switching.')



def prepare_switch(home, root, project_id, operation_id, binding, *, home_selection=None):
    """Pin the exact identity, preservation path and cloud destination."""
    home, root = Path(home), _root(root)
    _uuid(project_id)
    _uuid(operation_id)
    with project_binding_lock(home, root, shared=False):
        ensure_available(home, root)
        _idle(root)
        project = load_project(root)
        destination = _validate({**project, 'schema': 2, 'project_id': project_id,
                                 **{key: binding['connection'][key] for key in ('instance_id', 'recovery_epoch')}}, root)
        if project == destination:
            raise SetupPending('Folder already selects this project; no switch is needed.')
        old_home = _home_record(home)
        archive = root.parent / ('.maven-preserved-' + operation_id)
        _preservation_safe(archive)
        if archive.exists() or archive.is_symlink():
            raise SetupPending('Preservation path already exists; do not reuse another operation archive.')
        instance_id = binding['connection']['instance_id']
        update_home = home_selection is not None or bool(old_home and old_home['root'] == str(root))
        if update_home:
            validate_root(home, root)
        return {'operation_id': operation_id, 'root': str(root), 'instance_id': instance_id,
                'old_project': project, 'new_project': destination,
                'old_directory': _directory(root / '.maven'), 'preservation_path': str(archive),
                'destination': _cloud(home, project_id, binding['environment']),
                'previous_home': old_home, 'update_home': update_home,
                'home_enabled_at': datetime.now(timezone.utc).isoformat() if update_home else None,
                'home_label': home_selection['label'] if home_selection else (old_home or {}).get('label'),
                'unchanged': 'Old cloud project and memory, Git settings, and project files. The entire old .maven directory, including any unsynced local records, is preserved at the reviewed private path.'}


def apply_switch(home, candidate, binding, source):
    """Apply or resume only this pinned review, under the participating-writer lock."""
    home, root = Path(home), _root(candidate['root'])
    operation_id = _uuid(candidate['operation_id'])
    from tools.project import require_binding
    require_binding(candidate['new_project'], binding['connection'])
    if candidate['new_project'] == candidate['old_project']:
        raise SetupPending('A folder switch requires a changed project binding.')
    archive = root.parent / ('.maven-preserved-' + operation_id)
    if candidate['preservation_path'] != str(archive):
        raise SetupPending('Preservation path differs from the reviewed operation.')
    _preservation_safe(archive)
    directory = home / 'runtime' / 'project-switches'
    for parent in (home, home / 'runtime', directory):
        private_directory(parent)
    journal_path = directory / (operation_id + '.json')
    barrier = barrier_path(home, root)
    with project_binding_lock(home, root, shared=False):
        journal = read_private(journal_path) if journal_path.exists() else None
        intent = {'candidate': candidate, 'binding': binding, 'source': source}
        if journal is not None and journal.get('intent') != intent:
            raise SetupPending('Switch intent changed; resume the original reviewed operation only.')
        marker = {'schema': 1, 'root': str(root), 'operation_id': operation_id}
        if barrier.exists() and read_private(barrier) != marker:
            raise SetupPending('Another interrupted switch owns this folder; resume its exact operation.')
        ensure_available(home, root, switch_operation=operation_id)
        if journal is None:
            _idle(root)
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
                        'recovery_epoch': candidate['new_project']['recovery_epoch'],
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
        if journal is None:
            journal = {'schema': 1, 'intent': intent, 'phase': 'prepared'}
            save_json(journal_path, journal)
        # Retain a barrier across interruption between the two directory renames.
        save_json(barrier, marker)
        private_directory(archive)
        old, staged, active = archive / '.maven', archive / 'new', root / '.maven'
        if not journal.get('new_directory'):
            if old.exists() or not active.exists():
                raise SetupPending('Unexpected switch boundary; original runtime must still be active.')
            _idle(root)
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
            _idle(root)
            _preservation_safe(archive)
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
                  'next': 'Read the selected project’s local orientation.'}
        journal.update(phase='completed', result=result)
        save_json(journal_path, journal)
        barrier.unlink(missing_ok=True)
        _sync(directory)
        return result
