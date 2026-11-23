"""Terminal replica delivery. Only exact, managed artifacts are eligible for a sweep."""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3

from tools.coordination import LOCK_TIMEOUT, private_lock, project_binding_lock
from tools.project import load_project
from tools.reconcile_local import LocalStore, _directory, _file_info, _private_database
from tools.reconcile_records import record_uuid, redaction_target
from tools.setup_common import SetupPending, read_private, save_json, setup_lock

_UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_ZIP = re.compile(r'([a-z0-9]+(?:-[a-z0-9]+)*)-([0-9a-f]{64})\.zip\Z')


class PreservedArchiveUnavailable(SetupPending):
    next_action = (
        'Put the original preserved directory back at the recorded location beside the project folder, '
        'then start a fresh reconcile inspection. If it is gone, keep any unsynced local work here '
        'and use another installation bound to the same cloud project to access the cloud copy. '
        'That does not recover local-only records.'
        ' For an intentionally deleted archive, acknowledge that exact absent directory with '
        'maven-project acknowledge-deleted-archive --archive ABSOLUTE_PATH.'
    )


def _unsafe():
    raise SetupPending('Local redaction evidence is ambiguous or unsafe. No original request was replayed; inspect the selected installation privately.')


def _exists(path):
    return path.exists() or path.is_symlink()


def _owned_dir(path):
    if path != path.resolve() or not path.is_dir():
        _unsafe()
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        _unsafe()


def _file(path):
    if path != path.resolve():
        _unsafe()
    _file_info(path.lstat())


def _json(path):
    _file(path)
    return read_private(path, maximum=64 * 1024 * 1024)


def _changed(path, value):
    _file(path)
    save_json(path, value)


class Replica:
    """Installation-owned marker DB lives OUTSIDE replaceable project worktrees.

    Callers hold this instance lock throughout selection, delivery and access. A
    crash after recording a page but before rewriting caches leaves its terminal
    decisions in force on the next offline open. Never prune on cloud absence.
    """
    def __init__(self, home, instance_id):
        self.home = Path(home)
        self.instance_id = record_uuid(instance_id)
        self.directory = self.home / 'runtime' / 'redactions'
        self.path = self.directory / (self.instance_id + '.sqlite3')

    @contextmanager
    def locked(self):
        if self.home != self.home.resolve():
            _unsafe()
        _directory(self.home, private=False)
        _directory(self.home / 'runtime', private=False)
        _directory(self.directory, private=True)
        lock = self.directory / (self.instance_id + '.lock')
        if _exists(lock):
            _file(lock)
        with private_lock(self.directory, lock.name, timeout=LOCK_TIMEOUT):
            yield

    def _db(self):
        _private_database(self.path)
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA fullfsync=ON')
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                _unsafe()
            db.execute('CREATE TABLE IF NOT EXISTS markers(kind TEXT NOT NULL,id TEXT NOT NULL,scope TEXT NOT NULL,'
                       ' PRIMARY KEY(kind,id,scope)) WITHOUT ROWID')
            return db
        except BaseException:
            db.close()
            raise

    def remember(self, targets):
        values = [redaction_target(t) for t in targets]
        if not values:
            return
        db = self._db()
        try:
            with db:
                db.executemany('INSERT OR IGNORE INTO markers VALUES(?,?,?)',
                               ((t['kind'], t['id'], t.get('project_id', '')) for t in values))
        finally:
            db.close()

    def targets(self):
        if not _exists(self.path):
            return []
        # Used by read-only admin status too: do not acquire the application lock
        # here, create missing files, recover a hot journal, or migrate anything.
        _owned_dir(self.directory)
        _file(self.path)
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = self.directory / (self.path.name + suffix)
            if _exists(sidecar):
                _file(sidecar)
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                _unsafe()
            return [redaction_target({'kind': k, 'id': i, **({'project_id': p} if p else {})})
                    for k, i, p in db.execute('SELECT kind,id,scope FROM markers')]
        finally:
            db.close()

    def protect(self, store, *, delivering=False):
        targets = self.targets()
        private_ids = {t['id'] for t in targets if t.get('project_id') == store.project_id}
        retired = {'kind': 'project', 'id': store.project_id} in targets
        store.install_redactions(private_ids, retired=retired)
        if retired:
            _forget_project_name(store.root / '.maven', store.project_id)
        _sweep_workspace(store.root, store.project_id, private_ids)
        if retired and not delivering:
            raise SetupPending('Project identity is permanently retired. Unreviewed local work is not uploaded.')

    def receive(self, store, cloud):
        seen = set()

        def receive_project(project):
            if project not in seen:
                for page in cloud.marker_pages(project):
                    self.remember(page)
                seen.add(project)
            return self.targets()

        targets = receive_project(store.project_id)
        _sweep_preserved(self.home, self.instance_id, targets, cloud.retired_bundles, receive_project)
        targets = self.targets()
        if not targets:
            return
        self.protect(store, delivering=True)
        # Administration's normal lock serializes review retirement against a
        # concurrent advance. This lock is nonblocking (reverse order to admin).
        admin_dir = self.home / 'runtime' / 'administration'
        if _exists(admin_dir):
            _owned_dir(admin_dir)
            with setup_lock(admin_dir):
                _sweep_admin(self.home, self.instance_id, targets)
        _sweep_root(store.root, store.project_id, targets, cloud.retired_bundles)
        if {'kind': 'project', 'id': store.project_id} in targets:
            _sweep_home(self.home, self.instance_id, targets)
            raise SetupPending('Terminal markers applied. Project identity is permanently retired; offline copies remain unverified.')


def _index(targets):
    ids = defaultdict(set)
    for target in targets:
        if target['kind'] != 'project_record':
            ids[target['kind']].add(target['id'])
    return ids


def _matches(value, ids):
    """Match typed control fields, never arbitrary UUID substrings in authored text/data."""
    if isinstance(value, list):
        return any(_matches(item, ids) for item in value)
    if not isinstance(value, dict):
        return False
    fields = {'record_id': ('record', 'source', 'page'), 'source_id': ('source', 'citation'),
              'page_id': ('page',), 'proposal_id': ('proposal', 'publication'), 'challenge_id': ('challenge',),
              'request_key': ('operation', 'instruction_receipt', 'memory_receipt'), 'receipt_id': ('cleanup',),
              'operation_id': ('operation', 'cleanup'), 'project_id': ('project',), 'new_project_id': ('project',),
              'invocation_id': ('consumption',), 'checkpoint_id': ('record',), 'session_id': ('record',)}
    for key, kinds in fields.items():
        v = value.get(key)
        if isinstance(v, str) and any(v in ids[k] for k in kinds):
            return True
    selection = value.get('record_ids')
    if isinstance(selection, list) and any(isinstance(i, str) and i in ids['record'] for i in selection):
        return True
    return any(_matches(v, ids) for k, v in value.items() if k not in ('data', 'history', 'tags', 'source_text'))


def _retire(operation):
    return {'schema': 1, 'kind': operation['kind'], 'operation_id': record_uuid(operation['operation_id']),
            'home': operation['home'], 'revision': operation['revision'] + 1, 'state': 'cancelled',
            'step': 'redacted', 'parameters': {}, 'answers': {}, 'approvals': [], 'receipts': [],
            'pending_interaction': None, 'live_state_checked': False, 'outcome': {'status': 'redacted'}}


def _admin_instances(document):
    keys = [(document.get('presentation') or {}).get('target')]
    reviews = [a.get('review', {}) for a in document.get('approvals', [])]
    reviews.append((document.get('pending_interaction') or {}).get('review', {}))
    for review in reviews:
        if isinstance(review, dict):
            keys.append(review.get('instance_id'))
            binding = review.get('binding')
            if isinstance(binding, dict) and isinstance(binding.get('connection'), dict):
                keys.append(binding['connection'].get('instance_id'))
    return {i for i in keys if isinstance(i, str) and re.fullmatch(_UUID, i)}


def _redaction_control(document):
    return document.get('kind') == 'cleanup' and document.get('parameters', {}).get('action') in {
        'redact', 'purge_provider', 'redaction_pending', 'retry_objects', 'restore_redactions',
        'provider_candidates', 'redaction_candidates'}


def shadow_admin(home, document):
    """Pure status/replay guard when the post-commit multi-file sweep was interrupted."""
    if document.get('step') == 'redacted' or _redaction_control(document):
        return document
    for instance in _admin_instances(document):
        targets = Replica(home, instance).targets()
        if targets and _matches(document, _index(targets)):
            return _retire(document)
    return document


def _sweep_admin(home, instance_id, targets, current=None, roots=None):
    directory = home / 'runtime' / 'administration'
    if not _exists(directory):
        return 0
    _owned_dir(directory)
    ids = _index(targets)
    count = 0
    for path in directory.iterdir():
        match = re.fullmatch('(?:(restart)-)?(' + _UUID + r')\.json', path.name)
        if not match or match[2] == current:
            continue
        doc = _json(path)
        operation = doc['original']['operation'] if match[1] else doc
        if (operation.get('schema') != 1 or operation.get('operation_id') != match[2]
                or operation.get('home') != str(home)):
            _unsafe()
        if operation.get('step') == 'redacted' or _redaction_control(operation):
            continue
        instances = _admin_instances(operation)
        if instance_id not in instances:
            if not instances and _matches(operation, ids):
                raise SetupPending('A matching local administration request lacks a verified instance binding; preserved for private attribution.')
            continue
        if roots is not None and operation.get('kind') in ('project', 'home'):
            for approval in operation.get('approvals', []):
                review = approval.get('review') or {}
                selection = review.get('selection') or {}
                root = selection.get('root')
                if isinstance(root, str) and selection.get('mode') != 'create_cloud':
                    roots.add(root)
        if not _matches(operation, ids):
            continue
        if match[1]:
            if not doc.get('completed'):
                _unsafe()
            doc['original']['operation'] = _retire(operation)
            doc['source'] = {'redacted': True}
        else:
            doc = _retire(operation)
        _changed(path, doc)
        count += 1
    return count


def _sweep_journal(path, ids):
    if not _exists(path):
        return 0
    doc = _json(path)
    if doc.get('state') == 'redacted' or not _matches(doc, ids):
        return 0
    # Discard the original body and approval, including on uncertain outcomes;
    # a content-free, nonrequest document cannot pass legacy replay validation.
    receipt = {'state': 'redacted', 'replayable': False}
    body = doc.get('request', doc).get('body', {})
    for field in ('record_id', 'request_key'):
        value = body.get(field)
        if isinstance(value, str) and re.fullmatch(_UUID, value):
            receipt[field] = value
    _changed(path, receipt)
    return 1


def _sweep_maintenance(path, ids):
    if not _exists(path):
        return 0
    doc = _json(path)
    if doc.get('schema') != 1:
        _unsafe()
    changed = False
    retired = set()
    for field in ('assignments', 'checkpoints'):
        for key, item in doc.get(field, {}).items():
            record_uuid(key)
            selected = key in ids['record'] or key in ids['operation']
            effects = item.get('retained_effects', []) if item.get('redacted') else item.get('effects', [])
            keep = [] if selected else [effect for effect in effects if not _matches(effect, ids)]
            if item.get('redacted'):
                if keep != effects:
                    item['retained_effects'] = keep
                    changed = True
                retired.add(key)
                continue
            if (selected or _matches(item, ids)
                    or any(ref in retired for ref in item.get('assignments', []))):
                marker = {'state': 'cancelled', 'redacted': True,
                          'operation_id' if field == 'assignments' else 'checkpoint_id': key}
                if type(item.get('sequence')) is int:
                    marker['sequence'] = item['sequence']
                if keep:
                    # A mixed assignment loses its executable plan, not the
                    # evidence of unrelated work. Further markers scrub it too.
                    marker['retained_effects'] = keep
                doc[field][key] = marker
                retired.add(key)
                changed = True
    if changed:
        _changed(path, doc)
    return int(changed)


def _sweep_skills(directory, project, session, ids, bundle_check):
    if not _exists(directory) or not (ids['page'] or ids['skill_bundle']):
        return 0
    _owned_dir(directory)
    registry = directory / 'registry.json'
    doc = _json(registry) if _exists(registry) else None
    if doc is not None and (doc.get('session_id') != session or doc.get('project_id') != project
                            or not isinstance(doc.get('pages'), list)):
        _unsafe()
    selected, hashes = set(), set()
    pages = doc['pages'] if doc is not None else []
    for page in pages:
        sha = (page.get('bundle') or {}).get('sha256')
        if sha is not None:
            if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
                _unsafe()
            hashes.add(sha)
    archives = []
    for path in directory.iterdir():
        match = _ZIP.fullmatch(path.name)
        if match and len(match[1]) <= 80:
            hashes.add(match[2])
            archives.append((path, match[2]))
        elif path.name.startswith(('.download-', '.metadata-')):
            # Incomplete historical cache writes have no verified ownership key.
            _unsafe()
    if ids['skill_bundle']:
        # A stale registry is NOT authority to purge bytes. Even an archive
        # listed under a retired page must independently match the retired set.
        candidates = sorted(hashes)
        if candidates and bundle_check is None:
            raise SetupPending('Managed skill cache membership requires signed bundle verification. Reconcile this installation after sign-in; no unverified archive was removed.')
        for start in range(0, len(candidates), 50):
            batch = candidates[start:start+50]
            indexes = bundle_check(batch)
            if not isinstance(indexes, set) or any(type(i) is not int or not 0 <= i < len(batch) for i in indexes):
                _unsafe()
            selected.update(batch[i] for i in indexes)
    changed = 0
    for path, sha in archives:
        if sha not in selected:
            continue
        _file(path)
        # Do not delete an unrelated/edited owner file just because its name matches.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(fd)
            _file_info(before)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                _unsafe()
            now = path.lstat()
            if (now.st_dev, now.st_ino, now.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_ctime_ns):
                _unsafe()
        path.unlink()
        changed += 1
    if changed:
        fd = os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    keep = [page for page in pages if page.get('page_id') not in ids['page'] and (page.get('bundle') or {}).get('sha256') not in selected]
    if ids['page'] and any(not page.get('page_id') for page in keep):
        raise SetupPending('Legacy skill metadata lacks an exact page identity; retained for private attribution.')
    if len(keep) != len(pages):
        _changed(registry, {**doc, 'pages': keep})
        changed += 1
    return changed


def _sweep_workspace(root, project, private_ids):
    if not private_ids:
        return 0
    directory = root / '.maven' / 'runtime' / 'workspace' / project
    if not _exists(directory):
        return 0
    _owned_dir(directory)
    count = 0
    with private_lock(root, '.maven/runtime/workspace.lock', timeout=LOCK_TIMEOUT):
        for path in directory.iterdir():
            if not re.fullmatch('(?:' + _UUID + r'|[0-9a-f]{64})\.json', path.name):
                continue
            journal = _json(path)
            if journal.get('project_id') != project:
                _unsafe()
            key = record_uuid(journal['operation_id'])
            if journal.get('phase') == 'redacted':
                continue
            referenced = {key, (journal.get('request') or {}).get('record_id')}
            for effect in journal.get('effects', {}).values():
                referenced.add(effect['envelope']['record']['id'])
            if private_ids.isdisjoint(referenced):
                continue
            # The filename is the existing exact-retry lookup key. Keep it spent;
            # never follow stored_path/source_path/output or remove owners' originals.
            _changed(path, {'operation_id': key, 'project_id': project, 'phase': 'redacted'})
            count += 1
    return count


def _forget_project_name(private, project):
    path = private / 'project-name.json'
    if not _exists(path):
        return 0
    if _json(path).get('project_id') != project:
        _unsafe()
    path.unlink()
    return 1


def _sweep_root(root, project, targets, bundle_check):
    private = root / '.maven'
    _owned_dir(private)
    ids = _index(targets)
    # Project identity redaction does NOT retire every unrelated unsynced legacy
    # request. Only its exact record/dependency closure is authorized.
    setup = private / 'runtime' / 'project-setup'
    touched = _forget_project_name(private, project) if project in ids['project'] else 0
    if project in ids['project'] and _exists(setup / 'state.json'):
        _owned_dir(setup)
        with setup_lock(setup):
            state = _json(setup / 'state.json')
            if (state.get('selection') or {}).get('project_id') == project:
                _changed(setup / 'state.json', {'state': 'redacted', 'replayable': False, 'project_id': project,
                         'instance_id': (state.get('identity') or {}).get('instance_id'), 'root': state.get('root')})
                touched += 1
    ids['project'] = set()
    if not any(ids.values()):
        return touched
    touched += _sweep_journal(private / 'pending-memory.json', ids)
    sessions = private / 'runtime' / 'sessions'
    if _exists(sessions):
        _owned_dir(sessions)
        for directory in sessions.iterdir():
            if not re.fullmatch(_UUID, directory.name):
                continue
            _owned_dir(directory)
            if not any(_exists(directory / name) for name in
                       ('pending-memory.json', 'failed-memory-write.json', 'last-memory-write.json', 'maintenance.json', 'skills')):
                continue
            relative = directory.relative_to(root)
            with private_lock(root, relative / 'command.lock', timeout=LOCK_TIMEOUT), \
                    private_lock(root, relative / 'maintenance.lock', timeout=LOCK_TIMEOUT):
                for name in ('pending-memory.json', 'failed-memory-write.json', 'last-memory-write.json'):
                    touched += _sweep_journal(directory / name, ids)
                touched += _sweep_maintenance(directory / 'maintenance.json', ids)
                touched += _sweep_skills(directory / 'skills', project, directory.name, ids, bundle_check)
    return touched


def _bound_instance(private, binding):
    if binding.get('schema') != 1:
        return binding.get('instance_id')
    # Old project.json had no instance identifier. A project UUID or current
    # environment alone cannot attest ownership. Accept the original pinned
    # setup receipt (including its content-free retired form), never a guess.
    path = private / 'runtime' / 'project-setup' / 'state.json'
    if not _exists(path):
        return None
    state = _json(path)
    if state.get('root') != binding['root']:
        _unsafe()
    if state.get('state') == 'redacted' and state.get('project_id') == binding['project_id']:
        return state.get('instance_id')
    selection = state.get('selection') or {}
    if state.get('schema') == 1 and selection.get('project_id') == binding['project_id'] and selection.get('root') == binding['root']:
        return (state.get('identity') or {}).get('instance_id')
    return None


def _archive_absent(archive):
    # Only ENOENT counts as absence. Broken links, partial archives and access
    # errors must not become permission to skip a preserved root.
    try:
        archive.lstat()
    except FileNotFoundError:
        archive.parent.stat()
        return True
    return False


def _disposition(candidate, archive, instance_id):
    return {'schema': 1, 'state': 'owner_acknowledged_deleted', 'instance_id': instance_id,
            'operation_id': candidate['operation_id'], 'archive': str(archive),
            'project_id': record_uuid(candidate['old_project']['project_id']),
            'old_directory': candidate['old_directory'], 'parent_inode': archive.parent.stat().st_ino}


def acknowledge_deleted_archive(home, archive):
    """Local owner attestation; never edit a switch journal, archive, or cloud record."""
    match = re.fullmatch(r'\.maven-preserved-(' + _UUID + ')', archive.name)
    if not match or not archive.is_absolute() or archive != archive.resolve():
        raise SetupPending('Specify the exact absolute preserved archive path from the failure.')
    directory = home / 'runtime' / 'project-switches'
    _owned_dir(directory)
    key = match[1]
    path = directory / (key + '.json')
    journal = _json(path)
    candidate = journal['intent']['candidate']
    instance = record_uuid(candidate['instance_id'])
    root = Path(candidate['root'])
    with Replica(home, instance).locked(), project_binding_lock(home, root, shared=True):
        if (_json(path) != journal or journal.get('phase') != 'completed'
                or candidate['operation_id'] != key or candidate['preservation_path'] != str(archive)
                or root.parent != archive.parent or root != root.resolve() or not root.is_dir()
                or candidate['old_project']['root'] != str(root)
                or candidate['old_project'].get('instance_id', instance) != instance):
            raise SetupPending('A matching completed switch at this installation is required.')
        if not _archive_absent(archive):
            raise SetupPending('The original archive path is occupied; no acknowledgment was recorded.')
        marker = directory / (key + '.disposition.json')
        receipt = _disposition(candidate, archive, instance)
        if _exists(marker):
            if _json(marker) != receipt:
                _unsafe()
        else:
            save_json(marker, receipt)
        return receipt


def _sweep_preserved(home, instance_id, targets, bundle_check, receive_project=None):
    directory = home / 'runtime' / 'project-switches'
    if not _exists(directory):
        return 0
    _owned_dir(directory)
    count = 0
    for path in directory.iterdir():
        match = re.fullmatch('(' + _UUID + r')\.json', path.name)
        if not match:
            continue
        journal = _json(path)
        candidate = journal['intent']['candidate']
        old = candidate['old_project']
        retired_projects = {t['id'] for t in targets if t['kind'] == 'project'}
        destination_retired = (candidate.get('instance_id') == instance_id
                               and (candidate.get('new_project') or {}).get('project_id') in retired_projects)
        if old.get('instance_id') not in (None, instance_id) and not destination_retired:
            continue
        if old.get('instance_id') is None and candidate.get('instance_id') != instance_id and not destination_retired:
            continue
        if journal.get('phase') != 'completed':
            if not targets:
                continue  # No known work to apply; an unfinished switch is already barred.
            raise SetupPending('A reviewed project preservation is in progress. Finish it before claiming a local sweep.')
        project = record_uuid(old['project_id'])
        root = Path(candidate['root'])
        archive = root.parent / ('.maven-preserved-' + match[1])
        if (not root.is_absolute() or root != root.resolve() or candidate['operation_id'] != match[1]
                or old['root'] != str(root) or candidate['preservation_path'] != str(archive)):
            _unsafe()
        disposition = directory / (match[1] + '.disposition.json')
        acknowledged_absence = (_exists(disposition) and _archive_absent(archive)
                                and candidate.get('instance_id') == instance_id
                                and _json(disposition) == _disposition(candidate, archive, instance_id))
        if acknowledged_absence:
            # No archive to inspect. Only the switch file can establish the old instance.
            old_bound = old.get('instance_id') == instance_id
        else:
            if not _exists(archive) or not _exists(archive / '.maven'):
                raise PreservedArchiveUnavailable(f'Preserved archive {archive.name} is missing or incomplete.')
            _owned_dir(archive)
            private = archive / '.maven'
            _owned_dir(private)
            # A remount can assign a different device number to the same directory.
            if candidate['old_directory'].get('inode') != private.stat().st_ino:
                raise PreservedArchiveUnavailable(f'Preserved archive {archive.name} is not the recorded directory.')
            binding = _json(private / 'project.json')
            if binding.get('project_id') != project or binding.get('root') != str(root):
                _unsafe()
            old_bound = _bound_instance(private, binding) == instance_id
            if old.get('instance_id') == instance_id and not old_bound:
                _unsafe()
        if old_bound and receive_project is not None:
            targets = receive_project(project)
            retired_projects = {t['id'] for t in targets if t['kind'] == 'project'}
        # Retire the replayable switch intent (including the full destination
        # entity and old approval), but keep the pinned preservation locator.
        if destination_retired or old_bound and project in retired_projects:
            if not journal.get('redacted'):
                pinned = {key: candidate[key] for key in ('operation_id', 'root', 'old_directory', 'preservation_path')}
                if 'instance_id' in candidate:
                    pinned['instance_id'] = candidate['instance_id']
                pinned['old_project'] = {key: old[key] for key in ('root', 'project_id', 'instance_id') if key in old}
                if old_bound:
                    pinned['old_project']['instance_id'] = instance_id
                _changed(path, {'schema': 1, 'phase': 'completed', 'redacted': True,
                                'intent': {'candidate': pinned}})
                count += 1
        if acknowledged_absence or not old_bound:
            continue
        store = LocalStore(archive, project)
        if _exists(store.path):
            store.install_redactions([t['id'] for t in targets if t.get('project_id') == project],
                                     retired={'kind': 'project', 'id': project} in targets)
        count += _sweep_workspace(archive, project, {t['id'] for t in targets if t.get('project_id') == project})
        count += _sweep_root(archive, project, targets, bundle_check)
    return count


def _sweep_home(home, instance_id, targets):
    path = home / 'home.json'
    if not _exists(path):
        return 0
    selected = _json(path)
    if (selected.get('schema') == 1 and selected.get('instance_id') == instance_id
            and {'kind': 'project', 'id': selected.get('project_id')} in targets):
        clean = {**selected, 'label': 'Redacted', 'consent': {'origin': 'redaction', 'excerpt': 'Project retired'}}
        if clean != selected:
            _changed(path, clean)
            return 1
    return 0


def sweep_reviewed(home, instance_id, targets, *, current_operation, bundle_check=None):
    """Post-commit only. Caller holds administration setup_lock; never a global scan."""
    replica = Replica(home, instance_id)
    home = replica.home
    with replica.locked():
        replica.remember(targets)
        targets = replica.targets()
        roots = set()
        count = _sweep_admin(home, instance_id, targets, record_uuid(current_operation), roots)
        registry = home / 'runtime' / 'sessions.json'
        if _exists(registry):
            with private_lock(home, 'runtime/registry.lock', shared=True, timeout=LOCK_TIMEOUT):
                state = _json(registry)
            if state.get('schema') != 1 or not isinstance(state.get('sessions'), dict):
                _unsafe()
            roots.update(r['root'] for r in state['sessions'].values() if isinstance(r, dict) and isinstance(r.get('root'), str))
        selection = home / 'home.json'
        if _exists(selection):
            selected = _json(selection)
            if selected.get('instance_id') == instance_id:
                roots.add(selected['root'])
        checked = 0
        for name in sorted(roots):
            root = Path(name)
            if not root.exists():
                continue  # Unreachable roots remain unverified; never claim a device census.
            if not root.is_absolute() or root != root.resolve():
                _unsafe()
            with project_binding_lock(home, root, shared=False):
                if not _exists(root / '.maven' / 'project.json'):
                    continue
                binding = load_project(root)
                if _bound_instance(root / '.maven', binding) != instance_id:
                    continue
                store = LocalStore(root, binding['project_id'])
                if _exists(store.path):
                    replica.protect(store, delivering=True)
                count += _sweep_root(root, store.project_id, targets, bundle_check)
                checked += 1
        count += _sweep_preserved(home, instance_id, targets, bundle_check)
        count += _sweep_home(home, instance_id, targets)
        return {'known_roots_checked': checked, 'artifacts_changed': count, 'offline_copies': 'unverified'}
