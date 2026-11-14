"""Local conversation identity and read-only lifecycle diagnosis for every harness.

Hooks never open cloud sessions. Native conversation IDs identify exact bindings;
workspace paths, timestamps and process IDs never select a session to resume.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from tools.coordination import private_lock, session_uuid
from tools.home import start_hint
from tools.memory.boundary import BoundaryError, digest, private_path, read_private, save_private
from tools.project import load_project


def _conversation_path(harness, harness_session):
    if not harness_session:
        return None
    if (not isinstance(harness, str) or not harness.strip() or
            not isinstance(harness_session, str) or len(harness_session) > 4096):
        raise BoundaryError('a genuine harness and conversation identity are required')
    return 'runtime/conversations/' + digest([harness, harness_session]) + '.json'


def _optional(root, relative):
    path = private_path(root, relative)
    return read_private(root, relative) if path.exists() else None


def conversation_binding(home, *, harness, harness_session):
    path = _conversation_path(harness, harness_session)
    record = _optional(home, path) if path else None
    if record is None:
        return None
    binding = record.get('binding', {})
    if (record.get('schema') != 1 or binding.get('harness') != harness or
            binding.get('harness_session') != harness_session or
            binding.get('home') != str(Path(home).resolve()) or
            binding.get('env') != str(Path(home).resolve() / '.env')):
        raise BoundaryError('conversation binding differs from this installation or harness')
    session_uuid(binding.get('session_id'))
    session_uuid(binding.get('project_id'))
    root = Path(binding.get('root', ''))
    if not root.is_absolute() or root.resolve() != root:
        raise BoundaryError('conversation binding has an invalid project root')
    return dict(binding)


def _session_state(binding):
    root = Path(binding['root'])
    prefix = f'.maven/runtime/sessions/{session_uuid(binding["session_id"])}'
    persisted = _optional(root, prefix + '/mcp-binding.json')
    if persisted is None or any(persisted.get(key) != value for key, value in binding.items()):
        raise BoundaryError('the original session binding is missing or changed; inspect recovery evidence')
    project = load_project(root)
    if project['project_id'] != binding['project_id']:
        raise BoundaryError('the folder now belongs to another project; old session cannot be reused')
    finish = _optional(root, prefix + '/finish.json')
    pending = _optional(root, prefix + '/pending-memory.json')
    maintenance = _optional(root, prefix + '/maintenance.json') or {}
    if maintenance and maintenance.get('binding') != {
            key: binding[key] for key in ('root', 'session_id', 'project_id')}:
        raise BoundaryError('maintenance belongs to a different session')
    assignments = maintenance.get('assignments', {})
    checkpoints = maintenance.get('checkpoints', {})
    pending_count = sum(item.get('state') not in ('completed', 'superseded', 'cancelled')
                        for item in assignments.values())
    closed = bool(finish and finish.get('status') == 'closed')
    if closed and any(finish.get(key) != binding[key] for key in ('root', 'project_id', 'session_id')):
        raise BoundaryError('closure receipt belongs to another session')
    issues = []
    if pending:
        issues.append({'kind': 'uncertain_write', 'request_key': pending.get('body', {}).get('request_key'),
                       'route': pending.get('route'), 'action': 'recover_original_request'})
    if finish and not closed or maintenance.get('closure') and not closed:
        issues.append({'kind': 'unfinished_closure',
                       'checkpoint_id': maintenance.get('closure', {}).get('checkpoint_id'),
                       'action': 'recover_original_closure'})
    for identifier, item in checkpoints.items():
        if item.get('state') not in ('saved', 'cancelled'):
            issues.append({'kind': 'unfinished_checkpoint', 'checkpoint_id': identifier,
                           'action': 'inspect_original_checkpoint'})
    if closed and (pending or pending_count):
        raise BoundaryError('closure conflicts with retained unfinished work; inspect before exiting')
    return {'status': 'closed' if closed else 'open', 'binding': dict(binding),
            'initialized': bool(persisted.get('initialized') or persisted.get('resume_required')),
            'pending_count': pending_count, 'paused': maintenance.get('paused', False),
            'issues': issues}


def bind_conversation(home, binding):
    """Atomically reserve an exact native conversation before any session cloud write."""
    path = _conversation_path(binding['harness'], binding['harness_session'])
    if path is None:
        return
    with private_lock(home, path + '.lock'):
        previous = conversation_binding(home, harness=binding['harness'], harness_session=binding['harness_session'])
        if previous and previous != binding and _session_state(previous)['status'] != 'closed':
            raise BoundaryError('this conversation already has an unfinished session; resume its exact binding')
        # An actual initialization/resume supersedes the prior end-event hint.
        save_private(home, path, {'schema': 1, 'binding': binding})


def record_shutdown(home, cwd, *, harness, harness_session, reason='exit'):
    """Best-effort event evidence, not an attestation of process death or cloud closure."""
    path = _conversation_path(harness, harness_session)
    if path is None:
        return {'status': 'unbound', 'message': 'No native conversation identity; no session was marked closed.'}
    with private_lock(home, path + '.lock'):
        binding = conversation_binding(home, harness=harness, harness_session=harness_session)
        if binding is None:
            return {'status': 'unbound', 'message': 'No MAVEN binding for this conversation.'}
        record = read_private(home, path)
        record['last_end_event'] = {'reason': str(reason)[:80], 'at': datetime.now(timezone.utc).isoformat()}
        save_private(home, path, record)
    return inspect_shutdown(home, cwd, harness=harness, harness_session=harness_session)


def inspect_shutdown(home, cwd, *, harness, harness_session):
    try:
        binding = conversation_binding(home, harness=harness, harness_session=harness_session)
        if binding is None:
            return {'status': 'unbound', 'message': 'No exact MAVEN session binding for this conversation.'}
        result = _session_state(binding)
        result['message'] = ('MAVEN session closure is verified.' if result['status'] == 'closed' else
                             'MAVEN has an open session. Wrap up before exiting, or leave it for recovery.')
        return result
    except (BoundaryError, ValueError, OSError, KeyError, TypeError):
        return {'status': 'unknown', 'message': 'MAVEN session state could not be verified. Inspect recovery; no closure is assumed.'}


def _local_sessions(home, root):
    registry = _optional(home, 'runtime/sessions.json')
    if registry is None:
        return []
    if registry.get('schema') != 1 or not isinstance(registry.get('sessions'), dict):
        raise BoundaryError('local session registry is malformed; preserve it for recovery')
    sessions = []
    for identifier, item in registry['sessions'].items():
        if item.get('root') != str(root):
            continue
        session_uuid(identifier)
        entry = {key: item.get(key) for key in ('project_id', 'harness', 'harness_session', 'state')}
        entry.update(session_id=identifier, writers=list(item.get('writers', [])))
        path = f'.maven/runtime/sessions/{identifier}/mcp-binding.json'
        bound = _optional(root, path)
        if bound is not None:
            binding = {key: bound.get(key) for key in (
                'root', 'session_id', 'home', 'env', 'harness', 'harness_session', 'project_id')}
            status = _session_state(binding)
            entry.update(issues=status['issues'], pending_count=status['pending_count'], paused=status['paused'],
                         closure_verified=status['status'] == 'closed')
            native_path = _conversation_path(binding['harness'], binding['harness_session'])
            native = _optional(home, native_path) if native_path else None
            if native and native.get('binding') == binding and native.get('last_end_event'):
                entry['last_end_event'] = native['last_end_event']
        sessions.append(entry)
    return sessions


def inspect_startup(home, cwd, *, harness=None, harness_session=None):
    """Resolve only explicit folder enablement or this exact conversation's prior choice."""
    home, cwd = Path(home).expanduser().resolve(), Path(cwd).expanduser().resolve()
    try:
        binding = conversation_binding(home, harness=harness, harness_session=harness_session)
        root = Path(binding['root']) if binding else next((path for path in (cwd, *cwd.parents)
                    if (path / '.maven/project.json').exists() or (path / '.maven/project.json').is_symlink()), None)
        if root is None:
            return {'status': 'choose', 'start_hint': start_hint(home, cwd), 'local_sessions': [], 'issues': [],
                    'message': 'MAVEN: no enabled project here; choose Home, enable a project, or Read Only.'}
        project = load_project(root)
        sessions = _local_sessions(home, root)
        result = {'status': 'initialize', 'root': str(root), 'project_id': project['project_id'],
                  'local_sessions': sessions, 'issues': [], 'message': 'MAVEN: checking project ' + root.name + '…'}
        if binding:
            state = _session_state(binding)
            result.update(binding=binding, issues=state['issues'], paused=state['paused'],
                          pending_count=state['pending_count'], resume_required=state['initialized'])
            result['status'] = 'closed' if state['status'] == 'closed' else 'recovery' if state['issues'] else 'resume'
        else:
            for item in sessions:
                if (item.get('issues') or item.get('state') == 'abandoned'
                        or (item.get('last_end_event') and not item.get('closure_verified'))):
                    result['issues'].append({'kind': 'unfinished_prior_session', 'session_id': item['session_id'],
                                             'action': 'review_or_start_new_without_takeover'})
            if result['issues']:
                result['status'] = 'recovery'
        return result
    except (BoundaryError, ValueError, OSError, KeyError, TypeError):
        return {'status': 'blocked', 'issues': [{'kind': 'local_binding_invalid', 'action': 'review_local_repair'}],
                'local_sessions': [], 'message': 'MAVEN: local selection or session evidence is invalid; preserve it and review repair.'}


def project_sessions(client, project_id, *, maximum=100):
    """Bounded project-only read. Staleness is a warning, never proof a writer stopped."""
    sessions, cursor = [], None
    while len(sessions) < maximum:
        body = {'scope': 'project', 'project_id': project_id, 'kind': 'session',
                'limit': min(20, maximum - len(sessions)), 'include_archived': True}
        if cursor:
            body['after_id'] = cursor
        response = client.invoke('/v1/memory/search', body)
        page = response.get('data')
        if response.get('status') != 'ready' or not isinstance(page, dict) or not isinstance(page.get('records'), list):
            raise BoundaryError('project session inspection returned no verified result')
        for record in page['records']:
            if record.get('project_id') != project_id or record.get('kind') != 'session' or record.get('data_scope') != 'owner':
                raise BoundaryError('session inspection returned a different project or scope')
            data = record['data']
            try:
                meta = json.loads(data.get('summary', '{}'))
            except (ValueError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            updated = record.get('updated_at')
            old = False
            if updated:
                try:
                    stamp = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                    old = stamp.tzinfo is not None and stamp < datetime.now(timezone.utc) - timedelta(hours=24)
                except (ValueError, TypeError):
                    pass
            sessions.append({'session_id': record['record_id'], 'revision': record['revision'],
                             'state': data.get('state'), 'machine': meta.get('machine'), 'harness': meta.get('harness'),
                             'last_record_update': updated, 'possibly_stale': data.get('state') == 'active' and old,
                             'last_checkpoint': meta.get('last_checkpoint')})
        next_cursor = page.get('next_after_id')
        if not next_cursor:
            return {'sessions': sessions, 'next_after_id': None, 'complete': True}
        if not page['records'] or next_cursor == cursor:
            raise BoundaryError('project session pagination made no progress')
        cursor = next_cursor
    return {'sessions': sessions, 'next_after_id': cursor, 'complete': False}
