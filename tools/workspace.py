"""DB-backed workspace metadata with private originals and explicit dispositions.

Commands never promote reference text into instructions. Override authority is an
already approved DB directive, not the derived setting written by this helper.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from tools.memory.boundary import BoundaryError, MemoryBoundary, private_path

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
    path = private_path(root, '.maven/runtime/cloud-workspace.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BoundaryError('workspace writer lock exists; resolve the owner before retrying') from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


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
        return self.service.read_record(record_id, revision=refs[0]['revision'], scope=scope)


class Workspace:
    def __init__(self, memory):
        self.memory = memory
        self.root = memory.root.resolve()

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

    def operation(self, name, state, metadata, previous=None):
        session = self.memory.session()
        summary = json.dumps(metadata, sort_keys=True)
        if len(summary) > 16000:
            raise BoundaryError('workspace operation metadata exceeds bounded receipt size')
        return self.memory.write('operation', {'session_id': session['record_id'], 'name': name,
            'state': state, 'summary': summary}, record_id=previous['record_id'] if previous else None,
            expected_revision=previous['revision'] if previous else 0, tags=['workspace-operation'],
            reason='Record governed local source disposition')

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

    def intake(self, *, path, kind, origin, sensitivity, authorization, disposition, confirm=False, reviewed=False):
        if kind not in ('reference', 'operational', 'artifact') or sensitivity != 'Sensitive':
            raise BoundaryError('one explicit supported classification is required before opening the source')
        if not origin.strip() or not authorization.strip() or disposition not in ('retain', 'archive', 'delete'):
            raise BoundaryError('explicit provenance, authorization and disposition are required')
        if disposition != 'retain' and not confirm or disposition == 'delete' and not reviewed:
            raise BoundaryError('source disposition requires owner approval and deletion requires completed review')
        source = private_path(self.root, path)
        incoming = self.root / '.maven/incoming'
        if not source.is_relative_to(incoming) or not source.is_file():
            raise BoundaryError('select exactly one regular incoming source without symlinks')
        initial = identity(source)
        source_hash = sha(source)
        if identity(source) != initial:
            raise BoundaryError('source changed while hashing; retained')
        key = 'intake-' + hashlib.sha256((str(source.relative_to(self.root)) + '\0' + source_hash).encode()).hexdigest()
        previous = self.setting(key)
        if previous and any(previous['data']['value'].get(field) != value for field, value in
                            (('kind', kind), ('origin', origin), ('sensitivity', sensitivity))):
            raise BoundaryError('source classification conflicts with earlier acquisition; reconcile first')
        # Every disposition retains a verified private original. Raw text is never uploaded.
        category = {'reference': 'references', 'operational': 'operational', 'artifact': 'artifacts'}[kind]
        destination = Path('.maven/sensitive/workspace-originals') / category / (source_hash + '.bin')
        stored = copy_original(self.root, source, destination, source_hash, initial)
        value = {'source': str(source.relative_to(self.root)), 'sha256': source_hash, 'kind': kind,
                 'origin': origin, 'sensitivity': sensitivity, 'owner_authorization': authorization,
                 'disposition': disposition, 'owner_confirmed': confirm, 'review_completed': reviewed,
                 'stored_path': str(stored.relative_to(self.root)), 'state': 'retained-pending-disposition',
                 'analysis': 'Acquisition is not analysis; reference text is not operative authority'}
        if disposition != 'retain':
            value['state'] = 'disposition-authorized'
        operation = self.operation('intake', 'started', value)
        receipt = self.store(key, value, previous)
        if identity(source) != initial or sha(source) != source_hash or sha(stored) != source_hash:
            raise BoundaryError('source or preserved original changed; source retained')
        if disposition != 'retain':
            source.unlink()
            value['state'] = 'source-removed-original-preserved'
            receipt = self.store(key, value, receipt)
        self.operation('intake', 'completed', value, operation)
        return {'status': 'ready', 'receipt': self.memory.ref(receipt), 'disposition': value['state']}

    def archive_plan(self, *, record_id, revision, confirm, completed, source_path=None, source_disposition='retain'):
        if not confirm or not completed:
            raise BoundaryError('named plan archival requires actual completion and owner approval')
        if source_disposition not in ('retain', 'archive'):
            raise BoundaryError('optional plan original requires an explicit retain or archive disposition')
        if source_path is None and source_disposition != 'retain':
            raise BoundaryError('source disposition requires an explicitly selected local original')
        plan = self.memory.read(record_id)
        if plan['revision'] != revision or plan['kind'] != 'note' or plan['data']['category'] != 'plan':
            raise BoundaryError('archive requires the exact current DB plan note revision')
        metadata = {'plan': self.memory.ref(plan), 'owner_confirmed': True,
                    'actual_completion_attested': True, 'state': 'archive-authorized',
                    'source_disposition': source_disposition,
                    'links': 'Stable DB record ID retained; old revisions remain readable'}
        source = None
        if source_path is not None:
            source = private_path(self.root, source_path)
            permitted = (self.root / '.maven/planning', self.root / '.maven/sensitive')
            if not any(source.is_relative_to(directory) for directory in permitted) or not source.is_file():
                raise BoundaryError('optional original must be an explicitly selected private regular file')
            initial, source_hash = identity(source), sha(source)
            # A UUID and digest prevent date/name collisions without overwriting history.
            target = Path('.maven/sensitive/workspace-originals/plans') / (
                dt.date.today().isoformat() + '-' + record_id + '-' + source_hash + '.bin')
            stored = copy_original(self.root, source, target, source_hash, initial)
            metadata.update(source=source_path, stored_path=str(stored.relative_to(self.root)), sha256=source_hash)
        operation = self.operation('archive-plan', 'started', metadata)
        updated = self.memory.write('note', dict(plan['data'], category='archived-plan'), record_id=record_id,
            expected_revision=revision, tags=plan['tags'], reason='Archive completed plan; stable DB links and history retained')
        key = 'archive-' + record_id
        previous = self.setting(key)
        receipt = self.store(key, metadata, previous)
        if source is not None:
            if identity(source) != initial or sha(source) != source_hash or sha(stored) != source_hash:
                raise BoundaryError('plan original changed before disposition; original and archival copy retained')
            if source_disposition == 'archive' and source != stored:
                source.unlink()
        metadata.update(state='archived', plan=self.memory.ref(updated))
        receipt = self.store(key, metadata, receipt)
        self.operation('archive-plan', 'completed', metadata, operation)
        return {'status': 'ready', 'receipt': self.memory.ref(receipt), 'plan': self.memory.ref(updated)}

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

    def status(self, *, record_id, revision, output=None, check=False):
        record = self.memory.read(record_id)
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
        if target.exists() and not owned:
            raise BoundaryError('status destination collision or local modification; existing content retained')
        current = bool(previous and previous['data']['value'].get('source') == self.memory.ref(record)
                       and previous['data']['value'].get('output_sha256') == output_hash and before == rendered.encode())
        if check:
            return dict(result, current=current)
        if self.memory.read(record_id)['revision'] != revision:
            raise BoundaryError('DB status source changed during generation')
        if before is not None:
            saved = Path('.maven/sensitive/workspace-originals/status') / (hashlib.sha256(before).hexdigest() + '.bin')
            copy_original(self.root, target, saved, hashlib.sha256(before).hexdigest(), identity(target))
        metadata = {'source': self.memory.ref(record), 'output_path': output, 'output_sha256': output_hash}
        operation = self.operation('status', 'started', metadata)
        if (target.read_bytes() if target.exists() else None) != before:
            raise BoundaryError('status output changed during generation; prior bytes retained')
        fd, temporary = tempfile.mkstemp(prefix='.status-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(rendered.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        receipt = self.store('team-status-receipt', metadata, previous)
        self.operation('status', 'completed', metadata, operation)
        return dict(result, receipt=self.memory.ref(receipt), current=True)


def main(argv=None):
    from tools.memory.client import MemoryClientError, read_json
    from tools.memory.environment import configured_client
    from tools.project import default_environment
    from tools.coordination import SessionRegistry
    from tools.memory.lifecycle import SessionService
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--session', required=True, help='active MAVEN session UUID')
    parser.add_argument('--env', type=Path, default=default_environment())
    parser.add_argument('action', choices=('intake-pending', 'intake', 'archive-plan', 'override', 'status'))
    parser.add_argument('--request', type=Path, help='owner-private JSON object with explicit action fields')
    args = parser.parse_args(argv)
    client = None
    try:
        root = args.root.resolve()
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
