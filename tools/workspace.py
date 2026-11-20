"""Project-local artifact metadata, private originals and explicit dispositions."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from tools.memory.boundary import BoundaryError, private_path, read_private, save_private
from tools.memory.validation import MemoryError as CredentialError, reject_secrets
from tools.reconcile_records import digest as fingerprint, make_change, record_uuid

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
def workspace_lock(root):
    """Serialize cooperating artifact writers; process exit releases the lock."""
    from tools.coordination import private_lock
    with private_lock(root, '.maven/runtime/workspace.lock', blocking=False):
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


class WorkspaceMemory:
    def __init__(self, store):
        self.store = store
        self.root = store.root
        self.project_id = store.project_id

    def snapshot(self, record_id):
        record_uuid(record_id)
        inventory = self.store.inventory()
        token = inventory['token']
        if any(item['id'] == record_id for item in inventory['items']):
            return token, self.store.read([record_id], token)[0]
        return token, None

    def read(self, record_id):
        _, envelope = self.snapshot(record_id)
        if not envelope or envelope['record'].get('erased'):
            raise BoundaryError('local workspace record unavailable')
        return self.view(envelope)

    @staticmethod
    def view(envelope):
        record = envelope['record']
        return {'record_id': record['id'], 'digest': fingerprint(envelope),
                'kind': record['kind'], 'data': record['data']}

    @staticmethod
    def ref(record):
        return {key: record[key] for key in ('record_id', 'digest')}


class Workspace:
    def __init__(self, memory):
        self.memory = memory
        self.root = memory.root.resolve()


    def _begin(self, name, request, operation_id):
        try:
            # authorization is the owner's disposition explanation, not an HTTP header.
            reject_secrets(list(request.values()))
        except CredentialError as exc:
            raise BoundaryError('workspace request contains possible credential material') from exc
        key = record_uuid(operation_id) if operation_id is not None else fingerprint(
            {'project_id': self.memory.project_id, 'name': name, 'request': request})
        directory = Path('.maven/runtime/workspace') / self.memory.project_id
        path = str(directory / (key + '.json'))
        if operation_id is not None and not private_path(self.root, path).exists():
            matches = []
            for candidate in private_path(self.root, directory).glob('*.json'):
                if read_private(self.root, candidate.relative_to(self.root)).get('operation_id') == operation_id:
                    matches.append(str(candidate.relative_to(self.root)))
            if len(matches) > 1:
                raise BoundaryError('workspace operation UUID has ambiguous local evidence')
            if matches:
                path = matches[0]
        if private_path(self.root, path).exists():
            journal = read_private(self.root, path)
            if (journal.get('project_id') != self.memory.project_id
                    or journal.get('name') != name or journal.get('request') != request):
                raise BoundaryError('workspace operation identity belongs to a different exact request')
        else:
            journal = {'operation_id': operation_id or str(uuid.uuid4()), 'project_id': self.memory.project_id,
                       'name': name, 'request': request, 'phase': 'prepared', 'effects': {}}
            save_private(self.root, path, journal)
        return path, journal

    def _phase(self, path, journal, phase, **fields):
        journal.update(fields, phase=phase)
        save_private(self.root, path, journal)

    def _effect(self, path, journal, step, kind, data, *, record_id, expected, reason):
        token, previous = self.memory.snapshot(record_id)
        actual = fingerprint(previous) if previous else None
        effects = journal['effects']
        if step not in effects:
            if actual != expected:
                raise BoundaryError('workspace record changed before the reviewed effect')
            envelope = make_change(previous, kind, data, reason, record_id=record_id)
            effects[step] = {'before': expected, 'envelope': envelope}
            save_private(self.root, path, journal)
        effect = effects[step]
        target = effect['envelope']
        if actual != fingerprint(target):
            if actual != effect['before']:
                raise BoundaryError('local record changed; preserve files and inspect the receipt')
            token = self.memory.store.apply([target], token)['token']
        if self.memory.store.read([record_id], token)[0] != target:
            raise BoundaryError('workspace effect has no exact local read-back')
        return self.memory.view(target)

    def _store(self, path, journal, step, key, value):
        previous = None if step in journal['effects'] else self.setting(key)
        return self._effect(path, journal, step, 'setting',
            {'key': key, 'value': value, 'scope': 'project'}, record_id=self.setting_id(key),
            expected=previous['digest'] if previous else None,
            reason='Record explicitly governed workspace metadata')

    def _operation(self, path, journal, state, metadata):
        summary = json.dumps(metadata, sort_keys=True)
        if len(summary) > 16000:
            raise BoundaryError('workspace operation metadata exceeds bounded receipt size')
        if state == 'started' and 'completed' in journal['effects']:
            return
        started = journal['effects'].get('started')
        return self._effect(path, journal, state, 'action',
            {'name': journal['name'], 'state': state, 'summary': summary},
            record_id=journal['operation_id'],
            expected=fingerprint(started['envelope']) if state == 'completed' else None,
            reason='Record governed local source disposition')

    def _finish(self, path, journal, metadata, result):
        self._operation(path, journal, 'completed', metadata)
        result = dict(result, operation_id=journal['operation_id'])
        self._phase(path, journal, 'completed', result=result)
        return result
    def setting_id(self, key):
        return str(uuid.uuid5(uuid.UUID(self.memory.project_id), 'workspace:' + key))

    def setting(self, key):
        _, envelope = self.memory.snapshot(self.setting_id(key))
        if envelope is None:
            return None
        if envelope['record'].get('erased'):
            raise BoundaryError('workspace metadata was erased')
        record = self.memory.view(envelope)
        if record['kind'] != 'setting' or record['data']['key'] != key:
            raise BoundaryError('workspace setting identity conflicts with existing DB metadata')
        return record



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
        if (type(confirm) is not bool or type(reviewed) is not bool or
                disposition != 'retain' and not confirm or disposition == 'delete' and not reviewed):
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
        if 'disposed' not in journal['effects']:
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

    def archive_plan(self, *, record_id, digest, confirm, completed, source_path=None,
                     source_disposition='retain', operation_id=None):
        if confirm is not True or completed is not True:
            raise BoundaryError('named plan archival requires actual completion and owner approval')
        if source_disposition not in ('retain', 'archive'):
            raise BoundaryError('optional plan original requires an explicit retain or archive disposition')
        if source_path is None and source_disposition != 'retain':
            raise BoundaryError('source disposition requires an explicitly selected local original')
        request = dict(record_id=record_id, digest=digest, confirm=confirm, completed=completed,
                       source_path=source_path, source_disposition=source_disposition)
        path, journal = self._begin('archive-plan', request, operation_id)
        if journal.get('result'):
            return journal['result']
        if 'metadata' not in journal:
            plan = self.memory.read(record_id)
            if plan['digest'] != digest or plan['kind'] != 'plan' or plan['data'].get('state') == 'archived':
                raise BoundaryError('archive requires the exact current local plan')
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
        updated = self._effect(path, journal, 'archive', 'plan', dict(plan['data'], state='archived'),
            record_id=record_id, expected=digest, reason='Archive completed plan; retain history')
        key = 'archive-' + record_id
        if 'archived' not in journal['effects']:
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

    def status(self, *, record_id, digest, output=None, check=False, operation_id=None):
        if type(check) is not bool:
            raise BoundaryError('status check must be a boolean')
        journal = None
        if output is not None and not check:
            if output != 'team-status.md':
                raise BoundaryError('external status output must use the governed team-status.md path')
            path, journal = self._begin('status', dict(record_id=record_id, digest=digest, output=output), operation_id)
        record = self.memory.read(record_id)
        value = record['data'].get('value', {})
        if (record['digest'] != digest or record['kind'] != 'setting' or record['data'].get('key') != 'project-status'
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
        prior = previous['data']['value'] if previous else {}
        owned = False
        if before is not None and prior.get('output_sha256') == hashlib.sha256(before).hexdigest():
            saved = self.receipt(operation_id=prior.get('operation_id'))
            owned = (saved.get('name') == 'status' and saved.get('phase') == 'completed'
                     and saved['result'].get('receipt') == self.memory.ref(previous))
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
        metadata = {'source': self.memory.ref(record), 'output_path': output, 'output_sha256': output_hash,
                    'operation_id': journal['operation_id']}
        if 'before_sha256' not in journal:
            self._phase(path, journal, 'prepared',
                        before_sha256=hashlib.sha256(before).hexdigest() if before is not None else None)
        self._operation(path, journal, 'started', metadata)
        if not installed:
            if self.memory.read(record_id)['digest'] != digest:
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


    def receipt(self, *, operation_id):
        record_uuid(operation_id)
        directory = Path('.maven/runtime/workspace') / self.memory.project_id
        for candidate in private_path(self.root, directory).glob('*.json'):
            journal = read_private(self.root, candidate.relative_to(self.root))
            if journal['operation_id'] == operation_id and journal['project_id'] == self.memory.project_id:
                return {key: journal[key] for key in ('operation_id', 'name', 'phase', 'result') if key in journal}
        raise BoundaryError('exact workspace receipt unavailable')
