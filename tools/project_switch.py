"""Reviewed local project rebinding; cloud records and old runtime are never rewritten.

The instance registry lock serializes participating writers. A durable root barrier
survives interruption until the exact approved operation finishes. The old .maven
is renamed to a private sibling archive, never copied into the new active runtime.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from tools.coordination import SessionRegistry, private_lock, project_binding_lock, session_uuid
from tools.home import load_home, validate_root
from tools.project import _validate, load_project
from tools.setup_common import SetupPending, private_directory, read_private, save_json
from tools.memory.boundary import BoundaryError
from tools.startup import _conversation_path, failed_start_evidence, verified_finish


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


def _finished(root, directory):
    path = directory / 'finish.json'
    return path.exists() and verified_finish(read_private(path), {
        'root': str(root), 'project_id': load_project(root)['project_id'], 'session_id': directory.name})


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


def _superseded_start(root, directory):
    """Recognize a fenced no-session attempt, not an abandoned cloud session."""
    receipt_path = directory / 'failed-start-recovery.json'
    binding_path = directory / 'mcp-binding.json'
    if not receipt_path.exists() or not binding_path.exists():
        return False
    receipt, persisted = read_private(receipt_path), read_private(binding_path)
    original = receipt.get('binding', {})
    replacement = receipt.get('replacement_binding', {})
    snapshot = receipt.get('persisted_binding', {})
    replacement_id = replacement.get('session_id')
    allowed = {'mcp-binding.json', 'lifecycle.json', 'command.lock', 'maintenance.lock',
               'maintenance.json', 'failed-memory-write.json', 'skills',
               'failed-start-recovery.json'}
    if any(path.name not in allowed for path in directory.iterdir()):
        return False
    maintenance_path = directory / 'maintenance.json'
    if maintenance_path.exists():
        maintenance = read_private(maintenance_path)
        if maintenance.get('assignments') or maintenance.get('checkpoints') or maintenance.get('closure'):
            return False
    if (receipt.get('schema') != 1 or original.get('root') != str(root)
            or original.get('project_id') != load_project(root)['project_id']
            or original.get('session_id') != directory.name
            or not replacement_id or replacement_id == directory.name
            or any(replacement.get(key) != original.get(key)
                   for key in ('root', 'project_id', 'home', 'env', 'harness'))
            or snapshot.get('initialized') is not False
            or snapshot.get('resume_required', False) is not False
            or any(snapshot.get(key) != value for key, value in original.items())
            or persisted != {**snapshot, 'superseded_by': replacement_id}
            or receipt.get('cloud_session_check') != {
                'record_id': directory.name, 'include_archived': True,
                'status': 'record_unavailable'}):
        return False
    session_uuid(replacement_id)
    replacement_path = directory.parent / replacement_id / 'mcp-binding.json'
    if not replacement_path.exists():
        return False
    replacement_record = read_private(replacement_path)
    return all(replacement_record.get(key) == value for key, value in replacement.items())


def _idle(root, registry, failed=(), stopped=()):
    key = str(root)
    if any(item.get('root') == key and identifier not in failed
           for identifier, item in registry['sessions'].items()):
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
            if (directory.name not in failed and directory.name not in stopped
                    and not _finished(root, directory)
                    and not _superseded_start(root, directory)):
                raise SetupPending('Local session lacks a verified finish receipt or failed-start recovery receipt; recover it before switching.')
            if (directory / 'pending-memory.json').exists():
                raise SetupPending('Local session has an unresolved cloud request; recover it before switching.')
            maintenance = directory / 'maintenance.json'
            if maintenance.exists():
                state = read_private(maintenance)
                if (any(item.get('state') not in ('completed', 'superseded', 'cancelled')
                        for item in state.get('assignments', {}).values())
                        or any(item.get('state') not in ('saved', 'cancelled')
                               for item in state.get('checkpoints', {}).values())):
                    raise SetupPending('Local session has unfinished maintenance or checkpoints; recover them before switching.')
            approval = directory / 'approval.json'
            if approval.exists() and read_private(approval).get('status') not in (
                    'closed', 'checkpointed', 'cancelled', 'approval_cancelled'):
                raise SetupPending('Local session has unresolved Git publication; recover it before switching.')
    setup = runtime / 'project-setup' / 'state.json'
    if setup.exists() and read_private(setup).get('complete') is not True:
        raise SetupPending('Original project setup is unfinished; finish its original operation before switching.')


def _files(directory):
    """Pin every retained byte and directory, including otherwise empty evidence."""
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SetupPending('Original failed-start directory is missing or redirected.')
    result = {}
    for path in sorted(directory.rglob('*')):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_mode & 0o077)):
            raise SetupPending('Failed-start evidence must remain owner-private without symlinks.')
        result[str(path.relative_to(directory))] = (
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def _absent_session(home, identifier, values):
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import configured_client
    client = configured_client(Path(home) / '.env', values=values)
    proof = {'record_id': identifier, 'include_archived': True}
    try:
        try:
            client.invoke('/v1/memory/read', proof)
        except MemoryClientError as error:
            if error.code != 'record_unavailable':
                raise
        else:
            raise SetupPending('Original cloud session exists or its absence is unverified; no attempt was detached.')
    finally:
        client.close()
    return {**proof, 'status': 'record_unavailable'}


def _failed_attempts(home, root, registry, values, stopped=()):
    attempts = {}
    sessions = root / '.maven/runtime/sessions'
    if sessions.exists():
        for directory in sorted(sessions.iterdir()):
            path = directory / 'mcp-binding.json'
            if (directory.name not in stopped and path.exists()
                    and not _finished(root, directory)
                    and not _superseded_start(root, directory)):
                attempts[directory.name] = read_private(path)
    _idle(root, registry, attempts, stopped)
    result = {}
    project_id = load_project(root)['project_id']
    for identifier, persisted in attempts.items():
        original = {key: persisted.get(key) for key in (
            'root', 'project_id', 'session_id', 'home', 'env', 'harness', 'harness_session')}
        if (original['root'] != str(root) or original['project_id'] != project_id
                or original['session_id'] != identifier or not original['harness']
                or persisted.get('resume_required', False) is not False):
            raise SetupPending('Original failed-start binding is incomplete or changed; preserve its evidence.')
        native_path = _conversation_path(original['harness'], original['harness_session'])
        with ExitStack() as locks:
            if native_path:
                locks.enter_context(private_lock(home, native_path + '.lock'))
            try:
                evidence = failed_start_evidence(home, original)
            except BoundaryError as error:
                raise SetupPending('Local session lacks a verified finish receipt or safe failed-start proof: ' + str(error)) from error
            # The transaction may create an empty registry on prepare's return.
            # Pin the logical snapshot, not absence of its backing file.
            evidence['registry'] = registry
            result[identifier] = {
                'binding': original, 'evidence': evidence, 'native_path': native_path,
                'files': _files(sessions / identifier),
                'cloud_session_check': _absent_session(home, identifier, values)}
    return result


def _check_attempts(home, root, runtime, registry, attempts, values, *, cleaning=False):
    """Recheck pinned originals even after the active runtime has been renamed."""
    if str(root) in registry['reservations'] or any(
            item.get('root') == str(root) and identifier not in attempts
            for identifier, item in registry['sessions'].items()):
        raise SetupPending('New registration appeared; finish or recover it before resuming this switch.')
    for identifier, attempt in attempts.items():
        if _files(runtime / 'runtime/sessions' / identifier) != attempt['files']:
            raise SetupPending('Reviewed failed-start evidence changed; approval is invalid.')
        entry = registry['sessions'].get(identifier)
        expected = attempt['evidence']['registry_entry']
        if entry != expected and not (cleaning and entry is None):
            raise SetupPending('Reviewed failed-start registration changed; approval is invalid.')
        path = attempt['native_path']
        native = read_private(home / path) if path and (home / path).exists() else None
        if native != attempt['evidence']['conversation_record'] and not (cleaning and native is None):
            raise SetupPending('Reviewed native conversation changed; approval is invalid.')
        if _absent_session(home, identifier, values) != attempt['cloud_session_check']:
            raise SetupPending('Reviewed cloud absence proof changed; approval is invalid.')


def validate_stopped_session_ids(value):
    if (not isinstance(value, list) or len(value) > 200
            or any(not isinstance(identifier, str) for identifier in value)
            or len(set(value)) != len(value)):
        raise SetupPending('Stopped sessions require an exact, duplicate-free list of at most 200 session UUIDs.')
    for identifier in value:
        try:
            session_uuid(identifier)
        except BoundaryError as error:
            raise SetupPending('Stopped sessions require canonical session UUIDs.') from error
    return sorted(value)


def _stopped_evidence(root, runtime, registry, identifiers):
    """Owner-attested stopped historical runtimes, never proof of cloud closure."""
    from tools.memory.boundary import digest
    from tools.memory.client import DEFINITIVE_MUTATION_ERRORS
    result = {}
    allowed = {'session.json', 'last-memory-write.json', 'failed-memory-write.json', 'finish.json',
               'approval.json', 'maintenance.json', 'command.lock', 'maintenance.lock', 'skills'}
    for identifier in identifiers:
        if identifier in registry['sessions']:
            raise SetupPending('Owner-stopped historical session is registered; no active or writer registration may be detached.')
        directory = runtime / 'runtime/sessions' / identifier
        files = _files(directory)
        if any(path.name not in allowed or (path.name != 'skills' and not path.is_file())
               for path in directory.iterdir()):
            raise SetupPending('Stopped historical session has pending, unknown or bound-session evidence; recover it first.')
        pointer_path = directory / 'session.json'
        if not pointer_path.exists():
            raise SetupPending('Stopped historical session requires its exact session.json pointer.')
        pointer = read_private(pointer_path)
        if (pointer.get('record_id') != identifier or type(pointer.get('revision')) is not int
                or pointer['revision'] < 1):
            raise SetupPending('Stopped historical session pointer differs from its exact directory identity.')
        finish_path = directory / 'finish.json'
        if finish_path.exists():
            finish = read_private(finish_path)
            if finish != {'status': 'closed'} and not verified_finish(finish, {
                    'root': str(root), 'project_id': read_private(runtime / 'project.json')['project_id'],
                    'session_id': identifier}):
                raise SetupPending('Stopped historical closure evidence is not an exact receipt or bare legacy marker.')
        skills = directory / 'skills'
        if skills.exists() and (not skills.is_dir() or any(
                path.name != 'registry.json' or not path.is_file() for path in skills.iterdir())):
            raise SetupPending('Stopped historical session has unresolved skill execution evidence.')
        maintenance_path = directory / 'maintenance.json'
        if maintenance_path.exists():
            maintenance = read_private(maintenance_path)
            if (maintenance.get('schema') != 1 or maintenance.get('binding') != {
                    'root': str(root), 'project_id': read_private(runtime / 'project.json')['project_id'],
                    'session_id': identifier}
                    or maintenance.get('closure') or maintenance.get('catch_up')
                    or not isinstance(maintenance.get('assignments'), dict)
                    or not isinstance(maintenance.get('checkpoints'), dict)
                    or any(item.get('state') not in ('completed', 'superseded', 'cancelled')
                           for item in maintenance['assignments'].values())
                    or any(item.get('state') not in ('saved', 'cancelled')
                           for item in maintenance['checkpoints'].values())):
                raise SetupPending('Stopped historical session has unfinished maintenance or checkpoints.')
        approval_path = directory / 'approval.json'
        if approval_path.exists() and read_private(approval_path).get('status') not in (
                'closed', 'checkpointed', 'cancelled', 'approval_cancelled'):
            raise SetupPending('Stopped historical session has unresolved Git publication.')
        failed_path = directory / 'failed-memory-write.json'
        if failed_path.exists():
            failed = read_private(failed_path)
            request = failed.get('request', {})
            if 'digest' in request:
                verified_digest = ('pending_digest' not in failed and request['digest'] == digest(
                    {key: value for key, value in request.items() if key != 'digest'}))
            else:
                # Legacy receipts stored the canonical request hash beside, not
                # inside, the request. Missing hashes never establish rejection.
                verified_digest = failed.get('pending_digest') == digest(request)
            if (failed.get('status') not in DEFINITIVE_MUTATION_ERRORS
                    or request.get('session_id') != identifier or request.get('route') not in ('write', 'claim')
                    or not verified_digest):
                raise SetupPending('Stopped historical session has an uncertain failed cloud request.')
        result[identifier] = {'status': 'stopped-by-owner', 'session_pointer': pointer,
                              'registry_entry': None, 'files': files,
                              'cloud_state': 'not asserted; no session closure or request replay'}
    return result


def prepare_switch(home, root, project_id, operation_id, binding, *, home_selection=None,
                   stopped_session_ids=None):
    """Prepare the exact candidate under the root gate and registry lock."""
    home, root = Path(home), _root(root)
    session_uuid(project_id)
    session_uuid(operation_id)
    stopped_ids = validate_stopped_session_ids([] if stopped_session_ids is None else stopped_session_ids)
    with project_binding_lock(home, root, shared=False), SessionRegistry(home).transaction() as registry:
        SessionRegistry(home)._allow_work(root)
        project = load_project(root)
        if project['project_id'] == project_id:
            raise SetupPending('Folder already selects this project; no switch is needed.')
        attempts = _failed_attempts(home, root, registry, binding['environment'], stopped_ids)
        stopped = _stopped_evidence(root, root / '.maven', registry, stopped_ids)
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
                'old_project': project, 'new_project': {**project, 'project_id': project_id},
                'old_directory': _directory(root / '.maven'), 'preservation_path': str(archive),
                'failed_attempts': attempts,
                'stopped_sessions': stopped,
                'stopped_confirmation': ({
                    'stopped_session_ids': stopped_ids, 'sessions_and_workers_stopped': True,
                    'preserve_without_cloud_closure': True} if stopped_ids else {}),
                'destination': _cloud(home, project_id, binding['environment']),
                'previous_home': old_home, 'update_home': update_home,
                'home_enabled_at': datetime.now(timezone.utc).isoformat() if update_home else None,
                'home_label': home_selection['label'] if home_selection else (old_home or {}).get('label'),
                'unchanged': 'Old cloud project, tags and memory; project files and Git configuration. Git publication settings are retained exactly. Old sessions, reviewed never-initialized attempts and owner-stopped historical sessions are preserved, never closed or reused. Only reviewed starting registrations and their exact native pointers are removed after cutover. Owner-stopped history is detached locally without asserting its cloud state.'}


def apply_switch(home, candidate, binding, source, *, stopped_confirmation=None):
    """Apply or resume only this pinned review, under the participating-writer lock."""
    home, root = Path(home), _root(candidate['root'])
    operation_id = session_uuid(candidate['operation_id'])
    stopped = candidate.get('stopped_sessions', {})
    if stopped and (stopped_confirmation != candidate.get('stopped_confirmation')
                    or stopped_confirmation != {
                        'stopped_session_ids': sorted(stopped), 'sessions_and_workers_stopped': True,
                        'preserve_without_cloud_closure': True}
                    or not isinstance(source, dict) or any(
                        not isinstance(source.get(key), str) or not source[key].strip()
                        for key in ('origin', 'excerpt'))):
        raise SetupPending('Exact owner confirmation that these sessions and workers stopped, plus preservation consent and source, is required.')
    if candidate['instance_id'] != binding['connection']['instance_id']:
        raise SetupPending('Reviewed MAVEN instance changed; switch approval is invalid.')
    if candidate['new_project']['project_id'] == candidate['old_project']['project_id']:
        raise SetupPending('A folder switch requires distinct old and new project IDs.')
    archive = root.parent / ('.maven-preserved-' + operation_id)
    if candidate['preservation_path'] != str(archive):
        raise SetupPending('Preservation path differs from the reviewed operation.')
    _preservation_safe(archive)
    directory = home / 'runtime' / 'project-switches'
    for parent in (home, home / 'runtime', directory):
        private_directory(parent)
    journal_path = directory / (operation_id + '.json')
    barrier = barrier_path(home, root)
    with project_binding_lock(home, root, shared=False), SessionRegistry(home).transaction() as registry, ExitStack() as locks:
        attempts = candidate.get('failed_attempts', {})
        for path in sorted({item['native_path'] for item in attempts.values() if item['native_path']}):
            locks.enter_context(private_lock(home, path + '.lock'))
        journal = read_private(journal_path) if journal_path.exists() else None
        intent = {'candidate': candidate, 'binding': binding, 'source': source}
        if stopped:
            intent['stopped_confirmation'] = stopped_confirmation
        if journal is not None and journal.get('intent') != intent:
            raise SetupPending('Switch intent changed; resume the original reviewed operation only.')
        marker = {'schema': 1, 'root': str(root), 'operation_id': operation_id}
        if barrier.exists() and read_private(barrier) != marker:
            raise SetupPending('Another interrupted switch owns this folder; resume its exact operation.')
        if journal is None:
            SessionRegistry(home)._allow_work(root)
            _idle(root, registry, attempts, stopped)
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
        old_runtime = archive / '.maven'
        _check_attempts(home, root, old_runtime if old_runtime.exists() else root / '.maven',
                        registry, attempts, binding['environment'],
                        cleaning=bool(journal and journal.get('cleanup_started')))
        if _stopped_evidence(root, old_runtime if old_runtime.exists() else root / '.maven',
                             registry, stopped) != stopped:
            raise SetupPending('Reviewed stopped-session evidence changed; approval is invalid.')
        if not old_runtime.exists():
            _idle(root, registry, attempts, stopped)
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
            _idle(root, registry, attempts, stopped)
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
            _idle(root, registry, attempts, stopped)
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
        # Journal intent retains the exact original registrations/native records.
        # Persist cleanup permission before removing either, so an interruption
        # accepts absence but never a different conversation or registration.
        if attempts:
            journal['cleanup_started'] = True
            save_json(journal_path, journal)
            for identifier, attempt in attempts.items():
                registry['sessions'].pop(identifier, None)
                path = attempt['native_path']
                if path and (home / path).exists():
                    (home / path).unlink()
                    _sync((home / path).parent)
            # Commit cleanup before the completed journal/barrier release, not
            # merely at transaction exit (which an abrupt crash may bypass).
            save_json(home / 'runtime/sessions.json', registry)
        result = {'status': 'ready', 'project': candidate['new_project'], 'preservation_path': str(archive),
                  'previous_project_id': candidate['old_project']['project_id'], 'cloud_changed': False,
                  'home_updated': new_home is not None,
                  'detached_stopped_session_ids': list(stopped),
                  'next': 'Start a new MAVEN session for this project; old sessions remain preserved outside the active runtime.'}
        journal.update(phase='completed', result=result)
        save_json(journal_path, journal)
        barrier.unlink(missing_ok=True)
        _sync(directory)
        return result
