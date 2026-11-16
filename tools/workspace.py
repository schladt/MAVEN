"""DB-backed workspace metadata with private originals and explicit dispositions.

Commands never promote reference text into instructions. Override authority is an
already approved DB directive, not the derived setting written by this helper.
"""
from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid

from tools.memory.boundary import BoundaryError, MemoryBoundary, digest, private_path, read_private, save_private

SECTIONS = ('Progress', 'Milestones', 'Blockers', 'Next Steps', 'Client Actions')


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextlib.contextmanager
def workspace_lock(root, *, blocking=False, writers_quiescent=False):
    """Kernel-held cooperative lock; legacy existence locks require explicit recovery."""
    from tools.coordination import private_lock
    with private_lock(root, '.maven/runtime/cloud-workspace-v2.lock', blocking=blocking):
        legacy = private_path(root, '.maven/runtime/cloud-workspace.lock')
        if legacy.exists():
            if not writers_quiescent:
                raise BoundaryError('legacy workspace lock has ambiguous ownership; stop its writers and run '
                                    f'maven-workspace --root {shlex.quote(str(root))} recover-lock --writers-quiescent')
            # Do not reinterpret an old process's existence lock as a kernel lock.
            # Preserve the evidence and require the caller's actual quiescence.
            evidence = read_private(root, '.maven/runtime/cloud-workspace-legacy.json') if private_path(
                root, '.maven/runtime/cloud-workspace-legacy.json').exists() else {}
            evidence.update(recovered=True, writers_quiescent=True, identity=list(identity(legacy)))
            save_private(root, '.maven/runtime/cloud-workspace-legacy.json', evidence)
            legacy.unlink()
        yield


def copy_original(root, source, target, expected, initial):
    target = private_path(root, target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    if target.exists():
        if not target.is_file() or sha(target) != expected:
            raise BoundaryError('private original destination collision; source retained')
        return target
    fd, temporary = tempfile.mkstemp(prefix='.original-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as stream:
            os.fchmod(output.fileno(), 0o600)
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if sha(Path(temporary)) != expected or identity(source) != initial or sha(source) != expected:
            raise BoundaryError('original changed during preservation; source retained')
        # Hard-link is an atomic no-overwrite publish on the same filesystem.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


class WorkspaceMemory(MemoryBoundary):
    """Keep workspace receipts inside the same maintenance cutoff as ordinary notes."""
    def __init__(self, service):
        from tools.memory.maintenance import Maintenance
        super().__init__(service.client, service.root, service.project_id, service.session_id)
        self.service = service
        self.maintenance = Maintenance(service)

    def write(self, kind, data, *, record_id=None, expected_revision=0, tags=(),
              reason='Record managed work', source=None, scope='project', request_key=None):
        from tools.memory.boundary import digest
        record_id = record_id or str(uuid.uuid4())
        source = source if source is not None else {
            'origin': 'governed-workspace', 'excerpt': 'Explicit workspace operation: ' + reason}
        operation_id = str(uuid.uuid5(uuid.UUID(self.session_id), 'workspace-write:' + digest({
            'record_id': record_id, 'expected_revision': expected_revision, 'kind': kind,
            'data': data, 'tags': list(tags), 'source': source, 'reason': reason, 'scope': scope,
            'request_key': request_key})))
        result = self.maintenance.save(
            kind, data, source=source, record_id=record_id, expected_revision=expected_revision,
            tags=tags, reason=reason, scope=scope, request_key=request_key, operation_id=operation_id)
        refs = [ref for ref in result['assignment']['saved_refs'] if ref['record_id'] == record_id]
        if result.get('cloud_saved') is not True or len(refs) != 1:
            raise BoundaryError('workspace memory mutation has no exact verified receipt')
        return self.service.read_record(record_id, revision=refs[0]['revision'], scope=scope,
                                        include_archived='archived' in tags)


class Workspace:
    def __init__(self, memory):
        self.memory = memory
        self.root = memory.root.resolve()


    def _begin(self, name, request, operation_id):
        from tools.coordination import session_uuid
        session_id = self.memory.session()['record_id']
        key = session_uuid(operation_id) if operation_id is not None else digest(
            {'session_id': session_id, 'name': name, 'request': request})
        path = '.maven/runtime/workspace/' + key + '.json'
        if operation_id is not None and not private_path(self.root, path).exists():
            matches = []
            for candidate in private_path(self.root, '.maven/runtime/workspace').glob('*.json'):
                if read_private(self.root, candidate.relative_to(self.root)).get('operation_id') == operation_id:
                    matches.append(str(candidate.relative_to(self.root)))
            if len(matches) > 1:
                raise BoundaryError('workspace operation UUID has ambiguous local evidence')
            if matches:
                path = matches[0]
        if private_path(self.root, path).exists():
            journal = read_private(self.root, path)
            if (journal.get('project_id') != self.memory.project_id or journal.get('session_id') != session_id
                    or journal.get('name') != name or journal.get('request') != request):
                raise BoundaryError('workspace operation identity belongs to a different exact request')
        else:
            journal = {'operation_id': operation_id or str(uuid.uuid4()), 'project_id': self.memory.project_id,
                       'session_id': session_id, 'name': name, 'request': request, 'phase': 'prepared', 'effects': {}}
            save_private(self.root, path, journal)
        return path, journal

    def _phase(self, path, journal, phase, **fields):
        journal.update(fields, phase=phase)
        save_private(self.root, path, journal)

    def _effect(self, path, journal, step, kind, data, **kwargs):
        """Persist each exact request before submission and replay through the original receipt."""
        effects = journal['effects']
        if step not in effects:
            kwargs.setdefault('record_id', str(uuid.uuid4()))
            kwargs.setdefault('expected_revision', 0)
            effects[step] = deepcopy({'kind': kind, 'data': data, 'kwargs': kwargs})
            save_private(self.root, path, journal)
        effect = effects[step]
        if 'record' in effect:
            return effect['record']
        # WorkspaceMemory derives the same maintenance UUID and request key from
        # these retained bytes. Never infer success merely from similar current data.
        record = self.memory.write(effect['kind'], effect['data'], **effect['kwargs'])
        effect['record'] = record
        save_private(self.root, path, journal)
        return record

    def _store(self, path, journal, step, key, value):
        previous = None if step in journal['effects'] else self.setting(key)
        return self._effect(path, journal, step, 'setting',
            {'key': key, 'value': value, 'scope': 'project'}, record_id=self.setting_id(key),
            expected_revision=previous['revision'] if previous else 0,
            tags=['workspace-metadata'], reason='Record explicitly governed workspace metadata')

    def _operation(self, path, journal, state, metadata):
        summary = json.dumps(metadata, sort_keys=True)
        if len(summary) > 16000:
            raise BoundaryError('workspace operation metadata exceeds bounded receipt size')
        return self._effect(path, journal, state, 'operation',
            {'session_id': journal['session_id'], 'name': journal['name'], 'state': state,
             'summary': summary}, record_id=journal['operation_id'],
            expected_revision=0 if state == 'started' else 1, tags=['workspace-operation'],
            reason='Record governed local source disposition')

    def _finish(self, path, journal, metadata, result):
        self._operation(path, journal, 'completed', metadata)
        result = dict(result, operation_id=journal['operation_id'])
        self._phase(path, journal, 'completed', result=result)
        return result
    def setting_id(self, key):
        return str(uuid.uuid5(uuid.UUID(self.memory.project_id), 'workspace:' + key))

    def setting(self, key):
        from tools.memory.client import MemoryClientError
        try:
            record = self.memory.read(self.setting_id(key))
        except MemoryClientError as error:
            if error.code == 'record_unavailable':
                return None
            raise
        if record['kind'] != 'setting' or record['data']['key'] != key:
            raise BoundaryError('workspace setting identity conflicts with existing DB metadata')
        return record

    def store(self, key, value, previous=None):
        return self.memory.write('setting', {'key': key, 'value': value, 'scope': 'project'},
            record_id=self.setting_id(key), expected_revision=previous['revision'] if previous else 0,
            tags=['workspace-metadata'], reason='Record explicitly governed workspace metadata')


    def pending(self):
        incoming = private_path(self.root, '.maven/incoming')
        counts = dict(files=0, directories=0, symlinks=0, hidden=0, unreadable=0)
        def failed(error):
            counts['unreadable'] += 1
        for directory, dirs, files in os.walk(incoming, followlinks=False, onerror=failed):
            for name in dirs + files:
                path = Path(directory) / name
                if name == '.gitkeep' and not path.is_symlink() and path.is_file() and path.stat().st_size == 0:
                    continue
                if name.startswith('.'):
                    counts['hidden'] += 1
                if path.is_symlink():
                    counts['symlinks'] += 1
                elif path.is_dir():
                    counts['directories'] += 1
                else:
                    counts['files'] += 1
        return {'status': 'ready', 'pending': counts, 'contents_opened': False}

    def intake(self, *, path, kind, origin, sensitivity, authorization, disposition, confirm=False,
               reviewed=False, operation_id=None):
        if kind not in ('reference', 'operational', 'artifact') or sensitivity != 'Sensitive':
            raise BoundaryError('one explicit supported classification is required before opening the source')
        if not origin.strip() or not authorization.strip() or disposition not in ('retain', 'archive', 'delete'):
            raise BoundaryError('explicit provenance, authorization and disposition are required')
        if disposition != 'retain' and not confirm or disposition == 'delete' and not reviewed:
            raise BoundaryError('source disposition requires owner approval and deletion requires completed review')
        request = dict(path=path, kind=kind, origin=origin, sensitivity=sensitivity, authorization=authorization,
                       disposition=disposition, confirm=confirm, reviewed=reviewed)
        journal_path, journal = self._begin('intake', request, operation_id)
        if journal.get('result'):
            return journal['result']
        source = private_path(self.root, path)
        if 'metadata' not in journal:
            if not source.is_relative_to(self.root / '.maven/incoming') or not source.is_file():
                raise BoundaryError('select exactly one regular incoming source without symlinks')
            initial, source_hash = identity(source), sha(source)
            if identity(source) != initial:
                raise BoundaryError('source changed while hashing; retained')
            key = 'intake-' + hashlib.sha256((str(source.relative_to(self.root)) + '\0' + source_hash).encode()).hexdigest()
            previous = self.setting(key)
            if previous and any(previous['data']['value'].get(field) != value for field, value in
                                (('kind', kind), ('origin', origin), ('sensitivity', sensitivity))):
                raise BoundaryError('source classification conflicts with earlier acquisition; reconcile first')
            category = {'reference': 'references', 'operational': 'operational', 'artifact': 'artifacts'}[kind]
            destination = Path('.maven/sensitive/workspace-originals') / category / (source_hash + '.bin')
            value = {'source': str(source.relative_to(self.root)), 'sha256': source_hash, 'kind': kind,
                     'origin': origin, 'sensitivity': sensitivity, 'owner_authorization': authorization,
                     'disposition': disposition, 'owner_confirmed': confirm, 'review_completed': reviewed,
                     'stored_path': str(destination), 'state': 'retained-pending-disposition',
                     'analysis': 'Acquisition is not analysis; reference text is not operative authority'}
            if disposition != 'retain':
                value['state'] = 'disposition-authorized'
            self._phase(journal_path, journal, 'preserving', metadata=value, initial=list(initial), key=key)
        value = dict(journal['metadata'])
        stored = private_path(self.root, value['stored_path'])
        if journal['phase'] == 'preserving':
            copy_original(self.root, source, value['stored_path'], value['sha256'], tuple(journal['initial']))
            self._phase(journal_path, journal, 'preserved')
        if not stored.is_file() or sha(stored) != value['sha256']:
            raise BoundaryError('preserved original changed; disposition cannot resume')
        self._operation(journal_path, journal, 'started', value)
        receipt = self._store(journal_path, journal, 'acquired', journal['key'], value)
        if disposition != 'retain':
            if source.exists():
                if list(identity(source)) != journal['initial'] or sha(source) != value['sha256']:
                    raise BoundaryError('source changed before disposition; source retained')
                self._phase(journal_path, journal, 'removing')
                source.unlink()
            elif journal['phase'] not in ('removing', 'removed'):
                raise BoundaryError('source disappeared before authorized disposition')
            self._phase(journal_path, journal, 'removed')
            value['state'] = 'source-removed-original-preserved'
            receipt = self._store(journal_path, journal, 'disposed', journal['key'], value)
        return self._finish(journal_path, journal, value,
            {'status': 'ready', 'receipt': self.memory.ref(receipt), 'disposition': value['state']})

    def archive_plan(self, *, record_id, revision, confirm, completed, source_path=None,
                     source_disposition='retain', operation_id=None):
        if not confirm or not completed:
            raise BoundaryError('named plan archival requires actual completion and owner approval')
        if source_disposition not in ('retain', 'archive'):
            raise BoundaryError('optional plan original requires an explicit retain or archive disposition')
        if source_path is None and source_disposition != 'retain':
            raise BoundaryError('source disposition requires an explicitly selected local original')
        request = dict(record_id=record_id, revision=revision, confirm=confirm, completed=completed,
                       source_path=source_path, source_disposition=source_disposition)
        path, journal = self._begin('archive-plan', request, operation_id)
        if journal.get('result'):
            return journal['result']
        if 'metadata' not in journal:
            plan = self.memory.read(record_id)
            if plan['revision'] != revision or plan['kind'] != 'note' or plan['data']['category'] != 'plan':
                raise BoundaryError('archive requires the exact current DB plan note revision')
            metadata = {'plan': self.memory.ref(plan), 'owner_confirmed': True,
                        'actual_completion_attested': True, 'state': 'archive-authorized',
                        'source_disposition': source_disposition,
                        'links': 'Stable DB record ID retained; old revisions remain readable'}
            initial = None
            if source_path is not None:
                source = private_path(self.root, source_path)
                permitted = (self.root / '.maven/planning', self.root / '.maven/sensitive')
                if not any(source.is_relative_to(directory) for directory in permitted) or not source.is_file():
                    raise BoundaryError('optional original must be an explicitly selected private regular file')
                initial, source_hash = identity(source), sha(source)
                target = Path('.maven/sensitive/workspace-originals/plans') / (
                    dt.date.today().isoformat() + '-' + record_id + '-' + source_hash + '.bin')
                metadata.update(source=source_path, stored_path=str(target), sha256=source_hash)
            self._phase(path, journal, 'preserving', metadata=metadata, plan=plan,
                        initial=list(initial) if initial else None)
        metadata = dict(journal['metadata'])
        source = private_path(self.root, source_path) if source_path is not None else None
        if source is not None:
            stored = private_path(self.root, metadata['stored_path'])
            if journal['phase'] == 'preserving':
                copy_original(self.root, source, metadata['stored_path'], metadata['sha256'], tuple(journal['initial']))
            if not stored.is_file() or sha(stored) != metadata['sha256']:
                raise BoundaryError('preserved plan original changed; archival cannot resume')
        if journal['phase'] == 'preserving':
            self._phase(path, journal, 'preserved')
        self._operation(path, journal, 'started', metadata)
        plan = journal['plan']
        updated = self._effect(path, journal, 'archive', 'note', dict(plan['data'], category='archived-plan'),
            record_id=record_id, expected_revision=revision, tags=list(dict.fromkeys([*plan['tags'], 'archived'])),
            reason='Archive completed plan; stable DB links and history retained')
        key = 'archive-' + record_id
        self._store(path, journal, 'authorized', key, metadata)
        if source is not None and source_disposition == 'archive' and source != stored:
            if source.exists():
                if list(identity(source)) != journal['initial'] or sha(source) != metadata['sha256']:
                    raise BoundaryError('plan original changed before disposition; source retained')
                self._phase(path, journal, 'removing')
                source.unlink()
            elif journal['phase'] not in ('removing', 'removed'):
                raise BoundaryError('plan original disappeared before authorized disposition')
            self._phase(path, journal, 'removed')
        metadata.update(state='archived', plan=self.memory.ref(updated))
        receipt = self._store(path, journal, 'archived', key, metadata)
        return self._finish(path, journal, metadata,
            {'status': 'ready', 'receipt': self.memory.ref(receipt), 'plan': self.memory.ref(updated)})

    def override(self, *, identifier, slug, revision, content_hash, operation, confirm):
        if not confirm or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', identifier):
            raise BoundaryError('override metadata requires exact owner confirmation and a stable slug')
        page = self.memory.client.read_full({'slug': slug, 'expected_revision': revision})['data']
        state = 'active' if operation == 'record' else 'revoked'
        if (page.get('page_kind') != 'directive' or page.get('revision') != revision
                or page.get('content_hash') != content_hash or page.get('authority_state') != state
                or page.get('candidate_authority_state') != state or page.get('data_scope') != 'owner'):
            raise BoundaryError('override requires the exact already-approved current directive state')
        if state == 'active':
            context = self.memory.context()
            if not any(item['slug'] == slug and item['revision'] == revision and item['content_hash'] == content_hash
                       for item in context['instructions']):
                raise BoundaryError('override directive is not applicable to this project context')
        key = 'override-' + identifier
        previous = self.setting(key)
        if previous and previous['data']['value']['slug'] != slug:
            raise BoundaryError('override identifier belongs to a different approved directive')
        if state == 'revoked' and (not previous or previous['data']['value']['state'] != 'active'):
            raise BoundaryError('override is not active; no metadata revocation performed')
        value = {'slug': slug, 'revision': revision, 'content_hash': content_hash, 'state': state,
                 'operative': False, 'authority': 'Only the current approved directive governs; this setting grants no permission'}
        receipt = self.store(key, value, previous)
        return {'status': 'ready', 'receipt': self.memory.ref(receipt), 'authority': value}

    def status(self, *, record_id, revision, output=None, check=False, operation_id=None):
        journal = None
        if output is not None and not check:
            if output != 'team-status.md':
                raise BoundaryError('external status output must use the governed team-status.md path')
            path, journal = self._begin('status', dict(record_id=record_id, revision=revision, output=output), operation_id)
        record = journal['source_record'] if journal and 'source_record' in journal else self.memory.read(record_id)
        value = record['data'].get('value', {})
        if (record['revision'] != revision or record['kind'] != 'setting' or record['data']['key'] != 'project-status'
                or value.get('external_status_enabled') is not True):
            raise BoundaryError('external status is disabled or its current DB source receipt is stale')
        fields = value.get('publishable')
        if not isinstance(fields, dict) or set(fields) != set(SECTIONS) or not all(isinstance(v, str) for v in fields.values()):
            raise BoundaryError('status requires all explicit publishable sections; internal content is never a fallback')
        rendered = '# Team Status\n\n' + '\n\n'.join('## ' + section + '\n\n' + fields[section] for section in SECTIONS) + '\n'
        from tools.git_workflow import SECRET_PATTERNS
        if any(pattern.search(rendered) for _, pattern in SECRET_PATTERNS):
            raise BoundaryError('publishable status contains a possible secret; no output generated')
        warnings = []
        if re.search(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b|\b(?:\d{1,3}\.){3}\d{1,3}\b|\.maven/', rendered):
            warnings.append('publishable fields contain possible private identifiers; explicit team readership review required')
        result = {'status': 'ready', 'source': self.memory.ref(record), 'publishable': fields, 'warnings': warnings}
        if output is None:
            return result
        if output != 'team-status.md':
            raise BoundaryError('external status output must use the governed team-status.md path')
        target = private_path(self.root, output)
        previous = self.setting('team-status-receipt')
        before = target.read_bytes() if target.exists() and target.is_file() else None
        output_hash = hashlib.sha256(rendered.encode()).hexdigest()
        owned = previous and previous['data']['value'].get('output_sha256') == hashlib.sha256(before or b'').hexdigest()
        installed = bool(journal and journal['phase'] in ('installing', 'installed', 'completed')
                         and before == rendered.encode())
        if target.exists() and not owned and not installed:
            raise BoundaryError('status destination collision or local modification; existing content retained')
        current = bool(previous and previous['data']['value'].get('source') == self.memory.ref(record)
                       and previous['data']['value'].get('output_sha256') == output_hash and before == rendered.encode())
        if check:
            return dict(result, current=current)
        if journal.get('result'):
            return dict(journal['result'], current=current)
        metadata = {'source': self.memory.ref(record), 'output_path': output, 'output_sha256': output_hash}
        if 'source_record' not in journal:
            self._phase(path, journal, 'prepared', source_record=record,
                        before_sha256=hashlib.sha256(before).hexdigest() if before is not None else None)
        self._operation(path, journal, 'started', metadata)
        if not installed:
            if self.memory.read(record_id)['revision'] != revision:
                raise BoundaryError('DB status source changed during generation')
            before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
            if before_hash != journal['before_sha256']:
                raise BoundaryError('status output changed since original operation; prior bytes retained')
            if before is not None:
                saved = Path('.maven/sensitive/workspace-originals/status') / (before_hash + '.bin')
                copy_original(self.root, target, saved, before_hash, identity(target))
            self._phase(path, journal, 'installing')
            fd, temporary = tempfile.mkstemp(prefix='.status-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(rendered.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                if before is None:
                    # Atomic no-overwrite even if an external editor creates the target.
                    os.link(temporary, target)
                else:
                    # Existing-output replacement excludes cooperating writers only.
                    # Check as late as possible; arbitrary same-user editors are not sandboxed.
                    if private_path(self.root, output).read_bytes() != before:
                        raise BoundaryError('status output changed during generation; existing content retained')
                    os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self._phase(path, journal, 'installed')
        receipt = self._store(path, journal, 'receipt', 'team-status-receipt', metadata)
        return self._finish(path, journal, metadata, dict(result, receipt=self.memory.ref(receipt), current=True))


def main(argv=None):
    from tools.memory.client import MemoryClientError, read_json
    from tools.memory.environment import configured_client
    from tools.project import default_environment
    from tools.coordination import SessionRegistry
    from tools.memory.lifecycle import SessionService
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--session', help='exact MAVEN session UUID; required except for recover-lock')
    parser.add_argument('--env', type=Path, default=default_environment())
    parser.add_argument('action', choices=('intake-pending', 'intake', 'archive-plan', 'override', 'status',
                                          'receipt', 'recover-lock'))
    parser.add_argument('--request', type=Path, help='owner-private JSON object with explicit action fields')
    parser.add_argument('--operation-id', help='original workspace operation UUID for local receipt diagnosis')
    parser.add_argument('--writers-quiescent', action='store_true', help='attest legacy workspace writers have stopped')
    args = parser.parse_args(argv)
    client = None
    try:
        root = args.root.resolve()
        if args.action == 'recover-lock':
            if not args.writers_quiescent:
                raise BoundaryError('legacy workspace lock recovery requires --writers-quiescent')
            with workspace_lock(root, writers_quiescent=True):
                print(json.dumps({'status': 'ready', 'legacy_lock_recovered': True}))
            return 0
        if not args.session:
            raise BoundaryError('an exact --session UUID is required')
        if args.action == 'receipt':
            from tools.coordination import session_uuid
            identifier = session_uuid(args.operation_id) if args.operation_id else None
            directory = private_path(root, '.maven/runtime/workspace')
            matches = []
            for candidate in directory.glob('*.json'):
                journal = read_private(root, candidate.relative_to(root))
                if journal.get('session_id') == args.session and (
                        identifier is None or journal.get('operation_id') == identifier):
                    matches.append(journal)
            if identifier is not None and len(matches) != 1:
                raise BoundaryError('exact workspace operation receipt is unavailable or ambiguous')
            report = {'status': 'ready', 'operations': [
                {key: journal[key] for key in ('operation_id', 'name', 'phase')} for journal in matches]}
            if identifier is not None:
                report['operation'] = matches[0]
            print(json.dumps(report, sort_keys=True))
            return 0
        client = configured_client(args.env)
        service = SessionService(root, args.session, env=args.env, client=client)
        workspace = Workspace(WorkspaceMemory(service))
        if args.action == 'intake-pending':
            report = workspace.pending()
        else:
            if args.request is None:
                raise BoundaryError('an explicit private request is required')
            info = args.request.lstat()
            if not args.request.is_file() or args.request.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BoundaryError('workspace request must be an owner-private regular file')
            body = read_json(args.request)
            if args.action == 'status' and (body.get('check') or body.get('output') is None):
                report = workspace.status(**body)
            else:
                with SessionRegistry(args.env.expanduser().resolve().parent).writer(root, args.session), workspace_lock(root):
                    report = getattr(workspace, args.action.replace('-', '_'))(**body)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get('current', True) else 2
    except (BoundaryError, MemoryClientError) as error:
        print(json.dumps({'status': 'blocked', 'issues': [str(error)]}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'blocked', 'issues': ['workspace I/O or metadata failure; preserve sources and receipts']}))
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
