"""Local conversation identity and read-only lifecycle diagnosis for every harness.

Hooks never read cloud catalogs or open cloud sessions; maven_startup discovers skills
only after mode selection. Native conversation IDs identify exact bindings;
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


def verified_finish(receipt, binding):
    """Only exact root/project/session evidence proves closure; legacy markers do not."""
    return bool(isinstance(receipt, dict) and receipt.get('status') == 'closed'
                and all(receipt.get(key) == binding[key]
                        for key in ('root', 'project_id', 'session_id')))


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
    closed = verified_finish(finish, binding)
    if finish and finish.get('status') == 'closed' and not closed:
        raise BoundaryError('closure receipt lacks exact root/project/session proof; preserve it for explicit recovery')
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


def failed_start_evidence(home, binding, *, recovery=None):
    """Read conservative local proof; callers hold the project/registry gates for cutover."""
    home = Path(home).resolve()
    root = Path(binding['root'])
    if binding.get('home') != str(home) or binding.get('env') != str(home / '.env'):
        raise BoundaryError('failed initialization belongs to another memory instance')
    state = _session_state(binding)
    prefix = f'.maven/runtime/sessions/{binding["session_id"]}'
    persisted = read_private(root, prefix + '/mcp-binding.json')
    fenced = bool(recovery and persisted == {
        **recovery['persisted_binding'], 'superseded_by': recovery['replacement_binding']['session_id']})
    if recovery:
        if persisted != recovery['persisted_binding'] and not fenced:
            raise BoundaryError('failed-start original binding changed during recovery')
        persisted = recovery['persisted_binding']
    if (persisted.get('initialized') is not False or persisted.get('resume_required', False) is not False
            or persisted.get('superseded_by') or state['status'] != 'open'):
        raise BoundaryError('only a never-initialized, never-resumed binding can be replaced')
    allowed = {'mcp-binding.json', 'lifecycle.json', 'command.lock', 'maintenance.lock',
               'maintenance.json', 'failed-memory-write.json'}
    if recovery:
        allowed.add('failed-start-recovery.json')
    for path in private_path(root, prefix).iterdir():
        if path.name == 'skills' and not path.is_symlink() and path.is_dir():
            if any(child.name != 'registry.json' or child.is_symlink() or not child.is_file()
                   for child in path.iterdir()):
                raise BoundaryError('skill execution evidence prohibits failed-start replacement')
            catalog = _optional(root, prefix + '/skills/registry.json')
            if catalog is not None and (
                    any(catalog.get(key) != binding[key] for key in ('project_id', 'session_id'))
                    or not isinstance(catalog.get('pages'), list)
                    or type(catalog.get('authority_generation')) is not int):
                raise BoundaryError('failed initialization skill catalog is invalid')
        elif path.name not in allowed or path.is_symlink() or not path.is_file():
            raise BoundaryError('retained session work or receipts prohibit failed-start replacement')
    maintenance = _optional(root, prefix + '/maintenance.json')
    if maintenance is not None and (
            maintenance.get('schema') != 1 or maintenance.get('assignments') != {}
            or maintenance.get('checkpoints') != {} or maintenance.get('closure')
            or maintenance.get('catch_up') or maintenance.get('paused') or maintenance.get('sequence') != 0):
        raise BoundaryError('maintenance or checkpoint evidence prohibits failed-start replacement')
    failed = _optional(root, prefix + '/failed-memory-write.json')
    if failed is not None:
        from tools.memory.client import DEFINITIVE_MUTATION_ERRORS
        request = failed.get('request', {})
        body = request.get('body', {})
        if (failed.get('status') not in DEFINITIVE_MUTATION_ERRORS or request.get('route') != 'write'
                or request.get('session_id') != binding['session_id']
                or request.get('digest') != digest({key: value for key, value in request.items() if key != 'digest'})
                or body.get('kind') != 'session' or body.get('record_id') != binding['session_id']
                or body.get('project_id') != binding['project_id'] or body.get('expected_revision') != 0):
            raise BoundaryError('retained failed request is not a definitive initial-session rejection')
    lifecycle = _optional(root, prefix + '/lifecycle.json')
    if lifecycle is not None and (lifecycle.get('schema') != 1 or any(
            lifecycle.get(key) != binding[key] for key in (
                'root', 'session_id', 'project_id', 'env', 'harness', 'harness_session'))):
        raise BoundaryError('failed initialization lifecycle evidence differs from its binding')
    registry = _optional(home, 'runtime/sessions.json')
    entry = None
    if registry is not None:
        if (registry.get('schema') != 1 or not isinstance(registry.get('sessions'), dict)
                or not isinstance(registry.get('reservations'), dict)):
            raise BoundaryError('session registry is malformed; preserve it for recovery')
        entry = registry['sessions'].get(binding['session_id'])
        if str(root) in registry['reservations']:
            raise BoundaryError('a repository reservation prohibits failed-start replacement')
        if entry is not None and (
                any(entry.get(key) != binding[key] for key in ('root', 'project_id', 'harness', 'harness_session'))
                or entry.get('state') != 'starting' or entry.get('writers') != []
                or entry.get('memory_only') is not True):
            raise BoundaryError('the failed session has active or uncertain local use')
    if lifecycle is not None and entry is None and not fenced:
        raise BoundaryError('the original lifecycle registration is missing; inspect recovery')
    native_path = _conversation_path(binding['harness'], binding['harness_session'])
    native = _optional(home, native_path) if native_path else None
    selected = conversation_binding(home, harness=binding['harness'], harness_session=binding['harness_session'])
    if native_path and selected != binding and not (
            fenced and selected == recovery['replacement_binding']):
        raise BoundaryError('the original native conversation reservation changed')
    return {'persisted_binding': persisted, 'registry': registry, 'registry_entry': entry,
            'conversation_record': native}


def replace_failed_start(home, binding, replacement, evidence):
    """Persist the original decision before cutover; every following step is resumable."""
    root = Path(binding['root'])
    prefix = f'.maven/runtime/sessions/{binding["session_id"]}'
    receipt = {
        'schema': 1, 'binding': binding, 'replacement_binding': replacement,
        'persisted_binding': evidence['persisted_binding'], 'registry_entry': evidence['registry_entry'],
        'conversation_record': evidence['conversation_record'],
        'cloud_session_check': {'record_id': binding['session_id'], 'include_archived': True,
                                'status': 'record_unavailable'}}
    existing = _optional(root, prefix + '/failed-start-recovery.json')
    if existing is not None and existing != receipt:
        raise BoundaryError('failed-start replacement intent changed; resume the original transition')
    if existing is None:
        save_private(root, prefix + '/failed-start-recovery.json', receipt)
    return resume_failed_start(home, binding)


def resume_failed_start(home, binding):
    """Complete only the persisted local cutover; caller owns root and registry gates."""
    root = Path(binding['root'])
    prefix = f'.maven/runtime/sessions/{session_uuid(binding["session_id"])}'
    receipt = read_private(root, prefix + '/failed-start-recovery.json')
    replacement = receipt.get('replacement_binding', {})
    snapshot = receipt.get('persisted_binding', {})
    identifier = session_uuid(replacement.get('session_id'))
    if (receipt.get('schema') != 1 or receipt.get('binding') != binding
            or identifier == binding['session_id']
            or binding.get('home') != str(Path(home).resolve())
            or binding.get('env') != str(Path(home).resolve() / '.env')
            or load_project(root)['project_id'] != binding['project_id']
            or any(replacement.get(key) != binding.get(key)
                   for key in ('root', 'project_id', 'home', 'env', 'harness'))
            or any(snapshot.get(key) != value for key, value in binding.items())
            or snapshot.get('initialized') is not False or snapshot.get('resume_required', False) is not False
            or receipt.get('cloud_session_check') != {
                'record_id': binding['session_id'], 'include_archived': True, 'status': 'record_unavailable'}):
        raise BoundaryError('failed-start recovery identity or original proof differs')
    fenced = {**snapshot, 'superseded_by': identifier}
    current = read_private(root, prefix + '/mcp-binding.json')
    if current not in (snapshot, fenced):
        raise BoundaryError('failed-start binding changed after its replacement decision')
    failed_start_evidence(home, binding, recovery=receipt)
    registry = _optional(home, 'runtime/sessions.json')
    entry = registry['sessions'].get(binding['session_id']) if registry else None
    expected = receipt.get('registry_entry')
    if expected is not None and (
            any(expected.get(key) != binding[key] for key in ('root', 'project_id', 'harness', 'harness_session'))
            or expected.get('state') != 'starting' or expected.get('writers') != []
            or expected.get('memory_only') is not True):
        raise BoundaryError('original replacement registration does not prove a quiescent failed start')
    if (registry and str(root) in registry['reservations']
            or entry != expected and not (current == fenced and entry is None)):
        raise BoundaryError('failed-start registration changed after its replacement decision')
    native_path = _conversation_path(binding['harness'], binding['harness_session'])
    native = _optional(home, native_path) if native_path else None
    if native != receipt.get('conversation_record') and not (
            native and native.get('binding') == replacement and current == fenced):
        raise BoundaryError('failed-start native reservation changed after its replacement decision')
    replacement_path = f'.maven/runtime/sessions/{identifier}/mcp-binding.json'
    successor = _optional(root, replacement_path)
    if successor is not None and any(successor.get(key) != value for key, value in replacement.items()):
        raise BoundaryError('replacement binding differs from the original transition')
    if current != fenced:
        save_private(root, prefix + '/mcp-binding.json', fenced)
    if entry is not None:
        del registry['sessions'][binding['session_id']]
        save_private(home, 'runtime/sessions.json', registry)
    if successor is None:
        save_private(root, replacement_path, {**replacement, 'initialized': False, 'resume_required': False})
    bind_conversation(home, replacement)
    return replacement


def bind_conversation(home, binding, *, persisted=None):
    """Check native exclusivity before publishing a new local binding or cloud write."""
    path = _conversation_path(binding['harness'], binding['harness_session'])
    local_path = f'.maven/runtime/sessions/{binding["session_id"]}/mcp-binding.json'
    if path is None:
        if persisted is not None:
            save_private(binding['root'], local_path, persisted)
        return
    with private_lock(home, path + '.lock'):
        previous = conversation_binding(home, harness=binding['harness'], harness_session=binding['harness_session'])
        if previous and previous != binding and _session_state(previous)['status'] != 'closed':
            prefix = f'.maven/runtime/sessions/{previous["session_id"]}'
            previous_binding = read_private(previous['root'], prefix + '/mcp-binding.json')
            recovery = _optional(previous['root'], prefix + '/failed-start-recovery.json')
            if (previous_binding.get('superseded_by') != binding['session_id'] or not recovery
                    or recovery.get('binding') != previous or recovery.get('replacement_binding') != binding):
                raise BoundaryError('this conversation already has an unfinished session; resume its exact binding')
        if persisted is not None:
            save_private(binding['root'], local_path, persisted)
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
            retired = _optional(root, f'.maven/runtime/sessions/{binding["session_id"]}/failed-start-recovery.json')
            if retired:
                result.update(status='recovery', binding=binding,
                              replacement_binding=retired.get('replacement_binding'),
                              issues=[{'kind': 'failed_start_replaced', 'action': 'inspect_exact_replacement'}])
                return result
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
                'limit': min(20, maximum - len(sessions))}
        if cursor:
            body['after_id'] = cursor
        response = client.invoke('/v1/memory/search', body)
        page = response.get('data')
        if response.get('status') != 'ready' or not isinstance(page, dict) or not isinstance(page.get('records'), list):
            raise BoundaryError('project session inspection returned no verified result')
        for summary in page['records']:
            if summary.get('project_id') != project_id or summary.get('kind') != 'session' or summary.get('data_scope') != 'owner':
                raise BoundaryError('session inspection returned a different project or scope')
            # Search returns headers, not session bodies or timestamps. Read the
            # current record so peer state and its revision come from one result.
            response = client.invoke('/v1/memory/read', {'record_id': summary['record_id']})
            record = response.get('data')
            if (response.get('status') != 'ready' or not isinstance(record, dict)
                    or record.get('record_id') != summary['record_id']
                    or record.get('project_id') != project_id or record.get('kind') != 'session'
                    or record.get('data_scope') != 'owner' or not isinstance(record.get('data'), dict)):
                raise BoundaryError('the listed cloud session could not be verified')
            data = record['data']
            try:
                meta = json.loads(data.get('summary', '{}'))
            except (ValueError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            updated = record.get('recorded_at')
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
