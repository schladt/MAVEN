"""Owner-reviewed redaction. Only validated control data enters the administration journal."""
from __future__ import annotations

import re

from tools.administration import (_canonical_uuid, _owner_phrase, _redaction_targets, _REDACTION_INVENTORIES,
                                  _PROVIDER_KINDS)
from tools.memory import admin
from tools.setup_common import SetupPending

_OBJECT_KINDS = ('skill_bundle', 'recovery_object')
_EFFECTS = {
    'retry_objects': 'Resume only the immutable object purge approved for this original SQL job. No new '
                     'targets or versions may be added. This does not repeat the database redaction.',
    'redact': 'Permanently redact this exact selection and its listed dependents; revoke affected authority. '
              'Purge only reviewed provider versions, markers and incomplete uploads. Selecting a previously '
              'unclassified object destroys the entire object. A control-history purge preserves the current '
              'recovery fence. Other secrets require separate approval. The restore ledger '
              'must be reapplied before an isolated backup may serve. Offline replicas remain unverified.',
    'purge_provider': 'Separately and irreversibly delete the selected provider material in this installation’s '
                      'shared secret namespace. All reviewed versions and all projects using them are affected. '
                      'This is not catalog redaction. Revoke or rotate at the issuer separately; stop ALL writers '
                      '(including older and direct clients) before review and until deletion is verified. '
                      'No wrapping key, plaintext export, or unrelated provider entry is deleted.',
    'restore_redactions': 'Replay the complete independent erasure ledger onto this isolated physical destination '
                         'at its new recovery epoch, verify the result, and release only that verified checkpoint. '
                         'A missing, stale or changed ledger must not release normal service.'}


def stop(state, error):
    """Public status codes, never provider diagnostics, request text, ARNs, SQL errors or locators."""
    from tools.memory.redaction_objects import ObjectRedactionError, SAFE_CODES
    from tools.memory.validation import MemoryError
    from tools.memory.client import MemoryClientError
    uncertain = bool(state.get('effect_started'))
    code = 'redaction_outcome_unverified' if uncertain else 'redaction_review_blocked'
    if isinstance(error, ObjectRedactionError) and error.code in SAFE_CODES:
        code = error.code
    elif isinstance(error, (MemoryError, MemoryClientError)):
        supplied = getattr(error, 'status', None) or getattr(error, 'code', None)
        if supplied in ('redaction_scope_changed', 'redaction_ledger_unverified', 'context_stale',
                        'not_authorized', 'temporarily_unavailable', 'cleanup_refused', 'invalid_request'):
            code = supplied
    recovery = 'Inspect the saved outcomes; resolve the blocker and resume only this exact operation.'
    if code == 'provider_locator_required':
        recovery = 'Use the private owner terminal with --operation ' + state['operation_id'] + ' --private-selector ssm.'
    if code == 'redaction_scope_changed':
        recovery = 'The reviewed scope changed. Nothing new was authorized. A fresh review is required.'
    if state['step'] == 'redaction_sweep':
        code, uncertain = 'known_local_sweep_blocked', False
    if code in ('redaction_scope_changed', 'not_authorized', 'invalid_request', 'cleanup_refused'):
        uncertain = False
    state.update(state='uncertain' if uncertain else 'blocked', error=code, recovery=recovery)


def _require(condition):
    if not condition:
        raise SetupPending('Invalid redaction control data; no unvalidated evidence was saved.')


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _pins(values):
    return isinstance(values, list) and all(_hex(value) for value in values) and len(set(values)) == len(values)


def _count(value):
    return type(value) is int and value >= 0


def _bindings(config):
    result = {key: config[key] for key in ('instance_id', 'recovery_epoch')}
    _require(all(_canonical_uuid(value) for value in result.values()))
    return result


def _object_selection(row):
    return row['kind'] in _OBJECT_KINDS and _canonical_uuid(row['id'])


def _object_plan(plan):
    _require(set(plan) == {'format', 'scope', 'objects'} and type(plan['format']) is int and plan['format'] == 1
             and _canonical_uuid(plan['scope']) and isinstance(plan['objects'], list))
    for row in plan['objects']:
        _require(set(row) - {'blocked'} == {'kind', 'id', 'class', 'versions', 'preserve'} and _object_selection(row)
                 and ('blocked' not in row or row['blocked'] in ('object_retained', 'replication_unverified'))
                 and row['class'] in ('bundle', 'retirement_manifest', 'control_history', 'orphan')
                 and _pins(row['versions']) and (row['preserve'] is None or _hex(row['preserve'])))


def _validate_review(action, plan, binding, selected):
    _require(all(plan[key] == value for key, value in binding.items()))
    if action == 'restore_redactions':
        _require(set(plan) == {*binding, 'source_epoch', 'source_cluster', 'destination_cluster', 'checkpoint', 'sequence'}
                 and _canonical_uuid(plan['source_epoch']) and _count(plan['sequence'])
                 and all(_hex(plan[key]) for key in ('source_cluster', 'destination_cluster', 'checkpoint')))
    elif action == 'retry_objects':
        _require(set(plan) == {*binding, 'job_id', 'objects'} and _canonical_uuid(selected) and plan['job_id'] == selected)
        _object_plan(plan['objects'])
    elif action == 'purge_provider':
        _require(set(plan) == {*binding, 'provider'} and isinstance(plan['provider'], dict))
        p = plan['provider']
        _require(set(p) == {'format', 'scope', 'kind', 'id', 'versions'} and type(p['format']) is int
                 and p['format'] == 1 and _canonical_uuid(p['scope']) and _pins(p['versions'])
                 and selected == [{'kind': p['kind'], 'id': p['id']}])
    else:
        _require(set(plan) == {*binding, 'targets', 'permanent_targets', 'revisions', 'bundles', 'plan_digest', 'objects'}
                 and _hex(plan['plan_digest']) and isinstance(plan['bundles'], list)
                 and all(_canonical_uuid(item) for item in plan['bundles'])
                 and all(isinstance(plan[key], list) for key in ('targets', 'permanent_targets', 'revisions')))
        for key in ('targets', 'permanent_targets'):
            for target in plan[key]:
                _redaction_targets([target])
        identity = lambda target: (target['kind'], target.get('project_id', ''), target['id'])
        _require(sorted(map(identity, plan['targets'])) == sorted(map(identity, selected)))
        for row in plan['revisions']:
            _require(_hex(row['revision_token']))
            _redaction_targets([{k: v for k, v in row.items() if k != 'revision_token'}])
        _object_plan(plan['objects'])


def _store(values, session):
    from tools.aws.secret_store import SecretStore
    _require(bool(session))  # Do not allow SecretStore's ordinary daily credential fallback.
    return SecretStore(values, session=session)


def private_target(controller, kind, locator):
    """Terminal-only resolution; never put a locator in begin/respond or the operation record."""
    from tools.memory import redaction_objects
    values, config, session = controller._cleanup_admin()
    salt = admin.redaction_salt(config, session)
    if kind in _PROVIDER_KINDS:
        target = _store(values, session).redaction_target(kind, locator, salt)
    else:
        _require(kind in _OBJECT_KINDS)
        target = redaction_objects.target(config, salt, kind, locator)
    _redaction_targets([target], provider=kind in _PROVIDER_KINDS)
    _require(target['kind'] == kind)
    return target


def _objects_result(result, plan):
    if result.get('status') == 'blocked':
        # The backend owns the public reason vocabulary. Never relay an SDK message or locator.
        from tools.memory.redaction_objects import SAFE_CODES
        code = result.get('code')
        return {'status': 'blocked', 'code': code if isinstance(code, str) and code in SAFE_CODES else 'provider_unverified'}
    _require(set(result) == {'status', 'objects'} and result['status'] == 'objects_verified_purged'
             and isinstance(result['objects'], list))
    expected = {(row['kind'], row['id']) for row in plan['objects']}
    for row in result['objects']:
        _require(set(row) == {'kind', 'id', 'versions'} and _object_selection(row) and _count(row['versions']))
        expected.remove((row['kind'], row['id']))
    _require(not expected)
    return result


def advance(controller, state):
    action = state['parameters']['action']
    selected = state['parameters'].get('targets', state['parameters'].get('job_id'))
    operation = state['operation_id']
    values, config, session = controller._cleanup_admin()
    binding = _bindings(config)
    if state['step'] == 'start':
        if action in _REDACTION_INVENTORIES:
            if action == 'redaction_pending':
                jobs = admin.redaction_pending(config, session)
                _require(isinstance(jobs, list))
                for entry in jobs:
                    _require(set(entry) == {'job_id', 'objects'} and _canonical_uuid(entry['job_id'])
                             and isinstance(entry['objects'], list))
                    for row in entry['objects']:
                        _require(set(row) == {'kind', 'id'} and _object_selection(row))
                state.update(state='completed', outcome={**binding, 'read_only': True, 'jobs': jobs})
                return
            provider = action == 'provider_candidates'
            after = None
            if provider:
                candidates = _store(values, session).redaction_candidates(admin.redaction_salt(config, session))
            else:
                page = admin.redaction_candidates(config, session, state['parameters'].get('after'))
                _require(set(page) == {'candidates', 'next_after'})
                candidates, after = page['candidates'], page['next_after']
                if after is not None:
                    _redaction_targets([after])
                    _require(candidates and after == candidates[-1])
            _require(isinstance(candidates, list))
            for target in candidates:
                _redaction_targets([target], provider=provider)
            outcome = {**binding, 'read_only': True, 'candidates': candidates}
            if not provider:
                outcome['next_after'] = after
                from tools.memory.redaction_objects import recovery_inventory
                counts = recovery_inventory(config, session)
                _require(isinstance(counts, dict) and set(counts) <= {
                    'protected_ledger', 'control_history', 'retirement_manifest', 'unclassified',
                    'foreign_or_malformed_manifest', 'orphan'} and all(_count(value) for value in counts.values()))
                outcome['recovery_inventory'] = counts
            state.update(state='completed', outcome=outcome)
            return
        if action == 'redact':
            plan = admin.review_redaction(config, session, selected)
        elif action == 'retry_objects':
            plan = {**binding, 'job_id': selected, 'objects': admin.review_redaction_objects(config, session, selected)}
        elif action == 'restore_redactions':
            plan = admin.redaction_restore_review(config, session)
        else:
            plan = {**binding, 'provider': _store(values, session).review_redaction(
                selected[0], admin.redaction_salt(config, session))}
        _validate_review(action, plan, binding, selected)
        review = {'action': action, 'effect': _EFFECTS[action], 'candidate': plan}
        confirmations = ({'issuer_revoked': 'ISSUER REVOKED', 'writers_quiesced': 'WRITERS QUIESCED'}
                         if action == 'purge_provider' else {})
        controller._review(state, 'redaction_review', 'Review ' + action, review, 'redaction_apply', confirmations)
        question = state['pending_interaction']
        question['owner_phrase'] = _owner_phrase(operation, question)
        question['required_authorization'] = ('Relay the complete review to the owner. Wait for the owner to return '
            'the exact displayed owner_phrase; use source.origin owner_chat. The terminal records its own input. '
            'Never quote prose or locators.')
        question['description'] = _EFFECTS[action]
        return
    review = state['approvals'][-1]['review']
    controller._approved(state, 'redaction_review', review)
    _require(review['action'] == action)
    plan = review['candidate']
    _validate_review(action, plan, binding, selected)
    if state['step'] == 'redaction_apply':
        if action == 'purge_provider':
            attest = state['answers']['redaction_review']['confirmations']
            _require(attest == {'issuer_revoked': 'ISSUER REVOKED', 'writers_quiesced': 'WRITERS QUIESCED'})
            options = {'issuer_revoked': True, 'writers_quiesced': True}
            private = controller._private_provider_retry
            if private is not None:
                _require(private[0] == operation and selected[0]['kind'] == 'ssm')
                options['private_name'] = private[1]
            result = _store(values, session).apply_redaction(plan['provider'], admin.redaction_salt(config, session), **options)
            _require(set(result) == {'status', 'kind', 'id', 'versions', 'issuer_revocation'}
                     and result['status'] == 'provider_deletion_verified' and result['issuer_revocation'] == 'owner_attested'
                     and _count(result['versions']) and all(result[k] == v for k, v in selected[0].items()))
        elif action == 'retry_objects':
            result = _objects_result(admin.finish_redaction_objects(config, session, selected), plan['objects'])
            if result['status'] == 'blocked':
                state.update(state='blocked', error=result['code'], outcome={'job_id': selected, 'objects': result})
                return
            result = {'job_id': selected, 'objects': result}
        elif action == 'restore_redactions':
            result = admin.redaction_restore_apply(config, session, plan)
            _require(set(result) == {*binding, 'status', 'checkpoint'} and _hex(result['checkpoint'])
                     and result['status'] == 'restore_verified' and all(result[k] == v for k, v in binding.items()))
        else:
            result = admin.apply_redaction(config, session, operation_id=operation, review=plan)
            _require(set(result) == {'job_id', 'live_data_redacted', 'objects', 'backup_retention_pending', 'offline_copies_unverified'}
                     and result['job_id'] == operation and result['live_data_redacted'] is True
                     and result['backup_retention_pending'] is True and result['offline_copies_unverified'] is True)
            result = {**result, 'objects': _objects_result(result['objects'], plan['objects'])}
        if action != 'redact':
            controller._receipt(state, action, result)
            state.update(state='completed', outcome=result)
            return
        state.update(step='redaction_sweep', outcome={**result, 'known_local_sweep_verified': False})
        controller._save(state)  # Keep confirmed live SQL and object outcomes even if local sweep is blocked.
    _require(action == 'redact' and state['step'] == 'redaction_sweep')
    from tools.reconcile_redaction import Replica, sweep_reviewed
    from tools.memory import redaction_objects
    salt, handles = None, None

    def bundle_check(shas):
        nonlocal salt, handles
        if handles is None:
            # sweep_reviewed already holds the replica lock and has recorded this job. An old
            # archive might belong to any previously delivered marker, not just today's plan.
            salt = admin.redaction_salt(config, session)
            handles = {row['id'] for row in Replica(controller.home, binding['instance_id']).targets()
                       if row['kind'] == 'skill_bundle'}
        return {i for i, sha in enumerate(shas)
                if redaction_objects.target(config, salt, 'skill_bundle', sha)['id'] in handles}

    sweep_reviewed(controller.home, binding['instance_id'], plan['permanent_targets'],
                   current_operation=operation, bundle_check=bundle_check)
    state['outcome']['known_local_sweep_verified'] = True
    if state['outcome']['objects']['status'] == 'blocked':
        state.update(state='blocked', step='redaction_apply', error=state['outcome']['objects']['code'],
                     recovery='A provider purge is unverified. Correct the blocker and resume this exact job.')
    else:
        state.update(state='completed')
