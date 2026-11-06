"""Owner-private, flock-serialized session and shared credential coordination.

No process liveness guesses or lease expiry: interrupted work needs explicit recovery.
The registry coordinates participating writers, not arbitrary editors.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import uuid

from tools.memory.boundary import BoundaryError, private_path, read_private, save_private
from tools.project import instance_home


class CoordinationError(BoundaryError):
    pass


def session_uuid(value):
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise CoordinationError('a canonical MAVEN session UUID is required') from None
    if value != canonical:
        raise CoordinationError('a canonical MAVEN session UUID is required')
    return canonical


@contextlib.contextmanager
def private_lock(root, relative, *, blocking=True):
    path = private_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CoordinationError('coordination lock must be owner-private')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise CoordinationError('another command is using this session') from None
        yield
    finally:
        os.close(descriptor)


def session_lock(root, session_id):
    return private_lock(root, f'.maven/runtime/sessions/{session_uuid(session_id)}/command.lock', blocking=False)


class SessionRegistry:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else instance_home()

    @contextlib.contextmanager
    def transaction(self):
        with private_lock(self.home, 'runtime/registry.lock'):
            path = private_path(self.home, 'runtime/sessions.json')
            state = read_private(self.home, 'runtime/sessions.json') if path.exists() else {
                'schema': 1, 'sessions': {}, 'reservations': {}}
            if state.get('schema') != 1 or not isinstance(state.get('sessions'), dict) or not isinstance(state.get('reservations'), dict):
                raise CoordinationError('session registry is malformed; preserve it for recovery')
            yield state
            save_private(self.home, 'runtime/sessions.json', state)

    @staticmethod
    def key(root):
        return str(Path(root).resolve())

    @staticmethod
    def caches(values):
        result = []
        for prefix in ('MAVEN_AWS', 'MAVEN_DEPLOY'):
            profile, cache = values.get(prefix + '_PROFILE'), values.get(prefix + '_LOGIN_CACHE')
            if profile or cache:
                if not profile or not cache or not Path(cache).expanduser().is_absolute():
                    raise CoordinationError('shared access requires explicit profile and absolute cache directory')
                item = {'profile': profile, 'cache': str(Path(cache).expanduser().resolve())}
                if item not in result:
                    result.append(item)
        return result

    def register(self, root, project_id, session_id, values, *, harness=None, harness_session=None):
        session_uuid(session_id)
        key = self.key(root)
        with self.transaction() as state:
            if session_id in state['sessions']:
                raise CoordinationError('session is already registered; explicitly resume it')
            if key in state['reservations']:
                raise CoordinationError('repository is reserved; new writers cannot start')
            if harness_session and any(item['root'] == key and item.get('harness') == harness
                                       and item.get('harness_session') == harness_session
                                       for item in state['sessions'].values()):
                raise CoordinationError('this harness context already has a session; explicitly resume it')
            state['sessions'][session_id] = {
                'root': key, 'project_id': project_id, 'harness': harness, 'harness_session': harness_session,
                'state': 'starting', 'writers': [], 'caches': self.caches(values)}

    def _session(self, state, root, session_id):
        item = state['sessions'].get(session_uuid(session_id))
        if item is None or item['root'] != self.key(root):
            raise CoordinationError('session is not registered for this explicit project root')
        return item

    @staticmethod
    def pending(root, session_id):
        return private_path(root, f'.maven/runtime/sessions/{session_uuid(session_id)}/pending-memory.json').exists()

    def activate(self, root, session_id):
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            if self.key(root) in state['reservations']:
                raise CoordinationError('repository reservation prevents writer resume')
            if item['writers'] or self.pending(root, session_id):
                raise CoordinationError('session has unfinished writers or cloud receipts; recover them explicitly')
            item['state'] = 'active'
            item.pop('pause_ack', None)

    def peers(self, root, session_id):
        with self.transaction() as state:
            return [dict(item, session_id=identifier) for identifier, item in state['sessions'].items()
                    if item['root'] == self.key(root) and identifier != session_id]

    def reserve(self, root, session_id):
        key = self.key(root)
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            current = state['reservations'].get(key)
            if current and current['session_id'] != session_id:
                raise CoordinationError('another session owns the repository reservation')
            if item['writers'] or self.pending(root, session_id):
                raise CoordinationError('requesting session has unfinished writers or cloud receipts')
            if current is None:
                current = {'reservation_id': str(uuid.uuid4()), 'session_id': session_id}
                state['reservations'][key] = current
            item['state'] = 'paused'
            item['pause_ack'] = current['reservation_id']
            return dict(current)

    def pause(self, root, session_id, reservation_id=None):
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            if item['writers'] or self.pending(root, session_id):
                raise CoordinationError('cannot acknowledge pause while writer or cloud outcomes remain unresolved')
            current = state['reservations'].get(self.key(root))
            if current and reservation_id != current['reservation_id']:
                raise CoordinationError('pause must acknowledge the exact current reservation ID')
            if reservation_id and not current:
                raise CoordinationError('pause reservation is no longer current')
            item['state'] = 'paused'
            item['pause_ack'] = reservation_id
            return {'session_id': session_id, 'reservation': current}

    def quiescent(self, root, session_id, reservation_id=None):
        with self.transaction() as state:
            self._session(state, root, session_id)
            current = state['reservations'].get(self.key(root))
            if not current or current['session_id'] != session_id or (
                    reservation_id is not None and reservation_id != current['reservation_id']):
                raise CoordinationError('an exact requester-owned repository reservation is required')
            waiting = [identifier for identifier, item in state['sessions'].items()
                       if item['root'] == self.key(root) and (item['writers'] or item['state'] != 'paused'
                           or item.get('pause_ack') != current['reservation_id']
                           or self.pending(root, identifier))]
            if waiting:
                raise CoordinationError('awaiting explicit peer pause acknowledgments: ' + ', '.join(waiting)
                                        + '; reservation ' + current['reservation_id'])
            return dict(current)

    def release(self, root, session_id, *, activate=False):
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            current = state['reservations'].get(self.key(root))
            if current and current['session_id'] != session_id:
                raise CoordinationError('cannot release another session reservation')
            state['reservations'].pop(self.key(root), None)
            if activate:
                item['state'] = 'active'
                item.pop('pause_ack', None)

    @contextlib.contextmanager
    def writer(self, root, session_id):
        token = str(uuid.uuid4())
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            if item['state'] != 'active' or self.key(root) in state['reservations'] or self.pending(root, session_id):
                raise CoordinationError('session is paused or repository is reserved; writes are prohibited')
            item['writers'].append(token)
        try:
            yield token
        finally:
            with self.transaction() as state:
                item = self._session(state, root, session_id)
                item['writers'].remove(token)

    def recover_writers(self, root, session_id, tokens):
        """Owner attests these exact interrupted operations have stopped; never guess."""
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            if sorted(tokens) != sorted(item['writers']):
                raise CoordinationError('recovery must name the exact complete in-flight writer token set')
            item['writers'] = []
            item['state'] = 'paused'

    def inspect(self, root, session_id):
        with self.transaction() as state:
            return {'session': dict(self._session(state, root, session_id)),
                    'reservation': state['reservations'].get(self.key(root))}

    def close(self, root, session_id, logout):
        """Remove only this session. Serialize last-user logout with registration."""
        with self.transaction() as state:
            if session_id not in state['sessions']:
                return {'verified': True, 'issues': [], 'retained_for_peers': [],
                        'already_released': True, 'token_revocation_claimed': False,
                        'browser_signout_required': True}
            item = self._session(state, root, session_id)
            if item['writers']:
                raise CoordinationError('cannot close a session with unfinished writers')
            reservation = state['reservations'].get(self.key(root))
            if reservation and reservation['session_id'] == session_id:
                del state['reservations'][self.key(root)]
            peers = {identifier: peer for identifier, peer in state['sessions'].items() if identifier != session_id}
            retained, issues = [], []
            for cache in item['caches']:
                users = [identifier for identifier, peer in peers.items()
                         if any(other['cache'] == cache['cache'] for other in peer['caches'])]
                if users:
                    retained.append({'cache': cache, 'sessions': users})
                else:
                    issues.extend(logout(cache))
            if issues:
                item['state'] = 'cleanup_pending'
            else:
                del state['sessions'][session_id]
            return {'verified': not issues, 'issues': issues, 'retained_for_peers': retained,
                    'token_revocation_claimed': False, 'browser_signout_required': not retained}

    def revoke(self, root, session_id, logout):
        """Explicit owner logout may affect peers; report and pause every affected session."""
        with self.transaction() as state:
            item = self._session(state, root, session_id)
            caches = {cache['cache'] for cache in item['caches']}
            affected = {identifier: peer for identifier, peer in state['sessions'].items()
                        if any(cache['cache'] in caches for cache in peer['caches'])}
            cleanup = []
            targets = []
            for peer in affected.values():
                peer['state'] = 'access_revoked'
                for cache in peer['caches']:
                    if cache['cache'] in caches and cache not in targets:
                        targets.append(cache)
            for cache in targets:
                cleanup.extend(logout(cache))
            return {'verified': not cleanup, 'issues': cleanup, 'affected_sessions': sorted(affected),
                    'retained_for_peers': [], 'token_revocation_claimed': False,
                    'browser_signout_required': True}
