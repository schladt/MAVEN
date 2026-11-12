"""Verified DB session/checkpoint receipts; local files are pointers, never authority."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
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
        # Generation of the authority governing this boundary's writes; the service refuses a
        # write made under any other generation (context_stale) and commits nothing.
        self.authority_generation = None
        self.runtime = f'.maven/runtime/sessions/{self.session_id}'
        self.SESSION_PATH = self.runtime + '/session.json'
        self.pending_path = self.runtime + '/pending-memory.json'

    def invoke(self, route, body):
        """Reject malformed requests with field-level detail before any network call."""
        from .client import MemoryClientError
        from .validation import MemoryError, validate
        try:
            validate('/v1/memory/' + route, body)
        except MemoryError as error:
            raise MemoryClientError(error.status, detail=error.metadata.get('detail')) from None
        result = self.client.invoke('/v1/memory/' + route, body)
        if result.get('status') != 'ready' or not isinstance(result.get('data'), dict):
            raise BoundaryError('memory service returned no verified result')
        return result['data']

    def read(self, record_id, revision=None, *, scope='project', project_id=None):
        body = {'record_id': record_id}
        if revision is not None:
            body['revision'] = revision
        record = self.invoke('read', body)
        selected = self.project_id if project_id is None else project_id
        if scope not in ('project', 'universal', 'context', 'all'):
            raise BoundaryError('invalid memory scope')
        permitted = (scope == 'all' or scope == 'universal' and record.get('project_id') is None
                     or scope == 'project' and record.get('project_id') == selected
                     or scope == 'context' and record.get('project_id') in (None, selected))
        if (record.get('record_id') != record_id or not permitted
                or record.get('data_scope') != 'owner' or record.get('classification') != 'Sensitive'
                or revision is not None and record.get('revision') != revision):
            raise BoundaryError('memory record scope or revision mismatch')
        return record

    def _complete_request(self, route, body, result):
        """Verify a reconciled receipt by exact read-back: a lookup proves the key, not this body."""
        record = self.read(body['record_id'], body['expected_revision'] + 1,
                           scope='universal' if route == 'write' and body.get('project_id') is None else 'context')
        if (result.get('record_id') != body['record_id'] or result.get('request_key') != body['request_key']
                or result.get('revision') != body['expected_revision'] + 1
                or record.get('revision') != result['revision']):
            raise BoundaryError('memory mutation receipt does not match read-back')
        if route == 'write':
            if (record.get('kind') != body['kind'] or record.get('data') != body['data']
                    or record.get('project_id') != body.get('project_id')
                    or record.get('tags') != body.get('tags', [])):
                raise BoundaryError('memory write content does not match read-back')
        else:
            if record.get('kind') != 'task' or record['data']['state'] != result.get('state'):
                raise BoundaryError('task claim state does not match exact read-back')
            self._claim_receipt(body, result)
            # Historical versions deliberately omit mutable claims. The exact
            # operation receipt proves the old effect, not ownership of a live lease.
            current = self.read(body['record_id'], scope='context')
            if current['revision'] == result['revision']:
                claim = current.get('claim')
                if body['action'] == 'claim':
                    if claim is not None and (not isinstance(claim, dict) or any(
                            claim.get(key) != result.get(key) for key in ('session_id', 'fence', 'expires_at'))):
                        raise BoundaryError('current task claim does not match the verified receipt')
                elif claim is not None:
                    raise BoundaryError('current task claim release does not match read-back')
                record = current
            record = dict(record, claim_receipt=result)
        return self._finish_request(route, body, record, result)

    def _claim_receipt(self, body, result):
        if body['action'] == 'claim':
            if (result.get('session_id') != self.session_id or type(result.get('fence')) is not int
                    or result['fence'] < 1 or not isinstance(result.get('expires_at'), str)):
                raise BoundaryError('task claim receipt has no verified fence')
        elif result.get('session_id') is not None or result.get('fence') != body['fence']:
            raise BoundaryError('task claim release receipt does not match request')

    def _complete_receipt(self, route, body, result, target):
        """Bind a direct receipt to its exact request without a read-back.

        The service commits exactly this body under its request key and CAS revision, and
        replays only an identical canonical request; the record is the request applied.
        """
        if (result.get('record_id') != body['record_id'] or result.get('request_key') != body['request_key']
                or type(result.get('revision')) is not int or result['revision'] != body['expected_revision'] + 1):
            raise BoundaryError('memory mutation receipt does not match the request')
        identity = {'record_id': body['record_id'], 'revision': result['revision']}
        if route == 'write':
            record = {**identity, 'kind': body['kind'], 'classification': body['classification'],
                      'data_scope': body['data_scope'], 'project_id': body.get('project_id'),
                      'data': deepcopy(body['data']), 'tags': list(body.get('tags', []))}
        else:
            state = {'claim': 'in_progress', 'complete': 'completed', 'release': 'open'}[body['action']]
            if result.get('state') != state or target.get('kind') != 'task':
                raise BoundaryError('task claim receipt does not match the requested transition')
            self._claim_receipt(body, result)
            claim = ({key: result[key] for key in ('session_id', 'fence', 'expires_at')}
                     if body['action'] == 'claim' else None)
            record = {**identity, **{key: deepcopy(target[key]) for key in
                                     ('kind', 'classification', 'data_scope', 'project_id', 'tags')},
                      'data': dict(deepcopy(target['data']), state=state), 'claim': claim,
                      'claim_receipt': result}
        return self._finish_request(route, body, record, result)

    def _finish_request(self, route, body, record, result):
        save_private(self.root, self.runtime + '/last-memory-write.json', {
            'route': route, 'request_key': body['request_key'], 'record': self.ref(record), 'result': result})
        private_path(self.root, self.pending_path).unlink(missing_ok=True)
        return record

    def _validate_request(self, route, body, *, target=None):
        """Reject malformed mutations before any network call; returns a claim's exact target."""
        from .client import MemoryClientError
        from .validation import MemoryError, validate
        if route not in ('write', 'claim'):
            raise BoundaryError('only typed memory mutations can use the session journal')
        try:
            validate('/v1/memory/' + route, body)
        except MemoryError as error:
            raise MemoryClientError(error.status, detail=error.metadata.get('detail')) from None
        if route == 'write' and (body.get('project_id') not in (None, self.project_id)
                                or body.get('classification') != 'Sensitive' or body.get('data_scope') != 'owner'):
            raise BoundaryError('memory mutation does not match selected project or universal scope')
        if route == 'claim' and body.get('session_id') != self.session_id:
            raise BoundaryError('task claim belongs to another session')
        if route == 'claim':
            if target is None:
                target = self.read(body['record_id'], body['expected_revision'], scope='context')
            elif target.get('record_id') != body['record_id'] or target.get('revision') != body['expected_revision']:
                raise BoundaryError('task claim target differs from the exact requested version')
        if route == 'write' and body['kind'] in ('session', 'checkpoint', 'operation'):
            owner = body['record_id'] if body['kind'] == 'session' else body['data']['session_id']
            if (body.get('project_id') != self.project_id
                    or owner != self.session_id and not self._peer_abandonment(body)):
                raise BoundaryError('lifecycle record belongs to another project or session')
        return target

    def _peer_abandonment(self, body):
        """The only lifecycle write for another session: this session's owner-confirmed abandonment of it."""
        if body['kind'] != 'session' or body['data'].get('state') != 'abandoned':
            return False
        try:
            meta = json.loads(body['data']['summary'])
        except (ValueError, TypeError):
            return False
        return (isinstance(meta, dict) and meta.get('abandoned_by') == self.session_id
                and meta.get('boundary_outcome') == 'abandoned-by-owner')

    def _submit(self, route, body, *, target=None):
        target = self._validate_request(route, body, target=target)
        if private_path(self.root, self.pending_path).exists():
            raise BoundaryError('an unresolved cloud request exists; run explicit recover before further writes')
        if self.authority_generation is not None and 'authority_generation' not in body:
            body = dict(body, authority_generation=self.authority_generation)
        request = {'route': route, 'body': body, 'session_id': self.session_id}
        request['digest'] = digest(request)
        save_private(self.root, self.pending_path, request)
        from .client import DEFINITIVE_MUTATION_ERRORS, MemoryClientError
        try:
            response = self.client.invoke('/v1/memory/' + route, body)
        except MemoryClientError as error:
            if error.code in DEFINITIVE_MUTATION_ERRORS:
                save_private(self.root, self.runtime + '/failed-memory-write.json',
                             {'request': request, 'status': error.code})
                private_path(self.root, self.pending_path).unlink()
            raise
        result = response.get('data')
        if response.get('status') != 'ready' or not isinstance(result, dict):
            raise BoundaryError('memory service returned no verified result')
        if response.get('reconciled'):
            return result, self._complete_request(route, body, result)
        return result, self._complete_receipt(route, body, result, target)

    def submit(self, route, body):
        """Journal an exact caller-owned request key. Caller holds the session lock/writer guard."""
        if route.startswith('/v1/memory/'):
            route = route[len('/v1/memory/'):]
        result, _ = self._submit(route, body)
        return {'status': 'ready', 'data': result}

    def write(self, kind, data, *, record_id=None, expected_revision=0, tags=(),
              reason='Record managed work', source=None, scope='project', request_key=None):
        record_id = record_id or str(uuid.uuid4())
        if scope not in ('project', 'universal'):
            raise BoundaryError('writes require explicit project or universal scope')
        body = {'record_id': record_id, 'expected_revision': expected_revision, 'kind': kind,
                'classification': 'Sensitive', 'data_scope': 'owner',
                'data': data, 'tags': list(tags), 'source': source if source is not None else {
                    'origin': 'managed-work-boundary', 'excerpt': 'Explicit local work-boundary operation'},
                'change_reason': reason}
        if scope == 'project':
            body['project_id'] = self.project_id
        body['request_key'] = request_key or str(uuid.uuid5(uuid.UUID(record_id), digest(body)))
        _, record = self._submit('write', body)
        return record

    def recover(self):
        """Reconcile the exact persisted request key; never generate a replacement."""
        from .client import DEFINITIVE_MUTATION_ERRORS, MemoryClientError
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
                if retry.code in DEFINITIVE_MUTATION_ERRORS:
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
        self.authority_generation = data['authority_generation']
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
        return self.verified_session(self.read(self.session_id), closed=closed)

    def verified_session(self, record, *, closed=False):
        """Check an already-read session record exactly as session() would."""
        meta = self.metadata(record)
        if (record.get('record_id') != self.session_id or record.get('kind') != 'session'
                or meta.get('root') != str(self.root)
                or record['data']['state'] not in (('active', 'closed', 'abandoned') if closed else ('active',))):
            raise BoundaryError('an exact bound active cloud session is required')
        return record

    def project_context(self):
        """Latest handover across the project, peers, open work and owner preferences in one request.
        The MAVEN Home project also receives `project_index` (identity, name, state, last activity)."""
        data = self.invoke('project_context', {'project_id': self.project_id, 'session_id': self.session_id})
        handover = data.get('latest_handover')
        lists = ('active_peers', 'tasks', 'blockers', 'owner_preferences')
        index = data.get('project_index')
        if ((handover is not None and not isinstance(handover, dict))
                or any(not isinstance(data.get(key), list)
                       or any(not isinstance(item, dict) for item in data[key]) for key in lists)
                or (index is not None and (not isinstance(index, list) or len(index) > 100
                                           or any(not isinstance(item, dict) for item in index)))
                or any(peer.get('session_id') == self.session_id for peer in data['active_peers'])):
            raise BoundaryError('memory service returned no verified project context')
        return {'latest_handover': handover, **{key: data[key] for key in lists}, 'project_index': index}

    def revisions(self, record_ids):
        """Current visible revision per record, one request per hundred identifiers."""
        identifiers = list(dict.fromkeys(record_ids))
        result = {}
        for start in range(0, len(identifiers), 100):
            chunk = identifiers[start:start + 100]
            revisions = self.invoke('revisions', {'record_ids': chunk}).get('revisions')
            if (not isinstance(revisions, dict) or set(revisions) - set(chunk)
                    or any(type(value) is not int or value < 1 for value in revisions.values())):
                raise BoundaryError('memory service returned no verified revisions')
            result.update(revisions)
        return result

    def verify_refs(self, refs, *, current=False, message='memory reference is unavailable or changed'):
        """Exact versions must exist (current=True: still be latest) without one read per reference."""
        revisions = self.revisions([ref['record_id'] for ref in refs])
        for ref in refs:
            actual = revisions.get(ref['record_id'])
            if actual is None or (actual != ref['revision'] if current else actual < ref['revision']):
                raise BoundaryError(message)
        return revisions

    def start(self, git_base):
        context = self.context()
        if private_path(self.root, self.SESSION_PATH).exists():
            raise BoundaryError('session already has a cloud pointer; explicitly resume it')
        from .skills import SessionSkills
        skills = SessionSkills(self.client, self.root, self.project_id, self.session_id)
        prepared = skills.start()
        data = {'state': 'active', 'workflow': 'maven-workflow', 'summary': json.dumps({
            'root': str(self.root), 'harness': self.harness, 'harness_session': self.harness_session,
            'machine': socket.gethostname().split('.')[0] or None,
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
        if any(ref['record_id'] == self.session_id for ref in refs):
            raise BoundaryError('summary memory references must be current work records')
        self.verify_refs(refs, current=True, message='summary memory references must be current work records')
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
        from tools.git_workflow import candidate_digest, git
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
            self.verify_refs(meta.get('memory_refs', []), current=True,
                             message='DB work reference changed after the session summary')
            return []
        except BoundaryError as error:
            return [str(error)]

    def prepare_checkpoint(self, root, paths, base, reservation_id):
        issues = self.freshness_issues(root, paths, reservation_id=reservation_id)
        if issues:
            raise BoundaryError(issues[0])
        from tools.git_workflow import candidate_digest
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
        revisions = self.revisions([memory_ref['record_id'] for memory_ref in data['memory_refs']])
        for memory_ref in data['memory_refs']:
            actual = revisions.get(memory_ref['record_id'])
            if actual == memory_ref['revision']:
                continue
            # Resuming after closure advanced only this session record past the approved
            # version; retained history still proves the approved version.
            if (resume and memory_ref['record_id'] == self.session_id and actual is not None
                    and actual > memory_ref['revision']
                    and self.metadata(self.read(self.session_id)).get('last_checkpoint') == ref):
                continue
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

    def abandon_peer(self, record, *, reason, source):
        """Owner-confirmed closure of another active session of this project that no longer runs.
        Only its cloud record changes; that session's machine and local files are untouched."""
        import datetime
        if (record.get('kind') != 'session' or record.get('project_id') != self.project_id
                or record.get('record_id') == self.session_id or record['data'].get('state') != 'active'):
            raise BoundaryError('only another active session of this project can be closed')
        meta = self.metadata(record)
        meta.update(phase='abandoned', boundary_outcome='abandoned-by-owner', abandoned_by=self.session_id,
                    abandoned_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), reason=reason)
        return self.ref(self.write('session', dict(record['data'], state='abandoned',
                                   summary=json.dumps(meta, sort_keys=True)), record_id=record['record_id'],
                                   expected_revision=record['revision'], tags=record['tags'],
                                   source=source, reason=reason))
