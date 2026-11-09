"""Durable, session-bound work for harness-owned memory agents.

This module never starts a model. Assignments carry intent; a harness worker reads
current memory and supplies a revision-checked change set. Local acceptance is
not cloud persistence, and a terminated worker is never presumed to have failed.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

from tools.coordination import private_lock, session_uuid
from .boundary import BoundaryError, digest, private_path, read_private, save_private
from .client import DEFINITIVE_MUTATION_ERRORS
from .validation import reject_secrets, source as validate_source, text, validate


class MaintenanceError(BoundaryError):
    """A safe operation-state error, never a record body or credential."""


TERMINAL = {'completed', 'superseded', 'cancelled'}
ORDINARY_KINDS = {'entity', 'fact', 'relationship', 'task', 'decision', 'risk', 'note', 'setting', 'operation'}


def now():
    return datetime.now(timezone.utc).isoformat()


def exact_ref(record):
    return {'record_id': session_uuid(record['record_id']), 'revision': record['revision']}


def unique_refs(refs):
    result = []
    seen = set()
    for ref in refs:
        if set(ref) != {'record_id', 'revision'} or type(ref['revision']) is not int or ref['revision'] < 1:
            raise MaintenanceError('invalid memory reference')
        key = (session_uuid(ref['record_id']), ref['revision'])
        if key not in seen:
            seen.add(key)
            result.append(dict(ref))
    return result


class Maintenance:
    def __init__(self, service):
        self.service = service
        self.root = Path(service.root).resolve()
        self.session_id = session_uuid(service.session_id)
        self.project_id = session_uuid(service.project_id)
        self.runtime = f'.maven/runtime/sessions/{self.session_id}'
        self.path = self.runtime + '/maintenance.json'
        self.lock = self.runtime + '/maintenance.lock'
        self.binding = {'root': str(self.root), 'session_id': self.session_id,
                        'project_id': self.project_id}

    def _load(self):
        if not private_path(self.root, self.path).exists():
            return {'schema': 1, 'binding': self.binding, 'sequence': 0, 'paused': False,
                    'pause_anchor': None, 'catch_up': None, 'assignments': {}, 'checkpoints': {}}
        state = read_private(self.root, self.path)
        if state.get('schema') != 1 or state.get('binding') != self.binding:
            raise MaintenanceError('maintenance state does not match selected session')
        return state

    @contextmanager
    def _transaction(self):
        with private_lock(self.root, self.lock):
            state = self._load()
            yield state
            save_private(self.root, self.path, state)

    @staticmethod
    def _get(state, operation_id):
        item = state['assignments'].get(session_uuid(operation_id))
        if item is None:
            raise MaintenanceError('maintenance assignment unavailable')
        return item

    @staticmethod
    def _worker(item, token):
        if not token or item.get('worker_token') != token:
            raise MaintenanceError('assignment requires its exact worker token')
        if item['state'] in TERMINAL:
            raise MaintenanceError('assignment is no longer active')

    @staticmethod
    def _pinned(state, operation_id):
        return any(operation_id in checkpoint['assignments'] and checkpoint['state'] != 'cancelled'
                   for checkpoint in state['checkpoints'].values())

    @staticmethod
    def _refs(item):
        refs = [effect['record'] for effect in item.get('effects', []) if effect['state'] == 'saved']
        if item.get('completion'):
            refs.append(item['completion'])
        return unique_refs(refs)

    @staticmethod
    def _brief(item):
        return {key: deepcopy(item[key]) for key in
                ('operation_id', 'sequence', 'workstream', 'state', 'automatic', 'summary',
                 'error', 'superseded_by', 'scopes', 'completion') if key in item} | {
                     'saved_refs': Maintenance._refs(item),
                     'pending_effects': sum(effect['state'] not in ('saved', 'superseded')
                                            for effect in item.get('effects', [])),
                     'task_effects': [{'record': effect['record'], 'claim': deepcopy(effect.get('claim')),
                                       'task_state': effect.get('task_state'),
                                       'claim_receipt': deepcopy(effect.get('claim_receipt'))}
                                      for effect in item.get('effects', [])
                                      if effect['state'] == 'saved' and 'task_action' in effect['request']]}

    def status(self, operation_id=None):
        with private_lock(self.root, self.lock):
            state = self._load()
            if operation_id:
                return {'status': 'ready', 'live_state_checked': False,
                        'assignment': deepcopy(self._get(state, operation_id))}
            return {'status': 'paused' if state['paused'] else 'ready',
                    'binding': dict(self.binding), 'live_state_checked': False,
                    'automatic_updates': not state['paused'], 'pause_anchor': state['pause_anchor'],
                    'catch_up': deepcopy(state['catch_up']),
                    'assignments': [self._brief(item) for item in state['assignments'].values()],
                    'checkpoints': [deepcopy(item) for item in state['checkpoints'].values()],
                    'pending_count': sum(item['state'] not in TERMINAL for item in state['assignments'].values())}

    def enqueue(self, intent, *, source, context='', workstream='project', automatic=True,
                operation_id=None, supersede=False):
        text(intent, 16000)
        text(context, 24000, empty=True)
        text(workstream, 160)
        validate_source(source)
        reject_secrets({'intent': intent, 'context': context, 'source': source})
        operation_id = session_uuid(operation_id) if operation_id else str(uuid.uuid4())
        payload = {'intent': intent, 'source': dict(source), 'context': context,
                   'workstream': workstream, 'automatic': bool(automatic)}
        with self._transaction() as state:
            return self._enqueue(state, operation_id, payload, supersede=supersede)

    def _enqueue(self, state, operation_id, payload, *, supersede=False):
        if state.get('closure'):
            raise MaintenanceError('session closure has begun; recover that closure or start a new session')
        if operation_id in state['assignments']:
            existing = state['assignments'][operation_id]
            if existing['intent_digest'] != digest(payload):
                raise MaintenanceError('assignment identity already belongs to different intent')
            return {'status': existing['state'], 'assignment': deepcopy(existing),
                    'cloud_saved': existing['state'] == 'completed'}
        if payload['automatic'] and state['paused']:
            return {'status': 'paused', 'accepted': False, 'cloud_saved': False,
                    'message': 'Automatic memory is paused; no assignment was captured.'}
        if sum(item['state'] not in TERMINAL for item in state['assignments'].values()) >= 128:
            raise MaintenanceError('pending maintenance capacity reached; reconcile existing assignments')
        state['sequence'] += 1
        item = {'operation_id': operation_id, 'sequence': state['sequence'], **payload,
                'intent_digest': digest(payload), 'state': 'queued', 'created_at': now(),
                'effects': [], 'plan_revision': 0, 'scopes': []}
        if supersede:
            for old in state['assignments'].values():
                if (old['workstream'] == payload['workstream'] and old['state'] not in TERMINAL
                        and not self._pinned(state, old['operation_id'])):
                    old['superseded_by'] = operation_id
                    item.setdefault('predecessors', []).append(old['operation_id'])
                    if old['state'] not in ('applying', 'uncertain', 'completing'):
                        old['state'] = 'superseded'
        state['assignments'][operation_id] = item
        return {'status': 'queued', 'accepted': True, 'cloud_saved': False,
                'assignment': deepcopy(item), 'next_action': 'Delegate this bounded assignment to a memory agent.'}

    def claim(self, operation_id, *, worker):
        text(worker, 160)
        # Fresh approved context is a prerequisite to semantic work, not to queuing intent.
        context = self.service.context()
        with self._transaction() as state:
            item = self._get(state, operation_id)
            if state['paused'] and item['automatic']:
                return {'status': 'paused', 'operation_id': operation_id}
            if item['state'] in TERMINAL or item.get('superseded_by'):
                raise MaintenanceError('assignment was completed or superseded')
            if item['state'] in ('applying', 'uncertain', 'completing'):
                raise MaintenanceError('reconcile the original pending effect before claiming work')
            for other in state['assignments'].values():
                if (other['operation_id'] != operation_id and other['workstream'] == item['workstream']
                        and other['sequence'] < item['sequence'] and other['state'] not in TERMINAL):
                    raise MaintenanceError('earlier workstream assignment is still pending')
            if item.get('worker_token') and item.get('worker') != worker:
                raise MaintenanceError('assignment has another worker; explicitly release it after quiescence')
            item.setdefault('worker_token', str(uuid.uuid4()))
            item['worker'] = worker
            if item['state'] == 'queued':
                item['state'] = 'claimed'
            result = deepcopy(item)
        return {'status': 'ready', 'assignment': result, 'context': context,
                'instructions': 'Read current target records, reconcile only this intent, then apply exact revisions and complete. Do not activate instructions.'}

    def release(self, operation_id, *, worker_token, worker_stopped):
        if worker_stopped is not True:
            raise MaintenanceError('releasing a worker requires actual quiescence')
        with self._transaction() as state:
            item = self._get(state, operation_id)
            self._worker(item, worker_token)
            if item['state'] in ('applying', 'uncertain', 'completing'):
                raise MaintenanceError('recover pending effects before releasing a worker')
            item.pop('worker_token', None)
            item.pop('worker', None)
            if item['state'] == 'claimed':
                item['state'] = 'queued'
            return {'status': item['state'], 'operation_id': operation_id}

    def _normalize_changes(self, item, changes, plan_revision, state):
        if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
            raise MaintenanceError('a change set must contain between one and fifty effects')
        effects = []
        reserved = state['assignments'].keys() | state['checkpoints'].keys() | {self.session_id}
        for index, value in enumerate(changes):
            if not isinstance(value, dict):
                raise MaintenanceError('invalid maintenance change shape')
            task_action = value.get('task_action')
            allowed = ({'kind', 'task_action', 'record_id', 'expected_revision', 'scope',
                        'lease_seconds', 'fence', 'request_key', 'reason'} if task_action else
                       {'kind', 'data', 'record_id', 'expected_revision', 'scope', 'tags', 'reason', 'request_key'})
            required = {'kind', 'record_id', 'expected_revision'} if task_action else {'kind', 'data'}
            if not required <= value.keys() or value.keys() - allowed:
                raise MaintenanceError('invalid maintenance change shape')
            if value['kind'] not in ORDINARY_KINDS or task_action and value['kind'] != 'task':
                raise MaintenanceError('managed lifecycle and authority are not ordinary changes')
            scope = value.get('scope', 'project')
            if scope not in ('project', 'universal'):
                raise MaintenanceError('maintenance writes require project or universal scope')
            if value['kind'] == 'operation' and (scope != 'project' or
                    value['data'].get('session_id') != self.session_id):
                raise MaintenanceError('workspace operation must belong to this project session')
            if value['kind'] == 'operation' and value['data'].get('name') == 'memory-maintenance':
                raise MaintenanceError('maintenance completion receipts are controller-owned')
            body = {'kind': value['kind'], 'scope': scope,
                    'record_id': value.get('record_id') or str(uuid.uuid5(uuid.UUID(item['operation_id']),
                                                         f'record:{plan_revision}:{index}')),
                    'expected_revision': value.get('expected_revision', 0),
                    'reason': value.get('reason', 'Reconcile authorized memory assignment'),
                    'source': dict(item['source'])}
            if body['record_id'] in reserved:
                raise MaintenanceError('managed lifecycle identities are reserved')
            if task_action:
                body['task_action'] = task_action
                body.update({key: value[key] for key in ('lease_seconds', 'fence') if value.get(key) is not None})
            else:
                body.update(data=deepcopy(value['data']), tags=list(value.get('tags', [])))
            body['request_key'] = value.get('request_key') or str(uuid.uuid5(uuid.UUID(item['operation_id']), digest(body)))
            if task_action:
                request = {key: body[key] for key in
                           ('record_id', 'expected_revision', 'source', 'request_key', 'lease_seconds', 'fence')
                           if key in body}
                request.update(action=task_action, session_id=self.session_id, change_reason=body['reason'])
                validate('/v1/memory/claim', request)
            else:
                request = {key: body[key] for key in
                           ('kind', 'data', 'record_id', 'expected_revision', 'tags', 'source', 'request_key')}
                request.update(classification='Sensitive', data_scope='owner', change_reason=body['reason'])
                if scope == 'project':
                    request['project_id'] = self.project_id
                validate('/v1/memory/write', request)
            effects.append({'state': 'pending', 'request': body})
        return effects

    def _execute(self, request):
        if 'task_action' not in request:
            return self.service.save(**request)
        arguments = {key: request[key] for key in
                     ('record_id', 'expected_revision', 'scope', 'source', 'request_key', 'lease_seconds', 'fence', 'reason')
                     if key in request}
        return self.service.claim(action=request['task_action'], **arguments)

    @staticmethod
    def _verified_effect(effect, record):
        effect.update(state='saved', record=exact_ref(record))
        if 'task_action' in effect['request']:
            effect['claim'] = deepcopy(record.get('claim'))
            effect['claim_receipt'] = deepcopy(record.get('claim_receipt'))
            effect['task_state'] = record['data']['state']

    def _record_failure(self, operation_id, request, error, *, completion=False, recovering=False):
        code = getattr(error, 'code', 'operation_interrupted')
        definitive = (code in DEFINITIVE_MUTATION_ERRORS and
                      not private_path(self.root, self.runtime + '/pending-memory.json').exists())
        if definitive and recovering:
            failed_path = self.runtime + '/failed-memory-write.json'
            failed = read_private(self.root, failed_path) if private_path(self.root, failed_path).exists() else {}
            # A lookup or read-back rejection does not prove the mutation failed.
            definitive = (failed.get('status') == code and
                          failed.get('request', {}).get('body', {}).get('request_key') == request['request_key'])
        with self._transaction() as state:
            item = self._get(state, operation_id)
            if completion:
                if definitive:
                    item.pop('completion_request', None)
            else:
                effect = next(effect for effect in item['effects']
                              if effect['request']['request_key'] == request['request_key'])
                effect['state'] = 'failed' if definitive else 'uncertain'
            item['state'] = ('superseded' if item.get('superseded_by') else 'blocked') if definitive else 'uncertain'
            item['error'] = code

    def apply(self, operation_id, *, worker_token, changes=None):
        with self._transaction() as state:
            item = self._get(state, operation_id)
            self._worker(item, worker_token)
            if item.get('superseded_by'):
                raise MaintenanceError('newer intent superseded this assignment')
            if state['paused'] and item['automatic']:
                return {'status': 'paused', 'operation_id': operation_id}
            if item['state'] in ('applying', 'uncertain', 'completing'):
                raise MaintenanceError('recover the original pending effect instead of replaying apply')
            if item['state'] == 'blocked':
                raise MaintenanceError('revise the definitively rejected change set before applying')
            if changes is not None:
                changes_digest = digest(changes)
                if item.get('changes_digest') and item['changes_digest'] != changes_digest:
                    raise MaintenanceError('change set already persisted; revise only after a definitive conflict')
                if not item.get('changes_digest'):
                    item['plan_revision'] += 1
                    item['effects'].extend(self._normalize_changes(item, changes, item['plan_revision'], state))
                    item['changes_digest'] = changes_digest
            if not item.get('changes_digest'):
                raise MaintenanceError('a reconciled change set is required')
        while True:
            with self._transaction() as state:
                item = self._get(state, operation_id)
                self._worker(item, worker_token)
                if item.get('superseded_by'):
                    item['state'] = 'superseded'
                    return {'status': 'superseded', 'assignment': self._brief(item)}
                if state['paused'] and item['automatic']:
                    item['state'] = 'claimed'
                    return {'status': 'paused', 'operation_id': operation_id}
                effect = next((effect for effect in item['effects'] if effect['state'] == 'pending'), None)
                if effect is None:
                    if any(effect['state'] in ('uncertain', 'applying', 'failed') for effect in item['effects']):
                        raise MaintenanceError('change set has an unresolved effect')
                    item['state'] = 'applied'
                    return {'status': 'applied', 'cloud_saved': True, 'assignment': self._brief(item)}
                effect['state'] = 'applying'
                item['state'] = 'applying'
                request = deepcopy(effect['request'])
            try:
                record = self._execute(request)
            except Exception as error:
                self._record_failure(operation_id, request, error)
                raise
            with self._transaction() as state:
                item = self._get(state, operation_id)
                effect = next(effect for effect in item['effects']
                              if effect['request']['request_key'] == request['request_key'])
                self._verified_effect(effect, record)
                if request['scope'] not in item['scopes']:
                    item['scopes'].append(request['scope'])
                item['state'] = 'claimed'
                item.pop('error', None)

    def revise(self, operation_id, *, worker_token, changes):
        with self._transaction() as state:
            item = self._get(state, operation_id)
            self._worker(item, worker_token)
            if item.get('changes_digest') == digest(changes) and item['state'] in ('claimed', 'applied'):
                return {'status': item['state'], 'assignment': self._brief(item)}
            if item['state'] != 'applied' and not (item['state'] == 'blocked' and item.get('error') in DEFINITIVE_MUTATION_ERRORS):
                raise MaintenanceError('a new plan requires a fully verified phase or a definitive conflict')
            if private_path(self.root, self.runtime + '/pending-memory.json').exists():
                raise MaintenanceError('resolve the original session journal before revising')
            for effect in item['effects']:
                if effect['state'] in ('pending', 'failed'):
                    effect['state'] = 'superseded'
            item['plan_revision'] += 1
            item['effects'].extend(self._normalize_changes(item, changes, item['plan_revision'], state))
            item['changes_digest'] = digest(changes)
            item['state'] = 'claimed'
            item.pop('error', None)
            return {'status': 'ready', 'assignment': self._brief(item)}

    def complete(self, operation_id, *, worker_token, summary, no_changes=False):
        text(summary, 12000)
        reject_secrets(summary)
        with self._transaction() as state:
            item = self._get(state, operation_id)
            if item['state'] == 'completed':
                return {'status': 'completed', 'cloud_saved': True, 'assignment': self._brief(item)}
            self._worker(item, worker_token)
            if item.get('superseded_by'):
                raise MaintenanceError('a superseded assignment cannot claim completion')
            if item['state'] in ('applying', 'uncertain', 'completing'):
                raise MaintenanceError('reconcile the pending effect before completion')
            if state['paused'] and item['automatic']:
                return {'status': 'paused', 'operation_id': operation_id}
            if any(effect['state'] not in ('saved', 'superseded') for effect in item['effects']):
                raise MaintenanceError('all effects must have verified outcomes before completion')
            if not item['effects'] and not no_changes:
                raise MaintenanceError('explicit no_changes evidence is required for an empty reconciliation')
            if item.get('completion_request') and item['summary'] != summary:
                raise MaintenanceError('completion summary is already bound to its request')
            item['summary'] = summary
            item['state'] = 'completing'
            request = item.setdefault('completion_request', {
                'kind': 'operation', 'record_id': operation_id, 'expected_revision': 0, 'scope': 'project',
                'data': {'session_id': self.session_id, 'name': 'memory-maintenance', 'state': 'completed',
                         'summary': summary}, 'source': dict(item['source']), 'tags': ['memory-maintenance'],
                'reason': 'Verify completed memory reconciliation',
                'request_key': str(uuid.uuid5(uuid.UUID(operation_id), 'completion:' + digest(summary)))})
            request = deepcopy(request)
        try:
            record = self.service.save(**request)
        except Exception as error:
            self._record_failure(operation_id, request, error, completion=True)
            raise
        with self._transaction() as state:
            item = self._get(state, operation_id)
            item['completion'] = exact_ref(record)
            item['state'] = 'completed'
            item['completed_at'] = now()
            item.pop('error', None)
            return {'status': 'completed', 'cloud_saved': True, 'assignment': self._brief(item)}

    def recover(self, operation_id, *, worker_stopped):
        if worker_stopped is not True:
            raise MaintenanceError('recovery requires the actual worker to be stopped')
        with self._transaction() as state:
            item = self._get(state, operation_id)
            if item['state'] not in ('uncertain', 'applying', 'completing'):
                raise MaintenanceError('assignment has no interrupted effect to recover')
            effect = next((effect for effect in item['effects'] if effect['state'] in ('uncertain', 'applying')), None)
            request = deepcopy(effect['request'] if effect else item.get('completion_request'))
            if not request:
                raise MaintenanceError('original request is unavailable')
        # The service reconciles its journal first. If interruption happened before
        # submission, the persisted assignment still supplies exactly the original key.
        try:
            self.service.recover()
            record = self._execute(request)
        except Exception as error:
            self._record_failure(operation_id, request, error, completion=effect is None, recovering=True)
            raise
        with self._transaction() as state:
            item = self._get(state, operation_id)
            if effect is None:
                item.update(completion=exact_ref(record), state='completed', completed_at=now())
            else:
                current = next(value for value in item['effects']
                               if value['request']['request_key'] == request['request_key'])
                self._verified_effect(current, record)
                if request['scope'] not in item['scopes']:
                    item['scopes'].append(request['scope'])
                item['state'] = 'superseded' if item.get('superseded_by') else 'claimed'
            item.pop('error', None)
            return {'status': item['state'], 'cloud_saved': True, 'assignment': self._brief(item)}

    def save(self, kind, data, *, source, scope='project', record_id=None,
             expected_revision=0, tags=(), reason='Explicitly save memory', operation_id=None, request_key=None):
        intent = {'kind': kind, 'data': data, 'scope': scope, 'record_id': record_id,
                  'expected_revision': expected_revision, 'tags': list(tags), 'request_key': request_key}
        queued = self.enqueue(reason, source=source, context='Explicit save digest: ' + digest(intent),
                              workstream='record:' + (record_id or 'new'), automatic=False,
                              operation_id=operation_id, supersede=False)
        operation_id = queued['assignment']['operation_id']
        if queued['assignment']['state'] == 'completed':
            return {'status': 'saved', 'cloud_saved': True, 'assignment': self._brief(queued['assignment'])}
        claimed = self.claim(operation_id, worker='explicit-save')
        token = claimed['assignment']['worker_token']
        change = {'kind': kind, 'data': data, 'scope': scope, 'expected_revision': expected_revision,
                  'tags': list(tags), 'reason': reason}
        if record_id:
            change['record_id'] = record_id
        if request_key is not None:
            change['request_key'] = request_key
        self.apply(operation_id, worker_token=token, changes=[change])
        result = self.complete(operation_id, worker_token=token, summary=reason)
        result['status'] = 'saved'
        return result

    def claim_task(self, record_id, *, action, expected_revision, source,
                   lease_seconds=None, fence=None, request_key=None, operation_id=None, reason=None):
        target = self.service.read_record(record_id, revision=expected_revision)
        scope = 'universal' if target.get('project_id') is None else 'project'
        reason = reason or 'Task ' + action
        change = {'kind': 'task', 'task_action': action, 'record_id': record_id,
                  'expected_revision': expected_revision, 'scope': scope, 'reason': reason}
        change.update({key: value for key, value in
                       (('lease_seconds', lease_seconds), ('fence', fence), ('request_key', request_key))
                       if value is not None})
        queued = self.enqueue(reason, source=source, context='Task action digest: ' + digest(change),
                              workstream='record:' + record_id, automatic=False,
                              operation_id=operation_id, supersede=False)
        identifier = queued['assignment']['operation_id']
        if queued['assignment']['state'] != 'completed':
            token = self.claim(identifier, worker='explicit-task')['assignment']['worker_token']
            self.apply(identifier, worker_token=token, changes=[change])
            self.complete(identifier, worker_token=token, summary=reason)
        item = self.status(identifier)['assignment']
        effect = next(effect for effect in item['effects'] if effect['state'] == 'saved')
        return {'status': 'saved', 'cloud_saved': True, 'assignment': self._brief(item),
                'record': effect['record'], 'claim': effect['claim'], 'task_state': effect['task_state'],
                'claim_receipt': effect.get('claim_receipt')}

    def pause(self, *, anchor=None):
        if anchor is not None:
            text(anchor, 2000)
        with self._transaction() as state:
            if not state['paused']:
                state['catch_up'] = None
                state['pause_anchor'] = anchor
                state['paused_at'] = now()
            state['paused'] = True
            return {'status': 'paused', 'automatic_updates': False,
                    'message': 'MAVEN: automatic memory updates paused. Submitted writes may finish; explicit operations remain available.',
                    'pause_anchor': state['pause_anchor'], 'pending_count': sum(
                        item['state'] not in TERMINAL for item in state['assignments'].values())}

    def resume(self, *, fresh=False, discussion=None, source=None, context_complete=False):
        if discussion is not None:
            text(discussion, 24000, empty=True)
            reject_secrets({'discussion': discussion, 'source': source})
        with self._transaction() as state:
            anchor = state['pause_anchor']
            if not state['paused']:
                return {'status': 'ready', 'automatic_updates': True, 'catch_up': deepcopy(state['catch_up'])}
            if fresh:
                state.update(paused=False, catch_up=None, pause_anchor=None)
                return {'status': 'ready', 'automatic_updates': True, 'catch_up': None,
                        'message': 'MAVEN: automatic memory resumed fresh; paused discussion was not reviewed.'}
            if discussion is None:
                state['catch_up'] = {'state': 'context_required', 'pause_anchor': anchor}
                return {'status': 'context_required', 'automatic_updates': False,
                        'message': 'Retrieve the available paused discussion, then resume with that evidence or explicitly resume fresh.',
                        'pause_anchor': anchor}
            if source is None:
                raise MaintenanceError('catch-up requires actual discussion provenance')
            validate_source(source)
            operation_id = str(uuid.uuid4())
            result = self._enqueue(state, operation_id, {
                'intent': 'Review available paused discussion and reconcile relevant authorized memory changes.',
                'source': dict(source), 'context': discussion, 'automatic': False, 'workstream': 'resume-catch-up'})
            state['paused'] = False
            state['catch_up'] = {'state': 'queued', 'operation_id': operation_id,
                                 'context_complete': bool(context_complete), 'pause_anchor': anchor}
            result.update(automatic_updates=True, context_complete=bool(context_complete),
                          message='MAVEN: memory resumed; catch-up queued.' if context_complete else
                          'MAVEN: memory resumed; catch-up covers available discussion only. Missing history is not reconstructed.')
            return result

    def checkpoint(self, *, summary, refs=(), checkpoint_id=None, wait=False, wait_seconds=20):
        text(summary, 12000)
        reject_secrets(summary)
        refs = unique_refs(refs)
        checkpoint_id = session_uuid(checkpoint_id) if checkpoint_id else str(uuid.uuid4())
        with self._transaction() as state:
            existing = state['checkpoints'].get(checkpoint_id)
            if existing:
                if existing['summary'] != summary or existing['explicit_refs'] != refs:
                    raise MaintenanceError('checkpoint identity is bound to a different cutoff or summary')
            else:
                base = max((value for value in state['checkpoints'].values() if value['state'] == 'saved'),
                           key=lambda value: value['cutoff'], default=None)
                base_cutoff = base['cutoff'] if base else 0
                state['checkpoints'][checkpoint_id] = {
                    'checkpoint_id': checkpoint_id, 'cutoff': state['sequence'], 'state': 'preparing',
                    'summary': summary, 'explicit_refs': refs, 'base': deepcopy(base['receipt']) if base else None,
                    'assignments': [item['operation_id'] for item in state['assignments'].values()
                                    if item['sequence'] > base_cutoff], 'created_at': now()}
        deadline = time.monotonic() + min(max(float(wait_seconds), 0), 120)
        while True:
            result = self.advance_checkpoint(checkpoint_id)
            if result['status'] != 'pending' or not wait or time.monotonic() >= deadline:
                result['wait_requested'] = bool(wait)
                return result
            # Waiting is explicit, bounded, and does not hold the operation lock.
            # The harness must run semantic workers; this loop never starts a model.
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def advance_checkpoint(self, checkpoint_id):
        checkpoint_id = session_uuid(checkpoint_id)
        with self._transaction() as state:
            checkpoint = state['checkpoints'].get(checkpoint_id)
            if checkpoint is None:
                raise MaintenanceError('checkpoint unavailable')
            if checkpoint['state'] == 'saved':
                return {'status': 'checkpointed', 'cloud_saved': True, 'checkpoint': deepcopy(checkpoint),
                        'memory_summary': self._summary(state, cutoff=checkpoint['cutoff'])}
            if checkpoint['state'] == 'applying':
                return {'status': 'uncertain', 'checkpoint': deepcopy(checkpoint),
                        'next_action': 'Stop the original worker and explicitly recover this checkpoint.'}
            pending = [identifier for identifier in checkpoint['assignments']
                       if state['assignments'][identifier]['state'] not in TERMINAL]
            if pending:
                return {'status': 'pending', 'cloud_saved': False, 'checkpoint': deepcopy(checkpoint),
                        'pending_assignments': pending,
                        'next_action': 'Complete the included assignments, then advance this exact checkpoint.'}
            refs = unique_refs(([checkpoint['base']] if checkpoint.get('base') else []) +
                               checkpoint['explicit_refs'] + [ref for identifier in checkpoint['assignments']
                               for ref in self._refs(state['assignments'][identifier])])
            checkpoint['refs'] = refs
            checkpoint['state'] = 'applying'
            request = {'checkpoint_id': checkpoint_id, 'summary': checkpoint['summary'],
                       'refs': refs, 'cutoff': checkpoint['cutoff']}
        result = self._persist_checkpoint(**request)
        with self._transaction() as state:
            checkpoint = state['checkpoints'][checkpoint_id]
            checkpoint.update(state='saved', receipt=result['checkpoint'], completed_at=now())
            return {'status': 'checkpointed', 'cloud_saved': True, 'checkpoint': deepcopy(checkpoint),
                    'memory_summary': self._summary(state, cutoff=checkpoint['cutoff'])}

    def recover_checkpoint(self, checkpoint_id, *, worker_stopped):
        if worker_stopped is not True:
            raise MaintenanceError('checkpoint recovery requires its original worker to be stopped')
        checkpoint_id = session_uuid(checkpoint_id)
        with private_lock(self.root, self.lock):
            checkpoint = deepcopy(self._load()['checkpoints'].get(checkpoint_id))
            if checkpoint is None or checkpoint['state'] != 'applying':
                raise MaintenanceError('checkpoint has no interrupted cloud operation')
        self.service.recover()
        result = self._persist_checkpoint(checkpoint_id=checkpoint_id, summary=checkpoint['summary'],
                                          refs=checkpoint['refs'], cutoff=checkpoint['cutoff'])
        with self._transaction() as state:
            saved = state['checkpoints'][checkpoint_id]
            saved.update(state='saved', receipt=result['checkpoint'], completed_at=now())
            return {'status': 'checkpointed', 'cloud_saved': True, 'checkpoint': deepcopy(saved),
                    'memory_summary': self._summary(state, cutoff=saved['cutoff'])}

    def _persist_checkpoint(self, *, checkpoint_id, summary, refs, cutoff):
        # Each cloud record is bounded. A deterministic chain retains every pinned
        # version, including large reconciliations, without exceeding that contract.
        segments = [refs[index:index + 99] for index in range(0, len(refs), 99)] or [[]]
        previous = None
        for index, segment in enumerate(segments):
            final = index == len(segments) - 1
            identifier = checkpoint_id if final else str(uuid.uuid5(uuid.UUID(checkpoint_id), f'manifest:{index}'))
            result = self.service.checkpoint(
                checkpoint_id=identifier, summary=summary if final else f'Manifest segment {index + 1}: {summary}',
                refs=([previous] if previous else []) + segment, cutoff=cutoff)
            if result.get('status') != 'checkpointed' or not result.get('checkpoint'):
                raise MaintenanceError('cloud checkpoint has no verified completion')
            previous = result['checkpoint']
        return result

    @staticmethod
    def _summary(state, *, cutoff=None):
        groups = {'universal': [], 'project': [], 'pending': []}
        for item in state['assignments'].values():
            if cutoff is not None and item['sequence'] > cutoff:
                continue
            if item['state'] not in TERMINAL:
                groups['pending'].append(Maintenance._brief(item))
            for effect in item.get('effects', []):
                if effect['state'] == 'saved':
                    groups[effect['request']['scope']].append({**effect['record'],
                        'kind': effect['request']['kind'], 'reason': effect['request']['reason'],
                        'operation_id': item['operation_id']})
        return groups

    def summary(self):
        with private_lock(self.root, self.lock):
            return {'status': 'ready', 'live_state_checked': False, 'memory_summary': self._summary(self._load())}

    def finish(self, checkpoint_id, *, recover_only=False):
        with self._transaction() as state:
            if recover_only and not state.get('closure'):
                raise MaintenanceError('session closure recovery requires the original persisted closure')
            checkpoint = state['checkpoints'].get(session_uuid(checkpoint_id))
            if checkpoint is None or checkpoint['state'] != 'saved':
                raise MaintenanceError('session closure requires a verified memory checkpoint')
            if checkpoint['cutoff'] != state['sequence']:
                raise MaintenanceError('new work exists after this checkpoint; checkpoint it before closure')
            if any(item['state'] not in TERMINAL for item in state['assignments'].values()):
                raise MaintenanceError('unresolved maintenance prevents session closure')
            receipt, summary = deepcopy(checkpoint['receipt']), checkpoint['summary']
            closure = {'checkpoint_id': checkpoint_id, 'receipt': receipt, 'summary': summary}
            if state.get('closure') and state['closure'] != closure:
                raise MaintenanceError('recover the original session closure before selecting another checkpoint')
            state['closure'] = closure
        result = self.service.finish(summary=summary, checkpoint_ref=receipt)
        return {**result, 'memory_summary': self.summary()['memory_summary']}

    def classification_plan(self, record_id, *, scope, reason):
        """Read current source/target revisions for an agent's guarded change set."""
        from .client import MemoryClientError
        if scope not in ('project', 'universal'):
            raise MaintenanceError('scope must be project or universal')
        text(reason, 240)
        original = self.service.read_record(record_id, scope='all')
        if original['kind'] not in ORDINARY_KINDS or original['kind'] == 'operation':
            raise MaintenanceError('managed lifecycle records cannot be reclassified')
        original_scope = 'universal' if original.get('project_id') is None else 'project'
        if original_scope == scope and (scope == 'universal' or original.get('project_id') == self.project_id):
            return {'status': 'unchanged', 'source_record': exact_ref(original), 'scope': scope, 'changes': []}
        target_id = str(uuid.uuid5(uuid.UUID(record_id),
                                   'maven:scope:' + (self.project_id if scope == 'project' else 'universal')))
        try:
            target = self.service.read_record(target_id, scope='all')
        except MemoryClientError as error:
            if error.code != 'record_unavailable':
                raise
            target = None
        kind = 'note' if original['kind'] == 'task' else original['kind']
        if target is not None and target['kind'] != kind:
            raise MaintenanceError('scope representation has a different record kind')
        tags = [tag for tag in original.get('tags', [])
                if tag != 'maven-scope-superseded' and not tag.startswith('maven-origin')]
        tags.extend(['maven-origin:' + record_id, 'maven-origin-revision:' + str(original['revision'])])
        if len(tags) > 20:
            raise MaintenanceError('source tags leave no room for scope provenance; reconcile tags before reclassification')
        data = deepcopy(original['data'])
        if original['kind'] == 'setting':
            data['scope'] = 'global' if scope == 'universal' else 'project'
        elif original['kind'] == 'task':
            # Moving visibility must not clone task ownership or a completed task's
            # claim-fenced state. The original task remains the actionable record.
            data = {'title': data['title'], 'category': 'scoped-task-reference',
                    'body_markdown': f"Source task: {record_id}, revision {original['revision']}, "
                                     f"project {original.get('project_id') or 'universal'}.\n"
                                     f"Observed state: {data['state']}.\n\n{data['description']}"}
        changes = [{'kind': kind, 'data': data, 'record_id': target_id,
                    'expected_revision': target['revision'] if target else 0,
                    'scope': scope, 'tags': tags, 'reason': reason}]
        if original_scope == 'universal' and scope == 'project':
            retired_tags = list(dict.fromkeys([*original.get('tags', []), 'maven-scope-superseded']))
            if len(retired_tags) > 20:
                raise MaintenanceError('source tags leave no room for retirement; reconcile tags before reclassification')
            changes.append({'kind': original['kind'], 'data': original['data'], 'record_id': record_id,
                            'expected_revision': original['revision'], 'scope': 'universal',
                            'tags': retired_tags, 'reason': 'Correct universal visibility: ' + reason[:200]})
        return {'status': 'ready', 'source_record': exact_ref(original), 'scope': scope, 'changes': changes}

    def reclassify(self, record_id, *, scope, source, reason, operation_id=None):
        session_uuid(record_id)
        if scope not in ('project', 'universal'):
            raise MaintenanceError('scope must be project or universal')
        queued = self.enqueue(reason, source=source, context=f'Source record {record_id}; target scope {scope}',
                              workstream='scope:' + record_id, automatic=False,
                              operation_id=operation_id, supersede=False)
        operation_id = queued['assignment']['operation_id']
        if queued['assignment']['state'] == 'completed':
            return {'status': 'saved', 'cloud_saved': True, 'assignment': self._brief(queued['assignment'])}
        plan = None if queued['assignment'].get('changes_digest') else self.classification_plan(
            record_id, scope=scope, reason=reason)
        claimed = self.claim(operation_id, worker='scope-classification')
        token = claimed['assignment']['worker_token']
        no_changes = plan is not None and not plan['changes']
        if not no_changes:
            self.apply(operation_id, worker_token=token, changes=plan['changes'] if plan else None)
        result = self.complete(operation_id, worker_token=token, summary=reason, no_changes=no_changes)
        return {**result, 'status': 'saved', 'source_record_id': record_id, 'scope': scope}
