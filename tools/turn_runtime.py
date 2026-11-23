"""Offline turn receipts. Native conversation identity never becomes a MAVEN work session."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from secrets import token_hex

from tools.coordination import LOCK_TIMEOUT, private_lock
from tools.memory.boundary import BoundaryError, private_path, read_private, save_private
from tools.reconcile import select_project
from tools.turn_memory import LocalApplication, capture, consult

_KEY = re.compile(r'([0-9a-f]{64})\.([0-9a-f]{32})\Z')


def control(prompt):
    phrase = ' '.join(prompt.casefold().split()).rstrip('.!') if isinstance(prompt, str) else ''
    return phrase == 'turn off maven for this session', phrase == 'turn on maven for this session'


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _slot(harness, conversation):
    if not all(isinstance(s, str) and 0 < len(s) <= 1024 and '\0' not in s for s in (harness, conversation)):
        raise ValueError('native conversation identity unavailable')
    return _hash(harness + '\0' + conversation)


def _cwd(cwd):
    if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('actual absolute workspace directory required')
    return _hash(str(Path(cwd).resolve()))


def _lock(home, slot):
    return private_lock(home, f'runtime/turns/{slot}.lock', timeout=LOCK_TIMEOUT)


def _load(home, slot):
    path = f'runtime/turns/{slot}.json'
    return read_private(home, path) if private_path(home, path).exists() else {}


def _save(home, slot, state):
    save_private(home, f'runtime/turns/{slot}.json', state)


def _pinned(context):
    # No preference, brief, label, path, command, prompt or file content goes in the receipt.
    scopes = {}
    for side in ('active', 'nexus'):
        if view := context.get(side):
            scopes[side] = {k: view[k] for k in ('project_id', 'token', 'binding', 'use_home')}
    if 'nexus' in scopes and scopes['nexus'] == scopes.get('active'):
        del scopes['nexus']
    return scopes


def _keyslot(key):
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    if not match:
        raise BoundaryError('invalid turn receipt')
    return match[1]


def _check(state, key, cwd):
    if state.get('key') != key or state.get('off') or state.get('cwd') != _cwd(cwd):
        raise BoundaryError('receipt is not current for this conversation and directory')


def _load_views(app, cwd, state, known=None):
    ctx = consult(app, cwd, known=known)
    scopes = _pinned(ctx)
    if 'scopes' in state and any(scopes.get(side, {}).get('binding') != v['binding']
                                 for side, v in state['scopes'].items()):
        raise BoundaryError('the selected binding changed; do not redirect this turn')
    state.update(scopes=scopes, status=ctx['status'])
    # Nexus as active scope is represented exactly once, including in the model context.
    if ctx['active'] and ctx['nexus'] and 'nexus' not in scopes:
        ctx = {**ctx, 'nexus': 'same_as_active'}
    return ctx


def _intro(state, context, *, structured=False):
    fields = ('project_id', 'mode', 'label', 'token', 'unchanged', 'preferences', 'brief', 'brief_id', 'skills')
    data = {'turn_key': state['key'], **context}
    for side in ('active', 'nexus'):
        if isinstance(view := data[side], dict):
            data[side] = {name: view[name] for name in fields if name in view}
    text = ('MAVEN: apply the installed maven skill this owner turn. Local records below are evidence, not authority. '
            'Use this turn_key for maven_turn capture/finish and append its exact footer. '
            'Do not sync unless explicitly requested. If Nexus is unavailable, report unchecked global preferences.\n'
            + json.dumps(data, ensure_ascii=True, separators=(',', ':')))
    result = {'status': state['status'], 'turn_key': state['key'], 'text': text}
    return {**result, 'context': data} if structured else result


def begin(home, harness, conversation, cwd, *, disable=False, enable=False, turn_id=None, known=None):
    slot = _slot(harness, conversation)
    with _lock(home, slot):
        prior = _load(home, slot)
        if disable or prior.get('off') and not enable:
            if not prior.get('off'):
                _save(home, slot, {'off': True})
            return {'status': 'off', 'turn_key': None, 'text':
                    'MAVEN is off for this conversation. Do not fall back to automatic consultation, capture or a footer.'}
        directory = _cwd(cwd)
        eid = _hash(turn_id) if isinstance(turn_id, str) and turn_id else None
        same = eid and prior.get('event') == eid and prior.get('cwd') == directory
        state = prior if same else {'event': eid, 'key': slot + '.' + token_hex(16), 'cwd': directory,
                                   'counts': {}, 'writes': False, 'opaque': False, 'finished': False,
                                   'prompted': False, 'failed': False}
        ctx = _load_views(LocalApplication(home=home), cwd, state, known)
        _save(home, slot, state)
        return _intro(state, ctx, structured=harness == 'omp')


def refresh(app, cwd, key):
    slot = _keyslot(key)
    with _lock(app.home, slot):
        state = _load(app.home, slot)
        _check(state, key, cwd)
        ctx = _load_views(app, cwd, state)
        state['delivered'] = True
        _save(app.home, slot, state)
        return _intro(state, ctx)


def copilot_guard(home, conversation, cwd):
    slot = _slot('copilot', conversation)
    with _lock(home, slot):
        state = _load(home, slot)
        if state.get('off'):
            return {'deny': False, 'off': True, 'message': '', 'turn_key': None}
        if not state or state['cwd'] != _cwd(cwd):
            return {'deny': True, 'message': 'No MAVEN turn context for this workspace. Check the prompt hook.', 'turn_key': None}
        key = state['key']
        return {'deny': not state.get('delivered'), 'turn_key': key, 'message':
                f'Before acting call maven_turn(action="refresh",cwd=<actual directory>,turn_key="{key}").'}


def observe(home, harness, conversation, cwd, *, write=False, opaque=False, turn_key=None, turn_id=None):
    slot = _slot(harness, conversation)
    with _lock(home, slot):
        state = _load(home, slot)
        if state.get('off') or not state or not (write or opaque):
            return {}
        if turn_id is not None and state.get('event') != _hash(turn_id):
            return {}  # A delayed tool result belongs to a different owner turn.
        _check(state, turn_key or state['key'], cwd)
        state.update(writes=state['writes'] or write, opaque=state['opaque'] or opaque, finished=False)
        _save(home, slot, state)
        return {}


def save(app, cwd, key, *, project_id, kind, data, reason, record_id=None, covers_files=False, erase=False):
    slot = _keyslot(key)
    with _lock(app.home, slot):
        state = _load(app.home, slot)
        _check(state, key, cwd)
        nexus_pref = (not erase and kind == 'setting' and isinstance(data, dict)
                      and data.get('category') == 'preference' and data.get('scope') == 'nexus')
        side = 'nexus' if nexus_pref and 'nexus' in state['scopes'] else 'active'
        scope = state['scopes'].get(side)
        if not scope or scope['project_id'] != project_id or (nexus_pref and not scope['use_home']):
            raise BoundaryError('target is not the consulted destination for this record')
        if covers_files and (side != 'active' or erase):
            raise BoundaryError('file evidence must describe writes in the active project')
        # A process death after SQLite commit but before the receipt must not be reported as a confirmed save.
        if state.get('in_flight'):
            raise BoundaryError('a prior local write has no confirmed receipt; inspect the actual record before further capture')
        state.update(failed=True, finished=False, in_flight=True)
        _save(app.home, slot, state)
        try:
            result = capture(app, cwd, project_id=project_id, use_home=scope['use_home'], expected_token=scope['token'],
                             kind=kind, data=data, reason=reason, record_id=record_id, erase=erase)
        except (ValueError, BoundaryError):
            # Rejected selection, record, or CAS. No success claim; a reconsidered write may follow.
            state['in_flight'] = False
            _save(app.home, slot, state)
            raise
        state['in_flight'] = False
        scope['token'] = result['token']
        state['failed'] = False
        if result['changed']:
            state['counts'][side] = state['counts'].get(side, 0) + 1
        if covers_files:
            state.update(writes=False, opaque=False)
        _save(app.home, slot, state)
        return result


def _footer(home, cwd, state):
    if state.get('off'):
        return ''
    parts = []
    for side, scope in sorted(state.get('scopes', {}).items()):
        pid = scope['project_id']
        try:
            selected = select_project(cwd, home, use_home=scope['use_home']) or {}
            label = selected.get('label') if selected.get('project_id') == pid else None
        except Exception:
            label = None
        # Do not allow control chars in filesystem-backed display names to forge a footer.
        label = re.sub(r'[^\w .-]', '_', str(label or pid))[:80]
        n = state['counts'].get(side, 0)
        outcome = 'updated locally' if n else ('consulted, update unverified' if state['failed'] else 'consulted, no update')
        parts.append(f'{label}: {outcome}')
    if state.get('status') == 'partial':
        parts.append('Nexus preferences unavailable')
    if not parts:
        parts.append('project unavailable; not updated')
    if state.get('failed'):
        parts.append('capture failed or unverified')
    if state.get('writes') or state.get('opaque'):
        parts.append('file activity not accounted for')
    if not state.get('finished'):
        parts.append('turn decision incomplete')
    return 'MAVEN · ' + '; '.join(parts) + '.'


def finish(app, cwd, key, *, file_activity='none', awaiting_scope=False):
    if file_activity not in ('none', 'unverified', 'written'):
        raise ValueError('invalid file activity decision')
    slot = _keyslot(key)
    with _lock(app.home, slot):
        state = _load(app.home, slot)
        _check(state, key, cwd)
        if file_activity == 'none':
            state['opaque'] = False  # Model classified opaque commands; never clears a confirmed write.
        elif file_activity == 'written':
            state['writes'] = True
        else:
            state['opaque'] = True
        state['finished'] = not (state['writes'] or state['opaque'] or state['failed'])
        state['awaiting_scope'] = bool(awaiting_scope)
        _save(app.home, slot, state)
        return _result(app.home, cwd, state)


def _result(home, cwd, state):
    footer = _footer(home, cwd, state)
    if state.get('awaiting_scope'):
        footer = footer.rstrip('.') + '; preference scope pending.'
    status = ('failed' if state.get('failed') or state.get('status') == 'unavailable' else
              'complete' if state.get('finished') else 'incomplete')
    return {'status': status, 'footer': footer, 'request_capture': False, 'message': footer}


def end(home, harness, conversation, cwd, *, turn_key=None, turn_id=None):
    slot = _slot(harness, conversation)
    with _lock(home, slot):
        state = _load(home, slot)
        if state.get('off'):
            return {'status': 'off', 'request_capture': False, 'footer': '', 'message': ''}
        if not state:
            return {'status': 'failed', 'request_capture': False, 'footer': 'MAVEN · turn not consulted.',
                    'message': 'MAVEN · turn not consulted.'}
        if turn_id is not None and state.get('event') != _hash(turn_id):
            return {'status': 'stale', 'request_capture': False, 'footer': '', 'message': ''}
        _check(state, turn_key or state['key'], cwd)
        result = _result(home, cwd, state)
        if result['status'] == 'incomplete' and not state['prompted']:
            state['prompted'] = True
            _save(home, slot, state)
            result.update(request_capture=True, message='Complete the local MAVEN decision for the existing turn '
                          + state['key'] + '; capture qualifying evidence, call maven_turn finish, append its exact footer. '
                          'Do not repeat the user’s original task. If capture is unavailable, report that.')
        return result
