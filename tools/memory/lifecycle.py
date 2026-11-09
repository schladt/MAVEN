"""Memory-native sessions: explicit identity, serialized journals, verified cloud receipts."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from tools.coordination import SessionRegistry, session_lock, session_uuid
from tools.project import default_environment, load_project, project_values
from .boundary import BoundaryError, MemoryBoundary, digest, private_path, read_private, save_private


class SessionService:
    def __init__(self, root, session_id, *, env=None, client=None, harness=None, harness_session=None):
        self.root = Path(root).expanduser().resolve(strict=True)
        self.session_id = session_uuid(session_id)
        self.project = load_project(self.root)
        self.project_id = self.project['project_id']
        self.env = Path(env if env is not None else default_environment()).expanduser().resolve()
        self.home = self.env.parent
        self.client = client
        self._injected = client is not None
        self.harness, self.harness_session = harness, harness_session
        if harness_session is not None and harness is None:
            raise BoundaryError('harness_session requires an actual harness identity')
        self.registry = SessionRegistry(self.home)
        self.runtime = f'.maven/runtime/sessions/{self.session_id}'
        self.binding_path = self.runtime + '/lifecycle.json'
        self.pending_path = self.runtime + '/pending-memory.json'

    def _memory(self):
        if self.client is None:
            from .environment import configured_client
            self.client = configured_client(self.env, values=self._values())
        return MemoryBoundary(self.client, self.root, self.project_id, self.session_id,
                              harness=self.harness, harness_session=self.harness_session)

    def _values(self):
        from .environment import load_environment
        values = project_values({} if self._injected and not self.env.exists() else load_environment(self.env), self.project)
        binding = self._binding()
        if binding is not None and binding.get('identity') != self._identity(values):
            raise BoundaryError('memory instance or credential selection differs from this session')
        return values

    @staticmethod
    def _identity(values):
        return {key: values.get(key) for key in (
            'MAVEN_MEMORY_INSTANCE_ID', 'MAVEN_MEMORY_RECOVERY_EPOCH', 'MAVEN_MEMORY_ENDPOINT',
            'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_AWS_LOGIN_CACHE')}

    def _binding(self):
        if not private_path(self.root, self.binding_path).exists():
            return None
        value = read_private(self.root, self.binding_path)
        if any(value.get(key) != expected for key, expected in (
                ('root', str(self.root)), ('project_id', self.project_id),
                ('session_id', self.session_id), ('env', str(self.env)))):
            raise BoundaryError('session selection differs from its preserved binding')
        for key, selected in (('harness', self.harness), ('harness_session', self.harness_session)):
            if selected is not None and selected != value.get(key):
                raise BoundaryError('harness identity differs from the bound session')
        return value

    def _publication_guard(self):
        path = self.runtime + '/approval.json'
        if private_path(self.root, path).exists():
            receipt = read_private(self.root, path)
            if receipt.get('status') not in ('closed', 'checkpointed', 'cancelled', 'approval_cancelled'):
                raise BoundaryError('pending Git publication receipt: use maven-git recovery; memory cutover is blocked')

    def _no_pending(self):
        if private_path(self.root, self.pending_path).exists():
            raise BoundaryError('unresolved cloud request; explicitly recover its original request before continuing')

    def _require(self, *, closed=False):
        if self._binding() is None:
            raise BoundaryError('initialize this exact session before using memory')
        self._publication_guard()
        if not closed and private_path(self.root, self.runtime + '/finish.json').exists():
            raise BoundaryError('session closure has begun; recover its original finish receipt')
        return self._memory().session(closed=closed)

    def _report(self, status, **fields):
        return dict(status=status, root=str(self.root), project_id=self.project_id,
                    session_id=self.session_id, **fields)

    def initialize(self, *, resume=False):
        with session_lock(self.root, self.session_id):
            self._publication_guard()
            self._no_pending()
            if private_path(self.root, self.runtime + '/finish.json').exists():
                raise BoundaryError('session closure has begun; recover its original finish receipt')
            binding = self._binding()
            if resume and binding is None:
                raise BoundaryError('no memory-native binding for this session; recover legacy work with maven-git')
            values = self._values()
            if binding is None:
                self.registry.register(self.root, self.project_id, self.session_id, values,
                                       harness=self.harness, harness_session=self.harness_session, memory_only=True)
                save_private(self.root, self.binding_path, {
                    'schema': 1, 'root': str(self.root), 'project_id': self.project_id,
                    'session_id': self.session_id, 'env': str(self.env), 'identity': self._identity(values),
                    'harness': self.harness, 'harness_session': self.harness_session})
            else:
                registered = self.registry.inspect(self.root, self.session_id)['session']
                if registered['project_id'] != self.project_id or registered['caches'] != self.registry.caches(values):
                    raise BoundaryError('session credentials or project selection changed')
            memory = self._memory()
            record = memory.restore_pointer()
            if record is None:
                skills = memory.start(None)['skills']
            else:
                memory.session()
                from .skills import SessionSkills
                skills = SessionSkills(self.client, self.root, self.project_id, self.session_id).start()['data']
            # Always reload full current authority before reporting readiness.
            context = self._context(memory, skills=skills)
            self.registry.activate(self.root, self.session_id)
            return self._report('ready', context=context, session=memory.ref(memory.session()))

    def _context(self, memory, *, skills=None):
        from .skills import SessionSkills
        authority = memory.context()
        if skills is None:
            skills = SessionSkills(self.client, self.root, self.project_id, self.session_id).start()['data']
        return {**authority, **memory.project_context(), 'skills': skills}

    def context(self):
        with session_lock(self.root, self.session_id):
            self._require()
            return self._report('ready', context=self._context(self._memory()))

    def status(self):
        # Local inspection only: no configured client, authority fetch, or replay.
        binding = self._binding()
        pending = None
        if private_path(self.root, self.pending_path).exists():
            request = read_private(self.root, self.pending_path)
            body = request.get('body', {})
            pending = {'route': request.get('route'), 'request_key': body.get('request_key'),
                       'record_id': body.get('record_id'), 'expected_revision': body.get('expected_revision')}
        last = None
        if private_path(self.root, self.runtime + '/last-memory-write.json').exists():
            last = read_private(self.root, self.runtime + '/last-memory-write.json')
        finish = None
        if private_path(self.root, self.runtime + '/finish.json').exists():
            finish = read_private(self.root, self.runtime + '/finish.json')
        return self._report('uncertain' if pending else 'closed' if finish and finish.get('status') == 'closed'
                            else 'bound' if binding else 'uninitialized', pending_request=pending,
                            last_verified=last, finish=finish, live_checked=False)

    def read_record(self, record_id, *, revision=None, scope='context', project_id=None):
        with session_lock(self.root, self.session_id):
            self._require()
            memory = self._memory()
            memory.context()
            return memory.read(record_id, revision, scope=scope, project_id=project_id)

    def search(self, *, query='', kind=None, scope='context', project_id=None, after_id=None, limit=20):
        with session_lock(self.root, self.session_id):
            self._require()
            memory = self._memory()
            memory.context()
            if scope not in ('project', 'universal', 'context', 'all'):
                raise BoundaryError('invalid memory search scope')
            body = {'scope': scope, 'limit': limit}
            if query:
                body['query'] = query
            if scope in ('project', 'context'):
                body['project_id'] = project_id or self.project_id
            elif project_id is not None:
                raise BoundaryError('project_id requires project or context search scope')
            if kind is not None:
                body['kind'] = kind
            if after_id is not None:
                body['after_id'] = after_id
            page = memory.invoke('search', body)
            return dict(page, records=[item for item in page['records'] if item.get('data_scope') == 'owner'])

    def save(self, kind, data, *, source, record_id=None, expected_revision=0, scope='project',
             tags=(), reason='Record work', request_key=None):
        if kind in ('session', 'checkpoint'):
            raise BoundaryError('session and checkpoint writes require lifecycle operations')
        if scope not in ('project', 'universal'):
            raise BoundaryError('writes require project or universal scope')
        if kind == 'operation' and data.get('session_id') != self.session_id:
            raise BoundaryError('operation belongs to another session')
        with session_lock(self.root, self.session_id):
            self._require()
            with self.registry.memory_writer(self.root, self.session_id):
                memory = self._memory()
                memory.context()
                if record_id is None and request_key is not None:
                    record_id = str(uuid.uuid5(uuid.UUID(self.session_id), request_key))
                if expected_revision:
                    if record_id is None:
                        raise BoundaryError('updating memory requires its exact record_id')
                    # Verify historical ownership; the cloud CAS rejects stale writes while
                    # still recognizing a replay of the original successful request key.
                    memory.read(record_id, expected_revision, scope=scope)
                record = memory.write(kind, data, source=source, record_id=record_id,
                                      expected_revision=expected_revision, scope=scope, tags=tags,
                                      reason=reason, request_key=request_key)
                return dict(record, scope=scope)

    def claim(self, record_id, *, action, expected_revision, request_key=None,
              lease_seconds=None, fence=None, source=None, scope=None, reason=None):
        with session_lock(self.root, self.session_id):
            self._require()
            with self.registry.memory_writer(self.root, self.session_id):
                memory = self._memory()
                memory.context()
                record = memory.read(record_id, expected_revision, scope='context')
                if scope is not None and (scope not in ('project', 'universal') or
                        (scope == 'universal') != (record.get('project_id') is None)):
                    raise BoundaryError('task claim scope differs from the exact task version')
                if record['kind'] != 'task':
                    raise BoundaryError('claims require a task record')
                body = {'record_id': record_id, 'expected_revision': expected_revision,
                        'session_id': self.session_id, 'action': action,
                        'source': source if source is not None else {'origin': 'memory-session', 'excerpt': 'Explicit task ' + action},
                        'change_reason': reason if reason is not None else 'Task ' + action}
                if lease_seconds is not None:
                    body['lease_seconds'] = lease_seconds
                if fence is not None:
                    body['fence'] = fence
                body['request_key'] = request_key or str(uuid.uuid5(uuid.UUID(self.session_id), digest(body)))
                _, record = memory._submit('claim', body)
                return record

    def checkpoint(self, *, summary, refs, cutoff, checkpoint_id):
        checkpoint_id = session_uuid(checkpoint_id)
        if not isinstance(summary, str) or not summary.strip() or type(cutoff) is not int or cutoff < 0:
            raise BoundaryError('checkpoint requires a substantive summary and nonnegative integer cutoff')
        with session_lock(self.root, self.session_id):
            self._require()
            self._no_pending()
            memory = self._memory()
            memory.context()
            manifest = {'session_id': self.session_id, 'cutoff': cutoff, 'memory_refs': refs, 'summary': summary}
            path = self.runtime + '/checkpoints/' + checkpoint_id + '.json'
            if private_path(self.root, path).exists():
                if read_private(self.root, path)['manifest'] != manifest:
                    raise BoundaryError('checkpoint manifest is immutable; original receipts were preserved')
            else:
                # Validate each historical version, not whatever is latest after the cutoff.
                for ref in refs:
                    if set(ref) != {'record_id', 'revision'}:
                        raise BoundaryError('checkpoint references require exact record_id/revision')
                    memory.read(ref['record_id'], ref['revision'], scope='context')
                save_private(self.root, path, {'manifest': manifest, 'status': 'preparing'})
            with self.registry.memory_writer(self.root, self.session_id):
                from .client import MemoryClientError
                try:
                    record = memory.read(checkpoint_id)
                except MemoryClientError as error:
                    if error.code != 'record_unavailable':
                        raise
                    record = memory.write('checkpoint', dict(manifest, state='prepared'),
                                          record_id=checkpoint_id, tags=['memory-checkpoint'])
                if record['kind'] != 'checkpoint' or {key: value for key, value in record['data'].items()
                                                      if key != 'state'} != manifest:
                    raise BoundaryError('cloud checkpoint differs from the immutable manifest')
                if record['data']['state'] == 'prepared':
                    record = memory.write('checkpoint', dict(record['data'], state='saved'),
                                          record_id=checkpoint_id, expected_revision=record['revision'],
                                          tags=record['tags'], reason='Finalize verified memory checkpoint')
                if record['data']['state'] != 'saved':
                    raise BoundaryError('checkpoint is not saved')
                result = self._report('checkpointed', checkpoint=memory.ref(record), cutoff=cutoff, summary=summary)
                save_private(self.root, path, {'manifest': manifest, **result})
                return result

    def finish(self, *, summary, checkpoint_ref):
        if not isinstance(summary, str) or not summary.strip():
            raise BoundaryError('a substantive session handover is required')
        with session_lock(self.root, self.session_id):
            session = self._require(closed=True)
            if session['data']['state'] not in ('active', 'closed'):
                raise BoundaryError('only an active session or its original closure can finish')
            self._no_pending()
            memory = self._memory()
            memory.context()
            checkpoint = memory.read(checkpoint_ref['record_id'], checkpoint_ref['revision'])
            if (checkpoint['kind'] != 'checkpoint' or checkpoint['data'].get('session_id') != self.session_id
                    or checkpoint['data'].get('state') != 'saved' or 'cutoff' not in checkpoint['data']):
                raise BoundaryError('closure requires this session verified memory checkpoint')
            path = self.runtime + '/finish.json'
            intent = {'summary': summary, 'checkpoint': checkpoint_ref}
            if private_path(self.root, path).exists():
                saved = read_private(self.root, path)
                if saved.get('status') not in ('closing', 'closed') or any(
                        saved.get(key) != value for key, value in intent.items()):
                    raise BoundaryError('finish intent differs from its original receipt')
            else:
                if session['data']['state'] != 'active':
                    raise BoundaryError('closed session recovery requires its original finish receipt')
                save_private(self.root, path, dict(intent, status='closing'))
            meta = memory.metadata(session)
            if session['data']['state'] == 'closed':
                if (meta.get('last_checkpoint') != checkpoint_ref or meta.get('summary') != summary
                        or meta.get('phase') != 'closed' or meta.get('boundary_outcome') != 'memory-saved'):
                    raise BoundaryError('closed session differs from requested handover')
            else:
                with self.registry.memory_writer(self.root, self.session_id):
                    import datetime
                    meta.update(summary=summary, phase='closed', last_checkpoint=checkpoint_ref,
                                boundary_outcome='memory-saved',
                                completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                    session = memory.write('session', dict(session['data'], state='closed',
                                           summary=json.dumps(meta, sort_keys=True)),
                                           record_id=self.session_id, expected_revision=session['revision'],
                                           tags=session['tags'], reason='Close memory session with verified handover')
            self.registry.finish_memory(self.root, self.session_id)
            result = self._report('closed', session=memory.ref(session), **intent)
            save_private(self.root, path, result)
            return result

    def recover(self):
        with session_lock(self.root, self.session_id):
            if self._binding() is None:
                raise BoundaryError('recover requires the original explicit session binding')
            self._publication_guard()
            memory = self._memory()
            memory.context()
            result = memory.recover()
            return self._report('recovered', recovery=result, next_action='Resume the original operation or initialize this session')

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
