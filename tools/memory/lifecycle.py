"""Memory-native sessions: explicit identity, serialized journals, verified cloud receipts."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import uuid

from tools.coordination import SessionRegistry, session_lock, session_uuid
from tools.project import default_environment, load_project, project_values
from .boundary import BoundaryError, MemoryBoundary, digest, private_path, read_private, save_private

PROJECT_CONTEXT_KEYS = ('latest_handover', 'active_peers', 'tasks', 'blockers', 'owner_preferences', 'project_index')


def registered_sessions(root, *, home=None):
    """Local registry sessions for this root, for rebind hints; never contacts the cloud."""
    return SessionRegistry(home).registered(Path(root).expanduser().resolve())


class SessionService:
    """One bound session. Approved authority and the cloud session-active check are cached
    per process: loaded at initialize/context, reloaded before the next governed write
    after any response reports a different authority generation, never per call. Every
    governed write carries the cached generation; the service refuses it if authority moved."""

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
        self._authority = None
        self._stale = False
        self._session_checked = False

    def invalidate(self, *, authority=True):
        """Forget the cached session-active check (and, by default, authority); the next call reloads."""
        if authority:
            self._authority = None
            self._stale = False
        self._session_checked = False

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
        if load_project(self.root)['project_id'] != self.project_id:
            raise BoundaryError('project binding changed; this session cannot operate on the new project')
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

    @contextmanager
    def _session_lock(self, *, shared=False):
        # Keep the root stable before creating/opening any per-project runtime path.
        # Folder switches take this gate exclusively, then the registry lock.
        from tools.coordination import project_binding_lock, private_lock
        with project_binding_lock(self.home, self.root):
            with private_lock(self.home, 'runtime/registry.lock'):
                self.registry._allow_work(self.root)
                if load_project(self.root)['project_id'] != self.project_id:
                    raise BoundaryError('project binding changed; start a new session for the selected project')
            with session_lock(self.root, self.session_id, shared=shared):
                yield

    def _publication_guard(self):
        path = self.runtime + '/approval.json'
        if private_path(self.root, path).exists():
            receipt = read_private(self.root, path)
            if receipt.get('status') not in ('closed', 'checkpointed', 'cancelled', 'approval_cancelled'):
                raise BoundaryError('pending Git publication receipt: use maven-git recovery; memory cutover is blocked')

    def _no_pending(self):
        if private_path(self.root, self.pending_path).exists():
            raise BoundaryError('unresolved cloud request; explicitly recover its original request before continuing')

    def _guard(self, *, closed=False):
        """Local guards run on every call; they never need the network."""
        if self._binding() is None:
            raise BoundaryError('initialize this exact session before using memory')
        self._publication_guard()
        if not closed and private_path(self.root, self.runtime + '/finish.json').exists():
            raise BoundaryError('session closure has begun; recover its original finish receipt')

    def _ready(self, memory, *, write):
        """Use cached cloud state; load it on first use, and before a governed write after a
        response reported a different authority generation."""
        self._guard()
        reload = self._authority is None or write and self._stale
        if not self._session_checked or reload and self._stale:
            memory.session()
            self._session_checked = True
        if reload:
            self._authority = memory.context()
            self._stale = False
        memory.authority_generation = self._authority['authority_generation']

    @contextmanager
    def _writer(self):
        """This session's single memory writer. A write refused because authority changed
        (context_stale) committed nothing; the next governed call reloads authority."""
        from .client import MemoryClientError
        with self.registry.memory_writer(self.root, self.session_id):
            try:
                yield
            except MemoryClientError as error:
                if error.code == 'context_stale':
                    self._stale = True
                raise

    def _observe(self, result):
        generation = getattr(self.client, 'last_generation', None)
        if (type(generation) is int and self._authority is not None
                and generation != self._authority['authority_generation']):
            self._stale = True
        return {**result, 'authority_changed': True} if self._stale else result

    def _report(self, status, **fields):
        return dict(status=status, root=str(self.root), project_id=self.project_id,
                    session_id=self.session_id, **fields)

    def _skills(self):
        from .skills import SessionSkills
        return SessionSkills(self.client, self.root, self.project_id, self.session_id).start()['data']

    def initialize(self, *, resume=False):
        with self._session_lock():
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
            self.invalidate()
            memory = self._memory()
            record = memory.restore_pointer()
            if record is None:
                # Start loads current authority and project context for the new session.
                started = memory.start(None)
                authority, skills, session = started['context'], started['skills'], started['session']
                project = {key: started[key] for key in PROJECT_CONTEXT_KEYS}
            else:
                session = memory.ref(memory.verified_session(record))
                skills = self._skills()
                # Always reload full current authority before reporting readiness.
                authority = memory.context()
                project = memory.project_context()
            self._authority, self._session_checked = authority, True
            self.registry.activate(self.root, self.session_id)
            return self._report('ready', context={**authority, **project, 'skills': skills}, session=session)

    def context(self):
        """Explicit reload of authority, session state and project context."""
        with self._session_lock(shared=True):
            self._guard()
            memory = self._memory()
            memory.session()
            self._session_checked = True
            self._authority = memory.context()
            self._stale = False
            context = {**self._authority, 'skills': self._skills(), **memory.project_context()}
            return self._report('ready', context=context)

    def authority_ref(self):
        """Reference to the cached approved authority that governs delegated work."""
        with self._session_lock(shared=True):
            self._ready(self._memory(), write=True)
            return {'authority_generation': self._authority['authority_generation'],
                    'slugs': [page['slug'] for page in self._authority['instructions']]}

    def instructions(self):
        """Every active applicable instruction page; `loaded` marks the exact version in this session."""
        from .client import MemoryClientError
        with self._session_lock(shared=True):
            self._ready(self._memory(), write=False)
            loaded = {(page['slug'], page['revision']) for page in self._authority['instructions']}
            for _ in range(3):
                pages, after, generation = [], None, None
                while True:
                    request = {'limit': 10} if after is None else {'limit': 10, 'after_slug': after}
                    response = self.client.invoke('/v1/pages/index', request)
                    data = response.get('data') if response.get('status') == 'ready' else None
                    if (not isinstance(data, dict) or not isinstance(data.get('pages'), list)
                            or type(data.get('authority_generation')) is not int):
                        raise BoundaryError('memory service returned no verified instruction index')
                    if generation is None:
                        generation = data['authority_generation']
                    elif generation != data['authority_generation']:
                        generation = None
                        break
                    for page in data['pages']:
                        if not isinstance(page, dict) or not isinstance(page.get('scope', {}), dict):
                            raise BoundaryError('memory service returned no verified instruction index')
                        if (page.get('data_scope') != 'owner'
                                or page.get('scope', {}).get('project', self.project_id) != self.project_id):
                            continue
                        pages.append({'slug': page.get('slug'), 'page_kind': page.get('page_kind'),
                                      'title': page.get('title'), 'revision': page.get('revision'),
                                      'requires': page.get('requires', []),
                                      'loaded': (page.get('slug'), page.get('revision')) in loaded})
                    after = data.get('next_after_slug')
                    if after is None:
                        break
                    if not data['pages']:
                        raise BoundaryError('memory service returned no verified instruction index')
                if generation is not None:
                    return self._observe({'authority_generation': generation, 'pages': pages})
            raise MemoryClientError('context_stale')

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

    def read_record(self, record_id, *, revision=None, scope='context', project_id=None, include_archived=False):
        with self._session_lock(shared=True):
            memory = self._memory()
            self._ready(memory, write=False)
            return self._observe(memory.read(record_id, revision, scope=scope, project_id=project_id,
                                             include_archived=include_archived))

    def search(self, *, query='', kind=None, scope='context', project_id=None, after_id=None, limit=20,
               include_archived=False, offset=None):
        # Malformed combinations (scope, offset without query, after_id with query, limit)
        # are rejected locally by the request schema with a field-level detail.
        body = {'scope': scope, 'limit': limit}
        if query:
            body['query'] = query
        if scope in ('project', 'context'):
            body['project_id'] = project_id or self.project_id
        elif project_id is not None:
            body['project_id'] = project_id
        if kind is not None:
            body['kind'] = kind
        if after_id is not None:
            body['after_id'] = after_id
        if include_archived:
            body['include_archived'] = True
        if offset is not None:
            body['offset'] = offset
        with self._session_lock(shared=True):
            memory = self._memory()
            self._ready(memory, write=False)
            page = memory.invoke('search', body)
            if not isinstance(page.get('records'), list):
                raise BoundaryError('memory service returned no verified result')
            return self._observe({
                **page, 'records': [item for item in page['records']
                                    if isinstance(item, dict) and item.get('data_scope') == 'owner'],
                'next_after_id': page.get('next_after_id'), 'next_offset': page.get('next_offset')})

    def save(self, kind, data, *, source, record_id=None, expected_revision=0, scope='project',
             tags=(), reason='Record work', request_key=None):
        if kind in ('session', 'checkpoint'):
            raise BoundaryError('session and checkpoint writes require lifecycle operations')
        if scope not in ('project', 'universal'):
            raise BoundaryError('writes require project or universal scope')
        if kind == 'operation' and data.get('session_id') != self.session_id:
            raise BoundaryError('operation belongs to another session')
        if record_id is None and request_key is not None:
            record_id = str(uuid.uuid5(uuid.UUID(self.session_id), request_key))
        if expected_revision and record_id is None:
            raise BoundaryError('updating memory requires its exact record_id')
        with self._session_lock():
            memory = self._memory()
            self._ready(memory, write=True)
            with self._writer():
                # The cloud CAS binds kind, project and data scope to the exact expected
                # revision, and recognizes a replay of the original request key.
                record = memory.write(kind, data, source=source, record_id=record_id,
                                      expected_revision=expected_revision, scope=scope, tags=tags,
                                      reason=reason, request_key=request_key)
                return self._observe(dict(record, scope=scope))

    def claim(self, record_id, *, action, expected_revision, request_key=None,
              lease_seconds=None, fence=None, source=None, scope=None, reason=None):
        with self._session_lock():
            memory = self._memory()
            self._ready(memory, write=True)
            with self._writer():
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
                _, claimed = memory._submit('claim', body, target=record)
                return self._observe(claimed)

    def close_peer(self, session_id, *, expected_revision, source, reason):
        """Abandon another active session of this project that the owner confirmed no longer runs.
        Only its cloud record (and an active registration on this machine) changes."""
        peer = session_uuid(session_id)
        if peer == self.session_id:
            raise BoundaryError('this session closes only through its own finish or cancel')
        if type(expected_revision) is not int or expected_revision < 1:
            raise BoundaryError('closing another session requires its exact current revision')
        if not isinstance(reason, str) or not reason.strip():
            raise BoundaryError('closing another session requires the owner reason')
        with self._session_lock():
            memory = self._memory()
            self._ready(memory, write=True)
            with self._writer():
                record = memory.read(peer, scope='all')
                if record['kind'] != 'session' or record.get('project_id') != self.project_id:
                    raise BoundaryError('only a session of this project can be closed')
                if record['data'].get('state') != 'active':
                    raise BoundaryError('the other session is no longer active')
                if record['revision'] != expected_revision:
                    raise BoundaryError('the other session changed after the owner confirmed it; reload context')
                closed = memory.abandon_peer(record, reason=reason, source=source)
            local = self.registry.abandon(peer)
            return self._observe(self._report('peer_closed', closed_session=closed, local_registration_closed=local))

    def checkpoint(self, *, summary, refs, cutoff, checkpoint_id):
        checkpoint_id = session_uuid(checkpoint_id)
        if not isinstance(summary, str) or not summary.strip() or type(cutoff) is not int or cutoff < 0:
            raise BoundaryError('checkpoint requires a substantive summary and nonnegative integer cutoff')
        with self._session_lock():
            memory = self._memory()
            self._ready(memory, write=True)
            self._no_pending()
            manifest = {'session_id': self.session_id, 'cutoff': cutoff, 'memory_refs': refs, 'summary': summary}
            path = self.runtime + '/checkpoints/' + checkpoint_id + '.json'
            prepared_before = private_path(self.root, path).exists()
            if prepared_before:
                if read_private(self.root, path)['manifest'] != manifest:
                    raise BoundaryError('checkpoint manifest is immutable; original receipts were preserved')
            else:
                for ref in refs:
                    if (set(ref) != {'record_id', 'revision'} or type(ref['revision']) is not int
                            or ref['revision'] < 1):
                        raise BoundaryError('checkpoint references require exact record_id/revision')
                # Each exact historical version must exist, not whatever is latest after the cutoff;
                # the cloud checkpoint write rechecks scope and visibility of every version.
                memory.verify_refs(refs, message='checkpoint reference is unavailable at its exact revision')
                save_private(self.root, path, {'manifest': manifest, 'status': 'preparing'})
            with self._writer():
                from .client import MemoryClientError
                record = None
                if prepared_before:
                    # Only an earlier attempt can have written this identity.
                    try:
                        record = memory.read(checkpoint_id)
                    except MemoryClientError as error:
                        if error.code != 'record_unavailable':
                            raise
                if record is None:
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
                return self._observe(result)

    def finish(self, *, summary, checkpoint_ref):
        if not isinstance(summary, str) or not summary.strip():
            raise BoundaryError('a substantive session handover is required')
        with self._session_lock():
            self._guard(closed=True)
            memory = self._memory()
            # Closure needs the exact current session revision for its CAS.
            session = memory.session(closed=True)
            self._session_checked = False
            if session['data']['state'] not in ('active', 'closed'):
                raise BoundaryError('only an active session or its original closure can finish')
            self._no_pending()
            if self._authority is None or self._stale:
                self._authority = memory.context()
                self._stale = False
            memory.authority_generation = self._authority['authority_generation']
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
                with self._writer():
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
        with self._session_lock():
            if self._binding() is None:
                raise BoundaryError('recover requires the original explicit session binding')
            self._publication_guard()
            memory = self._memory()
            if self._authority is None or self._stale:
                self._authority = memory.context()
                self._stale = False
            # Recovery may complete a session closure or other lifecycle write.
            self._session_checked = False
            result = memory.recover()
            record = (result or {}).get('record') or {}
            if (result or {}).get('route') == 'write' and record.get('record_id') not in (None, self.session_id):
                # The only lifecycle write for another session is an owner-confirmed peer close.
                self.registry.abandon(record['record_id'])
            return self._report('recovered', recovery=result, next_action='Resume the original operation or initialize this session')

    def close(self):
        self.invalidate()
        if self.client is not None:
            self.client.close()
            self.client = None
