"""Verified DB session/checkpoint receipts; local files are pointers, never authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


class BoundaryError(Exception):
    """Safe, content-free failure at a memory work boundary."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.is_relative_to(root) or '..' in path.parts:
        raise BoundaryError('private path escapes workspace')
    cursor = root
    for part in path.relative_to(root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise BoundaryError('private paths must not be symlinks')
    return path


def save_private(root, relative, value):
    path = private_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix='.memory-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_private(root, relative):
    path = private_path(root, relative)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BoundaryError('runtime pointer must be owner-private')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise BoundaryError('required private runtime pointer is unavailable') from None


class MemoryBoundary:
    """An explicitly bound cloud session; local receipts only select verified records."""

    def __init__(self, client, root, project_id, session_id, *, harness=None, harness_session=None):
        from tools.coordination import session_uuid
        self.client = client
        self.root = Path(root).resolve()
        self.project_id = session_uuid(project_id)
        self.session_id = session_uuid(session_id)
        self.harness, self.harness_session = harness, harness_session
        self.runtime = f'.maven/runtime/sessions/{self.session_id}'
        self.SESSION_PATH = self.runtime + '/session.json'
        self.pending_path = self.runtime + '/pending-memory.json'

    def invoke(self, route, body):
        result = self.client.invoke('/v1/memory/' + route, body)
        if result.get('status') != 'ready' or not isinstance(result.get('data'), dict):
            raise BoundaryError('memory service returned no verified result')
        return result['data']

    def read(self, record_id, revision=None):
        body = {'record_id': record_id}
        if revision is not None:
            body['revision'] = revision
        record = self.invoke('read', body)
        if (record.get('record_id') != record_id or record.get('project_id') != self.project_id
                or record.get('data_scope') != 'owner' or record.get('classification') != 'Sensitive'
                or revision is not None and record.get('revision') != revision):
            raise BoundaryError('memory record scope or revision mismatch')
        return record

    def _complete_request(self, route, body, result):
        record = self.read(body['record_id'])
        if (result.get('record_id') != body['record_id'] or result.get('request_key') != body['request_key']
                or result.get('revision') != body['expected_revision'] + 1
                or record.get('revision') != result['revision']):
            raise BoundaryError('memory mutation receipt does not match read-back')
        if route == 'write':
            if (record.get('kind') != body['kind'] or record.get('data') != body['data']
                    or record.get('tags') != body.get('tags', [])):
                raise BoundaryError('memory write content does not match read-back')
        else:
            claim = record.get('claim')
            if record.get('kind') != 'task' or record['data']['state'] != result.get('state'):
                raise BoundaryError('task claim state does not match read-back')
            if body['action'] == 'claim':
                if (not isinstance(claim, dict) or claim.get('session_id') != self.session_id
                        or any(claim.get(key) != result.get(key) for key in ('session_id', 'fence', 'expires_at'))):
                    raise BoundaryError('task claim fence does not match read-back')
            elif claim is not None or result.get('session_id') is not None or result.get('fence') != body['fence']:
                raise BoundaryError('task claim release does not match read-back')
        save_private(self.root, self.runtime + '/last-memory-write.json', {
            'route': route, 'request_key': body['request_key'], 'record': self.ref(record), 'result': result})
        private_path(self.root, self.pending_path).unlink(missing_ok=True)
        return record

    def _validate_request(self, route, body):
        from .validation import MemoryError, validate
        if route not in ('write', 'claim'):
            raise BoundaryError('only typed memory mutations can use the session journal')
        try:
            validate('/v1/memory/' + route, body)
        except MemoryError as error:
            raise BoundaryError('invalid typed memory mutation: ' + error.status) from None
        if route == 'write' and (body.get('project_id') != self.project_id
                                or body.get('classification') != 'Sensitive' or body.get('data_scope') != 'owner'):
            raise BoundaryError('memory mutation does not match selected project scope')
        if route == 'claim' and body.get('session_id') != self.session_id:
            raise BoundaryError('task claim belongs to another session')

    def _submit(self, route, body):
        self._validate_request(route, body)
        if private_path(self.root, self.pending_path).exists():
            raise BoundaryError('an unresolved cloud request exists; run explicit recover before further writes')
        request = {'route': route, 'body': body, 'session_id': self.session_id}
        request['digest'] = digest(request)
        save_private(self.root, self.pending_path, request)
        result = self.invoke(route, body)
        return result, self._complete_request(route, body, result)

    def submit(self, route, body):
        """Journal an exact caller-owned request key. Caller holds the session lock/writer guard."""
        if route.startswith('/v1/memory/'):
            route = route[len('/v1/memory/'):]
        result, _ = self._submit(route, body)
        return {'status': 'ready', 'data': result}

    def write(self, kind, data, *, record_id=None, expected_revision=0, tags=(),
              reason='Record managed work', source=None):
        record_id = record_id or str(uuid.uuid4())
        body = {'record_id': record_id, 'expected_revision': expected_revision, 'kind': kind,
                'classification': 'Sensitive', 'data_scope': 'owner', 'project_id': self.project_id,
                'data': data, 'tags': list(tags), 'source': source or {
                    'origin': 'managed-work-boundary', 'excerpt': 'Explicit local work-boundary operation'},
                'change_reason': reason}
        body['request_key'] = str(uuid.uuid5(uuid.UUID(record_id), digest(body)))
        _, record = self._submit('write', body)
        return record

    def recover(self):
        """Reconcile the exact persisted request key; never generate a replacement."""
        from .client import MemoryClientError
        if not private_path(self.root, self.pending_path).exists():
            self.restore_pointer()
            return None
        request = read_private(self.root, self.pending_path)
        fingerprint = request.pop('digest', None)
        if request.get('session_id') != self.session_id or fingerprint != digest(request):
            raise BoundaryError('pending request binding is invalid')
        route, body = request['route'], request['body']
        self._validate_request(route, body)
        key = body['request_key']
        try:
            result = self.client.invoke('/v1/operations/lookup', {'request_key': key})
            if result.get('status') != 'ready':
                raise BoundaryError('cloud recovery lookup returned no verified result')
            result = result['data']
        except MemoryClientError as error:
            if error.code != 'operation_unavailable':
                raise
            try:
                result = self.invoke(route, body)
            except MemoryClientError as retry:
                if retry.code in ('revision_conflict', 'invalid_request', 'not_authorized'):
                    save_private(self.root, self.runtime + '/failed-memory-write.json',
                                 {'request': request, 'status': retry.code})
                    private_path(self.root, self.pending_path).unlink()
                raise
        record = self._complete_request(route, body, result)
        if record['record_id'] == self.session_id:
            save_private(self.root, self.SESSION_PATH, self.ref(record))
        return {'route': route, 'record': self.ref(record), 'result': result}

    def restore_pointer(self):
        """Recover the known session identity, including a crash before its pointer rename."""
        from .client import MemoryClientError
        if private_path(self.root, self.SESSION_PATH).exists():
            return self.session(closed=True)
        try:
            record = self.read(self.session_id)
        except MemoryClientError as error:
            if error.code == 'record_unavailable':
                return None
            raise
        if record['kind'] != 'session' or self.metadata(record).get('root') != str(self.root):
            raise BoundaryError('cloud session identity does not match this root')
        save_private(self.root, self.SESSION_PATH, self.ref(record))
        return record

    @staticmethod
    def ref(record):
        return {'record_id': record['record_id'], 'revision': record['revision']}

    def context(self):
        response = self.client.load_context({'project_id': self.project_id, 'workflow': 'maven-workflow'})
        data = response.get('data', {})
        instructions = data.get('instructions')
        if (response.get('status') != 'ready' or not isinstance(instructions, list) or not instructions
                or not any(page.get('page_kind') == 'directive' for page in instructions)
                or type(data.get('authority_generation')) is not int):
            raise BoundaryError('required approved DB authority is missing')
        return data

    @staticmethod
    def authority(context):
        return {'generation': context['authority_generation'], 'pages': [
            {key: page[key] for key in ('slug', 'revision', 'content_hash')}
            for page in context['instructions']]}

    @staticmethod
    def metadata(record):
        try:
            value = json.loads(record['data']['summary'])
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise BoundaryError('DB boundary metadata is missing or malformed') from None

    def session(self, *, closed=False):
        pointer = read_private(self.root, self.SESSION_PATH)
        if pointer.get('record_id') != self.session_id:
            raise BoundaryError('session pointer does not match the explicitly selected session')
        record = self.read(self.session_id)
        meta = self.metadata(record)
        if (record.get('kind') != 'session' or meta.get('root') != str(self.root)
                or record['data']['state'] not in (('active', 'closed', 'abandoned') if closed else ('active',))):
            raise BoundaryError('an exact bound active cloud session is required')
        return record

    def search(self, kind, state=None):
        after = None
        while True:
            body = {'kind': kind, 'project_id': self.project_id, 'limit': 20}
            if state is not None:
                body['state'] = state
            if after:
                body['after_id'] = after
            page = self.invoke('search', body)
            for item in page['records']:
                record = self.read(item['record_id'])
                if record['kind'] == kind and (state is None or record['data']['state'] == state):
                    yield record
            after = page['next_after_id']
            if after is None:
                return

    def project_context(self):
        completed = []
        peers = []
        for record in self.search('session'):
            if record['record_id'] == self.session_id:
                continue
            if record['data']['state'] == 'closed' and record['data'].get('workflow') == 'maven-workflow':
                completed.append(record)
            elif record['data']['state'] == 'active':
                peers.append(record)
        completed.sort(key=lambda record: (
            self.metadata(record).get('completed_at') or record.get('recorded_at', ''), record['record_id']), reverse=True)
        return {'previous_handover': completed[0] if completed else None,
                'tasks': [record for record in self.search('task')
                          if record['data']['state'] in ('open', 'in_progress', 'blocked')],
                'blockers': list(self.search('risk', 'open')), 'active_peers': peers}

    def start(self, git_base):
        context = self.context()
        if private_path(self.root, self.SESSION_PATH).exists():
            raise BoundaryError('session already has a cloud pointer; explicitly resume it')
        from .skills import SessionSkills
        skills = SessionSkills(self.client, self.root, self.project_id, self.session_id)
        prepared = skills.start()
        data = {'state': 'active', 'workflow': 'maven-workflow', 'summary': json.dumps({
            'root': str(self.root), 'harness': self.harness, 'harness_session': self.harness_session,
            'git_base': git_base, 'authority': self.authority(context), 'phase': 'started'}, sort_keys=True)}
        record = self.write('session', data, record_id=self.session_id, tags=['work-session'])
        save_private(self.root, self.SESSION_PATH, self.ref(record))
        return {'session': self.ref(record), 'context': context, 'skills': prepared['data'],
                **self.project_context()}

    def refresh(self):
        return {'session': self.ref(self.session()), 'context': self.context(), **self.project_context()}

    def reconcile(self, git_base):
        """A peer may advance shared HEAD; retain work memory but invalidate its candidate summary."""
        record = self.session()
        meta = self.metadata(record)
        if meta.get('git_base') == git_base and meta.get('phase') not in ('summarized', 'checkpointed'):
            return self.ref(record)
        meta.update(git_base=git_base, phase='active', writers_quiescent=False)
        updated = self.write('session', dict(record['data'], summary=json.dumps(meta, sort_keys=True)),
                             record_id=self.session_id, expected_revision=record['revision'], tags=record['tags'],
                             reason='Reconcile shared checkout without discarding session memory')
        return self.ref(updated)

    def summarize(self, *, summary, refs, git_base, candidate_digest, reservation_id):
        if not isinstance(summary, str) or not summary.strip():
            raise BoundaryError('a substantive work summary is required')
        record = self.session()
        context = self.context()
        if len(refs) > 99:
            raise BoundaryError('too many checkpoint memory references')
        for ref in refs:
            current = self.read(ref['record_id'])
            if current['revision'] != ref['revision'] or current['kind'] in ('session', 'checkpoint'):
                raise BoundaryError('summary memory references must be current work records')
        meta = self.metadata(record)
        meta.update(git_base=git_base, candidate_digest=candidate_digest, authority=self.authority(context),
                    phase='summarized', summary=summary, memory_refs=refs, writers_quiescent=True,
                    reservation_id=reservation_id)
        text = json.dumps(meta, sort_keys=True)
        if len(text) > 16000:
            raise BoundaryError('session summary exceeds the bounded record limit')
        updated = self.write('session', dict(record['data'], summary=text), record_id=self.session_id,
                             expected_revision=record['revision'], tags=record['tags'],
                             reason='Summarize this session after relevant writer acknowledgments')
        return self.ref(updated)

    def freshness_issues(self, root, paths, *, git_enabled=True, reservation_id=None):
        from tools.workflow import candidate_digest, git
        try:
            record = self.session()
            meta = self.metadata(record)
            base = git(root, 'rev-parse', 'HEAD').stdout.strip() if git_enabled else None
            candidate = candidate_digest(root, paths) if git_enabled else digest({})
            if (meta.get('phase') != 'summarized' or meta.get('writers_quiescent') is not True
                    or meta.get('git_base') != base or meta.get('candidate_digest') != candidate
                    or meta.get('reservation_id') != reservation_id
                    or meta.get('authority') != self.authority(self.context())):
                return ['DB session summary is stale for this candidate, reservation or authority']
            for ref in meta.get('memory_refs', []):
                if self.read(ref['record_id'])['revision'] != ref['revision']:
                    return ['DB work reference changed after the session summary']
            return []
        except BoundaryError as error:
            return [str(error)]

    def prepare_checkpoint(self, root, paths, base, reservation_id):
        issues = self.freshness_issues(root, paths, reservation_id=reservation_id)
        if issues:
            raise BoundaryError(issues[0])
        from tools.workflow import candidate_digest
        session = self.session()
        meta = self.metadata(session)
        record = self.write('checkpoint', {
            'session_id': self.session_id, 'candidate_digest': candidate_digest(root, paths), 'git_base': base,
            'memory_refs': [self.ref(session), *meta['memory_refs']], 'state': 'prepared',
            'summary': json.dumps({'authority': meta['authority'], 'remote_sha': None,
                                   'reservation_id': reservation_id}, sort_keys=True)},
            record_id=str(uuid.uuid5(uuid.UUID(reservation_id), 'checkpoint')),
            tags=['work-checkpoint'], reason='Bind exact candidate to verified session memory')
        return self.ref(record)

    def verify_checkpoint(self, proposal, *, resume=False):
        if proposal.get('session_id') != self.session_id or proposal.get('project_id') != self.project_id:
            raise BoundaryError('checkpoint approval belongs to another project or session')
        ref = proposal['memory']
        original = self.read(ref['record_id'], ref['revision'])
        current = self.read(ref['record_id'])
        data = original['data']
        if (original['kind'] != 'checkpoint' or data['state'] != 'prepared'
                or data['session_id'] != self.session_id
                or data['candidate_digest'] != proposal['candidate_digest'] or data['git_base'] != proposal['base_head']
                or any(current['data'].get(key) != data[key] for key in
                       ('session_id', 'candidate_digest', 'git_base', 'memory_refs'))
                or self.metadata(current).get('authority') != self.metadata(original).get('authority')
                or self.metadata(original).get('reservation_id') != proposal['reservation_id']
                or current['data']['state'] not in (('prepared', 'committed') if resume else ('prepared',))
                or not resume and current['revision'] != ref['revision']
                or self.metadata(original)['authority'] != self.authority(self.context())):
            raise BoundaryError('DB checkpoint receipt is stale or does not match the exact proposal')
        for memory_ref in data['memory_refs']:
            actual = self.read(memory_ref['record_id'])
            if resume and actual['record_id'] == self.session_id and self.metadata(actual).get('last_checkpoint') == ref:
                actual = self.read(memory_ref['record_id'], memory_ref['revision'])
            if actual['revision'] != memory_ref['revision']:
                raise BoundaryError('required DB memory receipt changed after approval')
        return current

    def link_checkpoint(self, proposal, commit, remote):
        record = self.verify_checkpoint(proposal, resume=True)
        if record['data'].get('final_commit') not in (None, commit):
            raise BoundaryError('checkpoint is already linked to another commit')
        meta = self.metadata(record)
        if record['data']['state'] == 'committed':
            if meta.get('remote_sha') != commit:
                raise BoundaryError('committed checkpoint lacks verified remote linkage')
            return self.ref(record)
        local_id = str(uuid.uuid5(uuid.UUID(record['record_id']), 'verified-local-git-link'))
        local_data = {'session_id': self.session_id, 'name': 'verified-local-git-link', 'state': 'completed',
                      'summary': json.dumps({'checkpoint': proposal['memory'], 'final_commit': commit}, sort_keys=True)}
        from .client import MemoryClientError
        try:
            local = self.read(local_id)
        except MemoryClientError as error:
            if error.code != 'record_unavailable':
                raise
            local = self.write('operation', local_data, record_id=local_id, tags=['work-checkpoint-local-link'],
                               reason='Preserve verified local Git link before publishing')
        if local['kind'] != 'operation' or local['data'] != local_data:
            raise BoundaryError('local Git linkage conflicts with approved checkpoint')
        if remote is None:
            return self.ref(local)
        if remote != commit:
            raise BoundaryError('remote SHA must equal the verified local commit')
        meta.update(remote_sha=remote, local_link=self.ref(local))
        data = dict(record['data'], state='committed', final_commit=commit, summary=json.dumps(meta, sort_keys=True))
        return self.ref(self.write('checkpoint', data, record_id=record['record_id'],
                       expected_revision=record['revision'], tags=[*record['tags'], 'git-' + commit],
                       reason='Commit independently verified local and remote Git linkage'))

    def memory_checkpoint(self, reservation_id):
        """Record memory-only durability without fabricating a Git checkpoint or SHA."""
        from .client import MemoryClientError
        identifier = str(uuid.uuid5(uuid.UUID(reservation_id), 'memory-only-checkpoint'))
        session = self.session(closed=True)
        meta = self.metadata(session)
        data = {'session_id': self.session_id, 'name': 'memory-only-checkpoint', 'state': 'completed',
                'summary': json.dumps({'session': self.ref(session), 'memory_refs': meta['memory_refs'],
                                      'authority': meta['authority'], 'reservation_id': reservation_id,
                                      'git_outcome': 'not_applicable_or_unchanged'}, sort_keys=True)}
        try:
            record = self.read(identifier)
        except MemoryClientError as error:
            if error.code != 'record_unavailable':
                raise
            record = self.write('operation', data, record_id=identifier, tags=['memory-checkpoint'],
                                reason='Preserve verified work memory without Git publication')
        if (record['kind'] != 'operation' or record['data']['session_id'] != self.session_id
                or self.metadata(record).get('reservation_id') != reservation_id
                or self.metadata(record).get('memory_refs') != meta['memory_refs']
                or self.metadata(record).get('authority') != meta['authority']):
            raise BoundaryError('memory checkpoint conflicts with this session summary')
        return self.ref(record)

    def abort_checkpoint(self, proposal):
        record = self.read(proposal['memory']['record_id'])
        if record['data']['session_id'] != self.session_id:
            raise BoundaryError('only this session checkpoint can be cancelled')
        if record['data']['state'] == 'aborted':
            return self.ref(record)
        if record['data']['state'] != 'prepared':
            raise BoundaryError('only a prepared checkpoint can be cancelled')
        return self.ref(self.write('checkpoint', dict(record['data'], state='aborted'),
                        record_id=record['record_id'], expected_revision=record['revision'], tags=record['tags'],
                        reason='Explicitly cancel uncommitted approval'))

    def complete(self, outcome, *, close, checkpoint=None):
        import datetime
        record = self.session(closed=True)
        meta = self.metadata(record)
        if meta.get('boundary_outcome') == outcome and meta.get('last_checkpoint') == checkpoint and (
                record['data']['state'] == 'closed' if close else meta.get('phase') == 'checkpointed'):
            return self.ref(record)
        if record['data']['state'] != 'active':
            raise BoundaryError('only an active session can complete work')
        meta.update(boundary_outcome=outcome, last_checkpoint=checkpoint,
                    phase='closed' if close else 'checkpointed', writers_quiescent=False)
        if close:
            meta['completed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self.ref(self.write('session', dict(record['data'], state='closed' if close else 'active',
                         summary=json.dumps(meta, sort_keys=True)), record_id=self.session_id,
                         expected_revision=record['revision'], tags=record['tags'],
                         reason='Close only requester session' if close else 'Preserve active session checkpoint'))

    def cancel(self):
        record = self.session(closed=True)
        if record['data']['state'] == 'abandoned':
            return self.ref(record)
        if record['data']['state'] != 'active':
            raise BoundaryError('only an active session may be cancelled')
        meta = self.metadata(record)
        meta.update(phase='cancelled', boundary_outcome='explicitly-cancelled')
        return self.ref(self.write('session', dict(record['data'], state='abandoned', summary=json.dumps(meta)),
                        record_id=self.session_id, expected_revision=record['revision'], tags=record['tags'],
                        reason='Explicitly cancel requester session without claiming completed work'))
