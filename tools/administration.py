"""Durable administration checkpoints shared by conversation and terminal presenters.

Status is local observation. Only advance executes a named backend phase; responses
never replay a Python stack. Approval evidence is the presenter's supplied owner
statement, bound to the complete persisted review, not an invented terminal click.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from uuid import UUID, uuid4, uuid5

from tools.interaction import Field
from tools.memory.redaction_ledger import KINDS as _REDACTION_KINDS
from tools.setup_common import SetupPending, private_directory, read_private, save_json, setup_lock

HOME_TAG = 'maven-home'
_REDACTION_INVENTORIES = ('redaction_candidates', 'provider_candidates', 'redaction_pending')
_REDACTION_ACTIONS = ('redact', 'purge_provider', 'restore_redactions', 'retry_objects', *_REDACTION_INVENTORIES)
CLEANUP_ACTIONS = ('review', 'forget', 'restore', 'erase', *_REDACTION_ACTIONS)
_PROVIDER_KINDS = frozenset(('ssm', 'encrypted-file'))
CLEANUP_LIMIT = 200
_LIFECYCLE_KINDS = ('session', 'checkpoint', 'operation')
_DEPLOY_KEYS = frozenset('MAVEN_DEPLOY_' + key for key in (
    'MODE', 'ROLE_ARN', 'PROFILE', 'LOGIN_CACHE', 'EXPECTED_PRINCIPAL'))
# What the SQL keeps of a memory receipt result after erase; the same keys survive locally.
_RECEIPT_KEYS = ('record_id', 'revision', 'request_key', 'replayed')
ERASE_CAVEATS = (
    'Erase cannot be undone: every revision of each record becomes a content-free tombstone, its tags become '
    '["erased"] and the record stays forgotten.',
    'Aurora automated backups keep the old content up to 30 days; erase cannot reach them.',
    'This machine\'s local MAVEN runtime files (last-write receipts, failed-write records and maintenance '
    'state, for every project registered on this machine and the Home folder) are swept after the cloud '
    'erase; every file changed is reported. Checkpoint manifests hold only record references and are kept.',
    'Other machines keep any locally cached copy until a separately selected cleanup applies there. '
    'Read-only inspection never changes a cache.',
    'Resolved administration review copies are also scrubbed: record identity, revision and original approval '
    'digest remain as content-free evidence. Unresolved requests must be reconciled before erasure.',
)
# Local replacement for content of records erased earlier, when that erase's tombstone is not at hand.
ERASED_LOCAL = {'erased': True}


class AdminInputError(ValueError):
    """Invalid caller input; the message is MAVEN-authored and safe to return to the caller."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _error_code(error):
    """Bounded safe code: client errors use .code, service MemoryError uses .status."""
    for name in ('code', 'status'):
        value = getattr(error, name, None)
        if isinstance(value, str) and value:
            return value
    return type(error).__name__


def _source(value):
    if (not isinstance(value, dict) or set(value) != {'origin', 'excerpt'}
            or any(not isinstance(value[key], str) or not value[key].strip() for key in value)):
        raise AdminInputError('Supply actual owner provenance as source.origin and source.excerpt; never credentials.')
    if len(value['origin']) > 160 or len(value['excerpt']) > 2000:
        raise AdminInputError('Approval origin/excerpt exceed the cloud provenance limits (160/2000 characters).')
    return value


def _connection_config(values):
    """Inspect legacy schema locators only for review, never for ordinary I/O."""
    from tools.memory.environment import memory_config
    from tools.memory.validation import SCHEMA_VERSION
    legacy = values.get('MAVEN_MEMORY_SCHEMA_VERSION') == '1'
    config = memory_config({**values, 'MAVEN_MEMORY_SCHEMA_VERSION': str(SCHEMA_VERSION)} if legacy else values)
    if legacy:
        config['schema_version'] = 1
    return config


def _installation_source(home):
    """The checkout the installer recorded for this home."""
    from tools.uninstall.local import MANIFEST
    path = Path(home) / MANIFEST
    source = read_private(path).get('source') if path.exists() else None
    if not source:
        raise SetupPending('No installer receipt names the MAVEN checkout for this home. Begin the update with '
                           'an explicit absolute checkout path.')
    return source


def _discover_stacks(values):
    """Read all stack pages using only confirmed maintenance access."""
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from tools.memory.bootstrap import _outputs
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import selected_aws_session, transport_ca_bundle
    from tools.setup_common import expected_account
    region, account = values['MAVEN_AWS_REGION'], expected_account(values)
    session = selected_aws_session(values, deploy=True, region=region)
    cloud = session.client('cloudformation', verify=transport_ca_bundle(), config=Config(
        connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
    found = []
    try:
        for page in cloud.get_paginator('describe_stacks').paginate():
            for stack in page['Stacks']:
                if stack.get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
                    continue
                if not stack.get('StackId', '').startswith(f'arn:aws:cloudformation:{region}:{account}:stack/'):
                    continue
                try:
                    config = _outputs(stack)
                except MemoryClientError as error:
                    if error.code not in ('invalid_stack_outputs', 'invalid_configuration'):
                        raise
                    continue
                if config['client_role_arn'].split(':')[4] != account:
                    continue
                found.append({'stack_id': stack['StackId'], 'name': stack['StackName'],
                              'instance_id': config['instance_id'], 'status': stack['StackStatus']})
    except ClientError as error:
        code = error.response.get('Error', {}).get('Code', 'AWS error')
        denied = code in ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation')
        raise SetupPending(
            ('Missing maintenance access: allow cloudformation:DescribeStacks to enumerate stacks '
             'in this account/region, then resume the same operation.' if denied else
             'CloudFormation discovery could not complete. Check the selected maintenance login '
             'and service availability, then resume the same operation. No stack was selected.')) from None
    finally:
        cloud.close()
    if not found:
        raise SetupPending(f'No ready MAVEN stacks found in account {account}, region {region}. '
                           'Check the account/region and deployment status. Discovery requires valid MAVEN '
                           'outputs and CREATE_COMPLETE or UPDATE_COMPLETE. Nothing was deployed or selected.')
    return sorted(found, key=lambda item: (item['name'], item['stack_id']))


def _canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _redaction_targets(targets, *, provider=False):
    if not isinstance(targets, list) or not 1 <= len(targets) <= (1 if provider else CLEANUP_LIMIT):
        raise AdminInputError('Select one provider target or 1–200 typed redaction targets.')
    identities = set()
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get('kind'), str)
                or target['kind'] not in (_PROVIDER_KINDS if provider else _REDACTION_KINDS)):
            raise AdminInputError('Select a supported redaction target kind.')
        keys = {'kind', 'id', 'project_id'} if target['kind'] == 'project_record' else {'kind', 'id'}
        if set(target) != keys or not all(_canonical_uuid(target[key]) for key in keys - {'kind'}):
            raise AdminInputError('Supply only the exact typed canonical UUIDs; never supply a label or locator.')
        identity = (target['kind'], target.get('project_id'), target['id'])
        if identity in identities:
            raise AdminInputError('Select each typed redaction target only once.')
        identities.add(identity)


def _private_cleanup(state):
    return (state['kind'] == 'cleanup'
            and state['parameters'].get('action') in _REDACTION_ACTIONS)


def _owner_phrase(operation, question):
    return f"APPROVE {operation} {question['interaction_id']} {question['revision']} {question['review_digest']}"


def _redaction_response(question, response, owner_terminal):
    """Never allow owner prose (including declined approvals) into the operation journal."""
    if type(response.get('approved')) is not bool or response.get('review_digest') != question['review_digest']:
        raise AdminInputError('Name the full current review digest and explicitly approve or decline.')
    if not response['approved']:
        if set(response) != {'approved', 'review_digest'}:
            raise AdminInputError('Decline with only approved=false and the reviewed digest.')
        return
    keys = {'approved', 'review_digest', 'source'}
    if question['confirmations']:
        keys.add('confirmations')
    if set(response) != keys:
        raise AdminInputError('Send only the displayed approval fields; never send target labels or explanations.')
    source = response['source']
    if (not isinstance(source, dict) or set(source) != {'origin', 'excerpt'}
            or source['origin'] != ('owner_terminal' if owner_terminal else 'owner_chat')
            or source['excerpt'] != question['owner_phrase']):
        raise AdminInputError('Relay the exact displayed phrase from the owner, or use the private approval terminal.')


def _cleanup_ids(value):
    """An explicit, bounded, duplicate-free list of canonical record UUIDs."""
    if not isinstance(value, list) or not value:
        raise AdminInputError('Select at least one record id for clean-up.')
    if len(value) > CLEANUP_LIMIT:
        raise AdminInputError(f'Select at most {CLEANUP_LIMIT} records per clean-up operation; split larger selections.')
    for item in value:
        try:
            canonical = str(UUID(item))
        except (ValueError, TypeError, AttributeError):
            canonical = None
        if canonical != item:
            raise AdminInputError('Clean-up record ids must be canonical lowercase record UUIDs.')
    if len(set(value)) != len(value):
        raise AdminInputError('Each clean-up record id may be selected once.')
    return list(value)


def _cleanup_reason(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
        raise AdminInputError('The clean-up reason is required text of at most 500 characters; never include '
                              'the content being forgotten or erased.')
    return value.strip()


def _erased_request(body, tombstone):
    """One journaled write request with its content replaced; identity and request key are kept."""
    erased = dict(body)
    if 'data' in erased and not (isinstance(erased['data'], dict) and erased['data'].get('erased') is True):
        erased['data'] = deepcopy(tombstone)
    if 'tags' in erased:
        erased['tags'] = ['erased']
    if isinstance(erased.get('source'), dict) and 'excerpt' in erased['source']:
        erased['source'] = {**erased['source'], 'excerpt': 'erased'}
    for key in ('change_reason', 'reason'):
        if key in erased:
            erased[key] = 'erased'
    return erased


def _sweep_session(root, session, ids, tombstone):
    """Plan the content rewrites of one local session runtime: ({file name: new value}, blockers).

    An unresolved request (a pending-memory.json journal or an unfinished maintenance effect) for an
    erased record is never rewritten or deleted: it must be recovered first.
    """
    from tools.memory.boundary import BoundaryError, digest, private_path, read_private
    runtime = f'.maven/runtime/sessions/{session}'
    updates, blockers = {}, []

    def load(name):
        try:
            if not private_path(root, runtime + '/' + name).exists():
                return None
            return read_private(root, runtime + '/' + name)
        except BoundaryError:
            blockers.append(f'{Path(root) / runtime / name}: unreadable or not owner-private; inspect it, then advance.')
            return None

    pending = load('pending-memory.json')
    if pending is not None and (pending.get('body') or {}).get('record_id') in ids:
        blockers.append(f'{Path(root) / runtime / "pending-memory.json"}: unresolved memory request for erased record '
                        f'{pending["body"]["record_id"]}. Preserve and inspect its original evidence; no request was replayed.')
    last = load('last-memory-write.json')
    if last is not None and isinstance(last.get('result'), dict):
        result = last['result']
        if ((last.get('record') or {}).get('record_id') in ids or result.get('record_id') in ids) \
                and set(result) - set(_RECEIPT_KEYS):
            updates['last-memory-write.json'] = {**last, 'result': {key: result[key] for key in _RECEIPT_KEYS
                                                                    if key in result}}
    failed = load('failed-memory-write.json')
    request = (failed or {}).get('request')
    if isinstance(request, dict) and isinstance(request.get('body'), dict) and request['body'].get('record_id') in ids:
        from tools.memory.client import DEFINITIVE_MUTATION_ERRORS
        if failed.get('status') not in DEFINITIVE_MUTATION_ERRORS:
            blockers.append(f'{Path(root) / runtime / "failed-memory-write.json"}: unresolved original write; recover it before erasure.')
            request = None
        elif ('digest' in request and ('pending_digest' in failed or request['digest'] != digest(
                {key: value for key, value in request.items() if key != 'digest'}))) or (
                'digest' not in request and failed.get('pending_digest') != digest(request)):
            blockers.append(f'{Path(root) / runtime / "failed-memory-write.json"}: original request digest is invalid; preserve it for recovery.')
            request = None
    if isinstance(request, dict) and isinstance(request.get('body'), dict) and request['body'].get('record_id') in ids:
        body = _erased_request(request['body'], tombstone)
        if body != request['body']:
            rewritten = {key: value for key, value in request.items() if key != 'digest'}
            rewritten['body'] = body
            if 'digest' in request:
                rewritten['digest'] = digest(rewritten)
                updates['failed-memory-write.json'] = {**failed, 'request': rewritten}
            else:
                updates['failed-memory-write.json'] = {**failed, 'request': rewritten,
                                                       'pending_digest': digest(rewritten)}
    maintenance = load('maintenance.json')
    if maintenance is not None:
        changed, dirty = deepcopy(maintenance), False
        for assignment in (changed.get('assignments') or {}).values():
            for effect in assignment.get('effects', []):
                request = effect.get('request') or {}
                if request.get('record_id') not in ids:
                    continue
                if effect.get('state') in ('pending', 'applying', 'uncertain'):
                    blockers.append(f'{Path(root) / runtime / "maintenance.json"}: maintenance assignment '
                                    f'{assignment.get("operation_id")} has an unresolved effect for erased record '
                                    f'{request["record_id"]}. Recover or finish it, then advance.')
                    continue
                erased = _erased_request(request, tombstone)
                if erased != request:
                    effect['request'], dirty = erased, True
        if dirty:
            updates['maintenance.json'] = changed
    return updates, blockers


def _session_directories(root):
    from tools.memory.boundary import BoundaryError, private_path
    try:
        base = private_path(root, '.maven/runtime/sessions')
    except BoundaryError as error:
        raise SetupPending('A registered project session directory is unsafe or unreadable; inspect it before cleanup.') from error
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.iterdir()):
        try:
            canonical = str(UUID(path.name))
        except ValueError:
            continue
        if canonical == path.name and path.is_dir() and not path.is_symlink():
            found.append(path.name)
    return found


def sweep_erased(roots, ids, tombstone):
    """Rewrite erased records' content in this machine's local runtime under each roots' sessions.

    All roots are checked first; any unresolved request referencing an erased record refuses the
    whole sweep before a file is changed. Returns every file touched.
    """
    from tools.coordination import LOCK_TIMEOUT, CoordinationError, private_lock
    from tools.memory.boundary import save_private
    ids = frozenset(ids)
    targets = [(root, session) for root in roots for session in _session_directories(root)]
    blockers = [item for root, session in targets for item in _sweep_session(root, session, ids, tombstone)[1]]
    if blockers:
        raise SetupPending('The local sweep was refused; nothing local was changed. Resolve first:\n'
                           + '\n'.join(blockers))
    touched = []
    for root, session in targets:
        runtime = f'.maven/runtime/sessions/{session}'
        try:
            with private_lock(root, runtime + '/command.lock', timeout=LOCK_TIMEOUT), \
                    private_lock(root, runtime + '/maintenance.lock', timeout=LOCK_TIMEOUT):
                updates, blockers = _sweep_session(root, session, ids, tombstone)
                if blockers:
                    raise SetupPending('The local sweep stopped; resolve, then advance:\n' + '\n'.join(blockers)
                                       + ('\nAlready swept: ' + ', '.join(touched) if touched else ''))
                for name, value in sorted(updates.items()):
                    save_private(root, runtime + '/' + name, value)
                    touched.append(str(Path(root) / runtime / name))
        except CoordinationError:
            raise SetupPending(f'Session {session} in {root} is busy; advance this operation again shortly.'
                               + ('\nAlready swept: ' + ', '.join(touched) if touched else '')) from None
    return {'roots': [str(root) for root in roots], 'sessions_checked': len(targets), 'files_touched': touched}


class AdminOperations:
    """Explicit, restartable setup/install/project/removal/recovery operations."""

    def __init__(self, home=None, *, private_provider_retry=None):
        self.home = Path(home or os.environ.get('MAVEN_HOME', '~/.maven')).expanduser().absolute()
        self.directory = self.home / 'runtime' / 'administration'
        # Optional owner-terminal input for verifying an SSM deletion after a lost reply. Never serialized.
        self._private_provider_retry = private_provider_retry

    def _prepare(self):
        for directory in (self.home, self.home / 'runtime', self.directory):
            private_directory(directory)

    def _path(self, identifier):
        if str(UUID(identifier)) != identifier:
            raise AdminInputError('Select the exact administration operation UUID.')
        return self.directory / (identifier + '.json')

    def _load(self, identifier):
        path = self._path(identifier)
        if path.absolute() != path.resolve():
            raise SetupPending('Administration evidence must not traverse symlinks.')
        state = read_private(path, maximum=32 * 1024 * 1024)
        if state.get('schema') != 1 or state.get('home') != str(self.home):
            raise SetupPending('Administration state/home mismatch; preserve the original evidence.')
        # A newly received marker already retires matching evidence even when a crash
        # interrupted the file sweep. Neither status nor advance may expose or replay it.
        from tools.reconcile_redaction import shadow_admin
        return shadow_admin(self.home, state)

    def _save(self, state):
        state['revision'] += 1
        save_json(self._path(state['operation_id']), state)

    def begin(self, kind, parameters):
        allowed = {
            'setup': {'mode', 'source', 'checkout'},
            'deployment_access': set(),
            'secrets_setup': set(),
            'environment_migration': {'path'},
            'repair': set(),
            'project': {'root'},
            'install': {'source', 'harness', 'profile'},
            'uninstall': {'project', 'reactivate_project', 'project_home', 'home', 'machine',
                          'all_homes', 'cloud', 'cloud_home', 'cloud_mode', 'archive_project',
                          'offline', 'export', 'optional_resource', 'retained_resource',
                          'output', 'adopt_legacy', 'plan_path'},
            'recovery': {'operation_id', 'action'},
            'home': {'action', 'root', 'project_id', 'label'},
            'cleanup': {'action', 'record_ids', 'reason', 'targets', 'job_id', 'after'},
            'project_lifecycle': {'action', 'project_id'},
        }
        if kind not in allowed or not isinstance(parameters, dict) or set(parameters) - allowed[kind]:
            raise AdminInputError('Unsupported administration kind or parameters.')
        if kind == 'recovery':
            if (not _canonical_uuid(parameters.get('operation_id'))
                    or parameters.get('action', 'diagnose') not in ('diagnose', 'recover')):
                raise AdminInputError('Select the exact operation UUID and recovery action diagnose or recover.')
            if _private_cleanup(self._load(parameters['operation_id'])):
                raise AdminInputError('Use status/advance on the original redaction UUID. Do not copy it into another recovery operation.')
        if kind == 'setup' and parameters.get('mode') not in (None, 'existing', 'new', 'update'):
            raise AdminInputError('Setup mode must be existing, new or update.')
        if kind == 'setup' and 'checkout' in parameters and (
                parameters.get('mode') != 'update' or not isinstance(parameters['checkout'], str)
                or not Path(parameters['checkout']).expanduser().is_absolute()):
            raise AdminInputError('checkout applies only to mode update and must be an absolute MAVEN checkout path.')
        if kind == 'install' and (not isinstance(parameters.get('harness', []), list)
                or any(not isinstance(item, str) for item in parameters.get('harness', []))):
            raise AdminInputError('Install harness must be an explicit list of names.')
        if kind == 'home' and (parameters.get('action', 'enable') not in ('enable', 'disable')
                or any(not isinstance(parameters.get(key, ''), str) for key in ('root', 'project_id', 'label'))):
            raise AdminInputError('Home action must be enable or disable; root, project_id and label are text.')
        if kind == 'project_lifecycle':
            if set(parameters) != {'action', 'project_id'} or parameters.get('action') not in ('archive', 'reactivate'):
                raise AdminInputError('Project lifecycle requires action archive or reactivate and an exact project_id.')
            project_id = parameters['project_id']
            try:
                valid = isinstance(project_id, str) and str(UUID(project_id)) == project_id
            except ValueError:
                valid = False
            if not valid:
                raise AdminInputError('Project lifecycle requires a canonical project UUID.')
        if kind == 'cleanup':
            if parameters.get('action') not in CLEANUP_ACTIONS:
                raise AdminInputError('Select a supported memory clean-up action.')
            if parameters['action'] in _REDACTION_ACTIONS:
                selected = parameters['action'] in ('redact', 'purge_provider')
                expected = {'action', 'targets'} if selected else {'action'}
                if parameters['action'] == 'redaction_candidates' and 'after' in parameters:
                    _redaction_targets([parameters['after']])
                    expected.add('after')
                if parameters['action'] == 'retry_objects':
                    expected.add('job_id')
                    if not _canonical_uuid(parameters.get('job_id')):
                        raise AdminInputError('Select the canonical UUID of the original reviewed object-purge job.')
                if set(parameters) != expected:
                    raise AdminInputError('Supply only the action and its required typed UUID selection.')
                if selected:
                    _redaction_targets(parameters['targets'], provider=parameters['action'] == 'purge_provider')
            elif set(parameters) & {'targets', 'job_id', 'after'}:
                raise AdminInputError('Typed targets require a separately reviewed redaction action.')
            if parameters['action'] == 'review' and set(parameters) != {'action'}:
                raise AdminInputError('The clean-up review is read-only and takes no records or reason.')
            if 'record_ids' in parameters:
                _cleanup_ids(parameters['record_ids'])
            if 'reason' in parameters:
                _cleanup_reason(parameters['reason'])
        if 'source' in parameters and kind == 'setup':
            _source(parameters['source'])
        self._prepare()
        with setup_lock(self.directory):
            identifier = str(uuid4())
            state = {'schema': 1, 'operation_id': identifier, 'kind': kind, 'home': str(self.home),
                     'revision': 0, 'state': 'pending', 'step': 'start', 'parameters': parameters,
                     'answers': {}, 'approvals': [], 'receipts': [], 'pending_interaction': None,
                     'live_state_checked': False}
            self._save(state)
        return self.advance(identifier)

    def status(self, operation_id):
        """Read existing evidence only: no credentials, AWS calls, or retry."""
        state = self._load(operation_id)
        result = dict(state)
        result['live_state_checked'] = False
        if state['state'] == 'working':
            result['state'] = 'uncertain'
            result['notice'] = 'The saved phase is in flight or was interrupted. Inspect its original receipts; only explicit advance reconciles it.'
        if (state['kind'] == 'setup' and
                (state.get('pending_interaction') or {}).get('key') in ('optional_project', 'project')):
            result['pending_interaction'] = None
            result['notice'] = 'This old installer project prompt is retired. Advance to verify instance readiness; no project will be enabled.'
        if state.get('plan_path'):
            from tools.uninstall.cli import status
            result['removal'] = status(state['plan_path'])
            if result['removal']['status'] == 'completed':
                result['state'] = 'completed'
        result['next_actions'] = (['respond'] if result['pending_interaction'] else
                                  [] if result['state'] in ('completed', 'cancelled', 'abandoned', 'failed') else ['advance'])
        if self._back_checkpoint(state) is not None:
            result['next_actions'].append('back')
        return result

    def respond(self, operation_id, interaction_id, revision, response, *, owner_terminal=False):
        self._prepare()
        with setup_lock(self.directory):
            state = self._load(operation_id)
            question = state['pending_interaction']
            if (not question or question['interaction_id'] != interaction_id
                    or type(revision) is not int or question['revision'] != revision):
                raise AdminInputError('Stale interaction; inspect the current full review before responding.')
            if not isinstance(response, dict):
                raise AdminInputError('A structured response is required.')
            if question['kind'] == 'review':
                if _private_cleanup(state):
                    _redaction_response(question, response, owner_terminal)
                if set(response) - {'approved', 'review_digest', 'source', 'confirmations'}:
                    raise AdminInputError('Unsupported approval response fields: '
                                          + ', '.join(sorted(set(response) - {'approved', 'review_digest', 'source', 'confirmations'}))
                                          + '. A review response is {approved, review_digest, source: {origin, excerpt}, '
                                          'confirmations}.')
                if response.get('review_digest') != question['review_digest']:
                    raise AdminInputError('Approval must name the complete current review digest.')
                if type(response.get('approved')) is not bool:
                    raise AdminInputError('Approval must be explicitly true or false.')
                if not response['approved']:
                    question = {**question, 'interaction_id': str(uuid4()), 'revision': state['revision'] + 1}
                    if _private_cleanup(state):
                        question['owner_phrase'] = _owner_phrase(operation_id, question)
                    state.update(state='paused', pending_interaction=question)
                    state.setdefault('responses', []).append({'interaction_id': interaction_id, 'revision': revision, 'response': response})
                    self._save(state)
                    return self.status(operation_id)
                _source(response.get('source'))
                if response.get('confirmations', {}) != question.get('confirmations', {}):
                    raise AdminInputError('Separate confirmations must match the exact reviewed targets and required attestations.')
                state['approvals'].append({'interaction_id': interaction_id, 'revision': revision,
                                           'review_digest': question['review_digest'], 'source': response['source'],
                                           'review': question['review'],
                                           'confirmations': response.get('confirmations', {})})
                state['answers'][question['key']] = response
            else:
                names = {field['name'] for field in question['fields']}
                if set(response) != names:
                    raise AdminInputError('Answer exactly the displayed fields (' + ', '.join(sorted(names))
                                          + '); never send credentials.')
                for field in question['fields']:
                    value = response[field['name']]
                    if field['kind'] == 'bool':
                        valid = type(value) is bool
                    else:
                        valid = isinstance(value, str) and (not field['required'] or bool(value.strip()))
                        if field['choices']:
                            valid = valid and value in [choice[1] for choice in field['choices']]
                        if field.get('pattern') and value:
                            valid = valid and bool(re.fullmatch(field['pattern'], value.strip()))
                    if not valid:
                        raise AdminInputError('Invalid answer for ' + field['name'])
                key = question['key']
                if key in ('identity', 'deploy_identity', 'removal_identity'):
                    if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', response['profile']) or not Path(response['cache']).expanduser().is_absolute():
                        raise AdminInputError('Select a named profile and an absolute private login-cache directory.')
                answer = response
                if key == 'selection':
                    fixed_mode = state['parameters'].get('mode')
                    if fixed_mode and response.get('mode', fixed_mode) != fixed_mode:
                        raise AdminInputError('This operation retains its explicitly selected setup mode.')
                    answer = {'connection': '', **response}
                    if fixed_mode:
                        answer['mode'] = fixed_mode
                    if answer['region'] != 'us-east-1':
                        raise AdminInputError('The reviewed deployment supports us-east-1 only.')
                    if answer['mode'] == 'new' and answer['connection']:
                        raise AdminInputError('New provisioning cannot select an existing connection.')
                    if answer['connection'].startswith('/'):
                        from tools.environment_migrations import preview
                        _connection_config(preview(Path(answer['connection'])))
                if key == 'cleanup_selection':
                    _cleanup_ids(re.split(r'[\s,]+', response['record_ids'].strip()))
                    _cleanup_reason(response['reason'])
                if key == 'home':
                    try:
                        self._home_selection(response)
                    except (ValueError, SetupPending) as error:
                        raise AdminInputError(str(error)) from None
                    except OSError:
                        raise AdminInputError('Select an accessible Home folder.') from None
                if key == 'project':
                    if not Path(response['root']).is_absolute() or not Path(response['root']).is_dir():
                        raise AdminInputError('Select an absolute existing project directory.')
                    if response['mode'] in ('create', 'create_cloud'):
                        if not response['name'].strip() or response['project_id']:
                            raise AdminInputError('Creation requires a name and no existing project UUID.')
                        if response['mode'] == 'create_cloud' and len(response['name']) > 240:
                            raise AdminInputError('Cloud creation requires a project name of at most 240 characters.')
                    else:
                        try:
                            valid_id = str(UUID(response['project_id'])) == response['project_id']
                        except ValueError:
                            valid_id = False
                        if not valid_id:
                            raise AdminInputError('Select an exact existing project UUID.')
                        if response['mode'] == 'rename':
                            if not response['name'].strip() or len(response['name']) > 240:
                                raise AdminInputError('Rename requires a nonblank project name of at most 240 characters.')
                        elif response['name']:
                            raise AdminInputError('Select an existing project without a new name, or choose rename.')
                if key == 'optional_project':
                    raise SetupPending('Installer project enablement has been removed. Advance this operation to finish instance readiness without changing a project.')
                if key == 'install' and (not Path(response['source']).is_absolute()
                        or not (Path(response['source']) / 'install.py').is_file()):
                    raise AdminInputError('Select an absolute checkout containing install.py.')
                if key == 'provision' and question.get('submit') == 'Prepare deployment review':
                    state['preparation_requested'] = _digest(answer)
                state['answers'][question['key']] = answer
            state.setdefault('responses', []).append({'interaction_id': interaction_id, 'revision': revision, 'response': response})
            state.update(step=question['next_step'], pending_interaction=None, state='pending')
            self._save(state)
        return self.status(operation_id)

    def _back_checkpoint(self, state):
        if state.get('effect_started') or state['state'] in ('working', 'uncertain', 'failed', 'completed', 'cancelled', 'abandoned'):
            return None
        current = (state.get('pending_interaction') or {}).get('interaction_id')
        for checkpoint in reversed(state.get('choice_checkpoints', [])):
            if (checkpoint['question']['interaction_id'] != current
                    and checkpoint['effect_sequence'] == state.get('effect_sequence', 0)):
                return checkpoint
        return None

    def back(self, operation_id):
        """Revise an unsubmitted choice without erasing approvals or effect evidence."""
        self._prepare()
        with setup_lock(self.directory):
            state = self._load(operation_id)
            checkpoint = self._back_checkpoint(state)
            if checkpoint is None:
                raise SetupPending('Cannot go back across a submitted or unresolved effect. Resume/reconcile this exact operation; historical approvals and receipts must be preserved.')
            question = deepcopy(checkpoint['question'])
            question.update(interaction_id=str(uuid4()), revision=state['revision'] + 1)
            selected = next((entry['response'] for entry in reversed(state.get('responses', []))
                             if entry['interaction_id'] == checkpoint['question']['interaction_id']), {})
            for field in question['fields']:
                if field['name'] in selected:
                    field['default'] = selected[field['name']]
            state.setdefault('navigation', []).append({'action': 'back', 'from_step': state['step'],
                                                       'revision': state['revision']})
            state['answers'] = deepcopy(checkpoint['answers'])
            state.update(step=checkpoint['step'], state='awaiting_decision', pending_interaction=question)
            state['choice_checkpoints'] = state['choice_checkpoints'][:state['choice_checkpoints'].index(checkpoint)]
            state['choice_checkpoints'].append({**deepcopy(checkpoint), 'question': deepcopy(question)})
            if 'presentation' in checkpoint:
                state['presentation'] = deepcopy(checkpoint['presentation'])
            state.pop('handoff', None)
            state.pop('error', None)
            self._save(state)
        return self.status(operation_id)

    def _question(self, state, key, title, fields, next_step, *, description='', details='', submit='Continue'):
        previous = state['answers'].get(key, {})
        serialized = [asdict(field) for field in fields]
        for field in serialized:
            if (field['name'] in previous and (not field['choices'] or
                    any(value == previous[field['name']] for _, value in field['choices']))):
                field['default'] = previous[field['name']]
        question = {
            'interaction_id': str(uuid4()), 'revision': state['revision'] + 1,
            'kind': 'form', 'key': key, 'title': title, 'fields': serialized,
            'description': description or 'Choose the exact target and access needed for this task. No cloud changes are made by this form.',
            'details': details, 'submit': submit, 'next_step': next_step}
        state.setdefault('choice_checkpoints', []).append({
            'question': deepcopy(question), 'answers': deepcopy(state['answers']),
            'step': state['step'], 'effect_sequence': state.get('effect_sequence', 0),
            'presentation': deepcopy(state.get('presentation', {}))})
        state.update(state='awaiting_decision', pending_interaction=question)

    def _review(self, state, key, title, review, next_step, confirmations=None):
        context = state.get('presentation', {})
        summary = {
            'deployment_binding': 'Change only this installation’s deployment credentials selection. '
                                  'No IAM permissions, cloud resources, memory roles or projects change.',
            'connection': 'Save the selected local connection only; existing mode never deploys or initializes.',
            'connect': 'Bind this existing memory instance locally; preserve its data and authority.',
            'plan': 'Create a reviewable CloudFormation change set, not the resources. The candidate describes Aurora database, networking, IAM, API, storage, notifications and logging. Execution is separately approved; Aurora capacity, retained data/snapshots, network traffic, API requests and logs can incur ongoing charges. Interruption must reconcile this same change set; no automatic execution or rollback.',
            'apply': 'Create billable Aurora database, network, IAM, API, storage, notification and logging resources. Aurora capacity, storage, snapshots, network traffic, API requests and logs incur charges. Interruption may leave an in-flight stack; resume the exact change set. Retained data can continue costing money after removal.',
            'schema': 'Run the exact SQL bundle under the existing separately assumed administration role. Initialization/migration changes database schema and may interrupt memory writers; preserve and reconcile uncertain requests. This does not approve authority activation.',
            'removal': 'Remove only the reviewed targets. Retire retains data and ongoing storage/snapshot costs; purge can permanently destroy data. Writers/harnesses must stop where the plan requires it. Interrupted removal resumes original receipts, never a new plan.',
            'project_lifecycle': 'Change only the reviewed cloud project lifecycle across all machines. '
                                 'Archive hides the project and its content from default discovery and blocks writes; '
                                 'explicit archived-history consultation remains available. Reactivate restores '
                                 'default visibility and cloud write eligibility, not erased or forgotten content. '
                                 'History is retained. No local project is attached, detached or changed. '
                                 'Confirm affected writers are quiescent; active sessions, claims and unfinished '
                                 'cloud work block this operation and are never automatically closed or abandoned. '
                                 'Resume this same operation after interruption.',
            'service_update': 'Execute this exact CloudFormation change set on the existing MAVEN service stack. '
                              'Listed resources are modified in place; no resource that holds memory data or '
                              'credentials is removed or replaced (that is refused before review). The service may '
                              'briefly return errors while the update runs; interruption resumes this same change set.',
        }.get(key, title + '. Approve only this exact candidate; changes require a fresh review.')
        summary += '\nAccount: ' + str(context.get('account', 'Unknown')) + '; region: ' + str(context.get('region', 'us-east-1'))
        summary += '\nTarget home: ' + state['home']
        if context.get('target'):
            summary += '\nExact target: ' + context['target']
        if isinstance(review, dict):
            targets = {name: review[name] for name in
                       ('instance_id', 'identity', 'selection', 'instance', 'targets',
                        'machine_homes', 'scopes', 'region', 'mode', 'connection',
                        'previous_binding', 'deployment_binding', 'daily_source', 'check',
                        'home_file', 'remove', 'consent', 'replaces', 'switch', 'rename', 'create_cloud')
                       if name in review}
            if targets:
                summary += '\nTargets and scope:\n' + json.dumps(targets, indent=2)
        if state.get('preflight'):
            report = state['preflight']
            summary += '\nAccess preflight: ' + report['status'] + '. ' + ' '.join(report['limitations'])
        state.update(state='awaiting_decision', pending_interaction={
            'interaction_id': str(uuid4()), 'revision': state['revision'] + 1,
            'kind': 'review', 'key': key, 'title': title, 'review': review,
            'description': summary, 'details': json.dumps({'preflight': state.get('preflight')}, indent=2),
            'review_digest': _digest(review), 'confirmations': confirmations or {},
            'required_authorization': 'Actual owner approval with truthful origin and excerpt',
            'next_step': next_step})

    def _approved(self, state, key, review):
        answer = state['answers'][key]
        if answer['review_digest'] != _digest(review):
            raise SetupPending('Reviewed candidate changed. Preserve this operation and review a new exact candidate; approval is invalid.')
        return answer['source']

    def _receipt(self, state, step, result):
        state['receipts'].append({'step': step, 'result': result})

    def _handoff(self, state, reason, command, *, environment=None):
        state.update(state='blocked', handoff={'reason': reason, 'command': command,
                     'environment': environment or {}, 'completion_claimed': False})

    def _expired_login_handoff(self, state, error, *, values=None, deploy=None):
        from tools.memory.environment import authentication_handoff, load_environment
        from tools.memory.client import AUTHENTICATION_CODES, MemoryClientError, aws_error
        if aws_error(error).code not in AUTHENTICATION_CODES | {'deployment_elevation_denied', 'deployment_session_expired'}:
            return False
        from tools.setup_common import expected_account
        if values is None:
            try:
                values = load_environment(self.home / '.env')
            except MemoryClientError:
                return False
        if deploy is None:
            deploy = (state.get('presentation', {}).get('purpose', '').startswith('Maintenance')
                      or state['step'] in ('plan', 'apply', 'update_service_apply', 'update_service',
                                            'migration_stack', 'discover', 'deploy_identity'))
        handoff = authentication_handoff(error, values, deploy=deploy)
        if handoff:
            separate = deploy and values.get('MAVEN_DEPLOY_MODE', 'profile') == 'profile'
            prefix = 'MAVEN_DEPLOY_' if separate else 'MAVEN_AWS_'
            state['handoff'] = {**handoff, 'completion_claimed': False, 'account': expected_account(values),
                                'deploy_profile': bool(separate),
                                'expected_principal': values.get(prefix + 'EXPECTED_PRINCIPAL', 'Unknown')}
        return handoff is not None

    def advance(self, operation_id, *, expected_revision=None):
        self._prepare()
        from tools.environment_migrations import prerequisite
        original = self._load(operation_id)
        if expected_revision is not None and original['revision'] != expected_revision:
            return {**self.status(operation_id), 'login_status': 'handoff_changed'}
        if original['state'] in ('completed', 'cancelled', 'abandoned', 'failed'):
            return self.status(operation_id)
        prerequisite_target = original
        if original['kind'] == 'recovery' and original['parameters'].get('operation_id'):
            prerequisite_target = self._load(original['parameters']['operation_id'])
        local_only = (prerequisite_target['kind'] in ('install', 'environment_migration')
                      or prerequisite_target['kind'] == 'recovery'
                      or (prerequisite_target['kind'] == 'home'
                          and prerequisite_target['parameters'].get('action') == 'disable')
                      or (prerequisite_target['kind'] == 'uninstall'
                          and prerequisite_target['parameters'].get('offline')))
        if original['kind'] == 'recovery' and original['parameters'].get('action', 'diagnose') == 'diagnose':
            local_only = True
        pending = None if local_only else prerequisite(self, prerequisite_target)
        if pending:
            return {**self.status(operation_id), 'prerequisite_operation': pending['operation_id'],
                    'prerequisite': pending, 'next_actions': ['advance_prerequisite'],
                    'notice': 'Complete the separate persisted migration review, then advance this original operation. Its interactions and approvals are unchanged.'}
        with setup_lock(self.directory):
            state = self._load(operation_id)
            if expected_revision is not None and state['revision'] != expected_revision:
                return {**self.status(operation_id), 'login_status': 'handoff_changed'}
            self._advance_locked(state)
            return self.status(operation_id)

    def _advance_locked(self, state):
        """Execute one phase while the caller holds the administration lock."""
        if (state['kind'] == 'setup' and
                (state.get('pending_interaction') or {}).get('key') in ('optional_project', 'project')):
            if state['step'] == 'project_apply' or any(
                    receipt['step'].startswith('project_') for receipt in state['receipts']):
                raise SetupPending('Legacy project effects may be submitted or unresolved. Reconcile their original receipts separately; installer setup cannot rewind or retry them.')
            state.setdefault('retired_interactions', []).append(state['pending_interaction'])
            state.update(pending_interaction=None, step='verify', state='pending')
        if state['pending_interaction'] or state['state'] in ('completed', 'cancelled', 'abandoned', 'failed'):
            return
        if (state['kind'] == 'cleanup' and state['step'] == 'cleanup_apply'
                and state['state'] in ('working', 'uncertain') and not state.get('effect_started')
                and not state.get('cleanup_request')):
            state['legacy_cleanup_uncertain'] = True
        if (state['kind'] == 'setup' and state['step'] == 'update_service_apply'
                and state['state'] in ('working', 'uncertain', 'blocked') and not state.get('effect_started')
                and not state.get('service_apply_started')):
            # Legacy interrupted executions have no new intent field; preserve their original request.
            state['service_apply_started'] = True
        state.update(state='working', live_state_checked=False)
        for key in ('error', 'error_detail', 'handoff', 'read_only_retry'):
            state.pop(key, None)
        if _private_cleanup(state):
            state.pop('recovery', None)
        if state['step'] in ('connection', 'plan', 'apply', 'initialize', 'migrate',
                              'migration_bind',
                              'update_service_apply', 'project_apply', 'removal_apply', 'recover',
                              'home_apply', 'home_disable', 'cleanup_apply', 'cleanup_sweep',
                              'redaction_apply', 'redaction_sweep', 'project_lifecycle_apply'):
            state['effect_sequence'] = state.get('effect_sequence', 0) + 1
            state['effect_started'] = True
        self._save(state)
        try:
            if _private_cleanup(state):
                # There is no console transcript for a redaction. Only structured, checked results
                # are published; incidental prints must not be redirected to the harness log.
                with open(os.devnull, 'w') as discard, redirect_stdout(discard):
                    self._cleanup(state)
            else:
                with redirect_stdout(sys.stderr):
                    getattr(self, '_' + state['kind'])(state)
        except Exception as error:
            if _private_cleanup(state):
                from tools.redaction_administration import stop
                stop(state, error)
                self._save(state)
                return
            code = _error_code(error)
            uncertain = any(word in code.lower() for word in ('unknown', 'uncertain', 'timeout'))
            if state.get('effect_started') and not isinstance(error, (SetupPending, ValueError)):
                from tools.memory.client import MemoryClientError
                from tools.memory.validation import MemoryError
                uncertain = uncertain or not isinstance(error, (MemoryClientError, MemoryError))
            state['state'] = 'uncertain' if uncertain else 'blocked'
            state['error'] = str(error) if isinstance(error, SetupPending) else code
            from tools.memory.client import MemoryClientError
            if isinstance(error, MemoryClientError) and error.detail:
                state['error_detail'] = error.detail
            elif isinstance(error, subprocess.CalledProcessError):
                state['error_detail'] = 'Child process exited with status ' + str(error.returncode) + '; command output was not retained.'
            state['read_only_retry'] = code == 'warming' and state['step'] in (
                'verify', 'update_schema', 'home_review', 'project_review', 'cleanup_review')
            from tools.memory.validation import MemoryError
            if isinstance(error, MemoryError) and code in (
                    'migration_rejected', 'migration_failed', 'migration_outcome_unknown',
                    'cleanup_upgrade_required', 'cleanup_refused'):
                state['error_detail'] = error.metadata.get('detail', '')
            state['recovery'] = 'Inspect status, correct the prerequisite, then advance this same operation. Original request artifacts are retained.'
            try:
                # An unsaved candidate can differ from the installed binding (even root).
                # Only the code holding that candidate may supply its login handoff.
                if state['step'] not in ('deploy_access_inspect', 'deploy_access_save'):
                    self._expired_login_handoff(state, error)
                if state.get('handoff', {}).get('type') == 'aws_login':
                    state['state'] = 'blocked'
            except (OSError, ValueError, KeyError):
                pass
        if state['state'] not in ('blocked', 'uncertain', 'failed'):
            state.pop('effect_started', None)
        if state['state'] == 'working':
            state['state'] = 'pending'
        self._save(state)

    def _wizard(self, state):
        from tools.setup import Wizard
        wizard = Wizard(self.home)
        owner = wizard.state.get('administration_operation')
        if owner != state['operation_id']:
            if owner:
                previous = self._load(owner)
                verified = any(receipt.get('step') == 'verify'
                               and receipt.get('result', {}).get('instance_ready') is True
                               for receipt in previous.get('receipts', []))
                if (previous.get('operation_id') != owner or previous.get('kind') != 'setup'
                        or previous.get('state') != 'completed'
                        or previous.get('pending_interaction') or not verified):
                    raise SetupPending('Existing setup operation ' + owner + ' is unresolved. Resume that exact '
                                       'administration UUID; cancellation alone does not resolve its effects.')
                # Archive before replacing the shared owner pointer. A crash between
                # these writes may retry the identical archive, never overwrite it.
                archive = self.directory / ('setup-' + owner + '.json')
                if archive.exists() or archive.is_symlink():
                    if read_private(archive) != wizard.state:
                        raise SetupPending('Archived setup evidence differs; preserve both states and reconcile the original operation.')
                else:
                    save_json(archive, wizard.state)
                state['previous_setup_operation'] = owner
                self._save(state)
                wizard.state = {'schema': 1, 'administration_operation': state['operation_id'],
                                'administration_artifacts': state['operation_id']}
            else:
                if (set(wizard.state) - {'schema', 'secrets_operation'}
                        or any(path.name not in ('state.json', 'lock') for path in wizard.directory.iterdir())):
                    raise SetupPending('Existing legacy setup evidence has no verified terminal operation. Resume the original maven-setup terminal flow; do not replace its receipts.')
                wizard.state['administration_operation'] = state['operation_id']
            wizard.save()
        artifacts = wizard.state.get('administration_artifacts')
        if artifacts is not None:
            if artifacts != state['operation_id']:
                raise SetupPending('Setup artifact ownership differs from this operation; preserve the original evidence.')
            # Prior root-level or operation-scoped requests remain untouched and
            # cannot be mistaken for this operation's approvals.
            wizard.directory = wizard.directory / artifacts
            private_directory(wizard.directory)
        return wizard

    def _environment_migration(self, state):
        from tools.environment_migrations import advance
        advance(self, state)

    def _secrets_setup(self, state):
        from tools.secret_setup import standalone
        standalone(self, state)

    def _repair(self, state):
        from tools.secret_setup import standalone
        standalone(self, state)

    def _deployment_access(self, state):
        from tools.setup import Wizard
        directory = self.home / 'runtime' / 'setup'
        private_directory(directory)
        with setup_lock(directory):
            wizard = Wizard(self.home)
            if not wizard.env.is_file():
                raise SetupPending('Connect this installation first; no deployment setting was changed.')
            owner = wizard.state.get('administration_operation')
            if owner:
                active = self._load(owner)
                # A software-only update handoff may be parked before the daily identity
                # prompt. It has not selected deployment access or submitted cloud work.
                pre_cloud_update = (active['kind'] == 'setup' and active['parameters'].get('mode') == 'update'
                    and active['step'] == 'identity' and active['state'] == 'pending'
                    and not active['pending_interaction'] and not active['approvals']
                    and set(active['answers']) <= {'local_update'}
                    and all(r['step'] in ('update_local', 'update_local_choice') for r in active['receipts'])
                    and not any(active.get(key) for key in ('effect_started', 'effect_sequence', 'service_apply_started',
                        'service_candidate_directory', 'deploy_identity_actual', 'identity_return_step')))
                if active['state'] != 'completed' and not pre_cloud_update:
                    raise SetupPending('Resume the unfinished setup operation ' + owner +
                                       ' before changing its deployment binding.')
            elif not wizard.state.get('instance_ready') and (
                    set(wizard.state) - {'schema', 'mode', 'existing_source', 'secrets_operation'}
                    or any(p.name not in ('lock', 'state.json') for p in directory.iterdir())):
                raise SetupPending('Unresolved legacy setup artifacts may bind deployment access. Preserve and reconcile them first.')
            self._deploy_access_phase(state, wizard)

    def _deploy_access_phase(self, state, wizard):
        """Review a replacement in memory; the sole settings write is after approval and preflight."""
        from tools.setup_common import expected_account, verify_deployment_access
        step, answers, values = state['step'], state['answers'], wizard.values
        previous = {key: values[key] for key in _DEPLOY_KEYS if key in values}
        account = expected_account(values)
        region = values.get('MAVEN_AWS_REGION', 'us-east-1')
        state['presentation'] = {'title': 'Deployment access', 'purpose': 'Maintenance access — CloudFormation',
            'region': region, 'account': account, 'target': str(self.home)}
        if step in ('start', 'deploy_identity'):
            choices = [('Assume an existing IAM role from daily sign-in (recommended)', 'role'),
                       ('Use a separate sign-in profile', 'profile')]
            if state['kind'] == 'deployment_access' or (state.get('identity_return_step') == 'connect_review'
                                                        and values.get('MAVEN_MEMORY_INSTANCE_ID')):
                choices.append(('Configure later — do not use deployment credentials', 'deferred'))
            default = previous.get('MAVEN_DEPLOY_MODE', 'profile' if previous else 'role')
            self._question(state, 'deploy_access', 'How should MAVEN access AWS for deployment?', [
                Field('mode', 'Deployment access', default if any(mode == default for _, mode in choices) else 'role',
                      kind='select', choices=tuple(choices),
                      help='Role mode verifies the daily login and assumes one exact role for 15 minutes.')],
                'deploy_access_details', description='Expected account: ' + account + '. Uses existing IAM access only. '
                'No roles, grants or cloud resources are created. No root or other credential fallback.')
            return
        mode = answers['deploy_access']['mode']
        if step == 'deploy_access_details':
            if mode == 'role':
                self._question(state, 'deploy_role', 'Select the existing deployment role', [
                    Field('role_arn', 'Exact same-account IAM role ARN', values.get('MAVEN_DEPLOY_ROLE_ARN', ''),
                          pattern=r'arn:aws:iam::[0-9]{12}:role/[\w+=,.@/-]+',
                          help='Use an IAM role ARN, not a CLI profile or an STS session ARN.')],
                    'deploy_access_inspect', description='Source: ' + values.get('MAVEN_AWS_EXPECTED_PRINCIPAL', 'Not configured')
                    + '. MAVEN verifies this source and the assumed role, then reads CloudFormation account limits.')
            elif mode == 'profile':
                self._question(state, 'deploy_identity', 'Select the separate deployment sign-in', [
                    Field('profile', 'Named AWS login-only profile', values.get('MAVEN_DEPLOY_PROFILE', ''),
                          help='Use a separate deployment login. No access keys, tokens, or passwords.'),
                    Field('cache', 'Private deployment login-cache directory', values.get('MAVEN_DEPLOY_LOGIN_CACHE',
                          str(self.home / 'runtime/aws-deploy-login-cache')), help='Separate from the daily login cache.')],
                    'deploy_access_inspect', description='Expected account: ' + account +
                    '. MAVEN checks STS identity and reads CloudFormation account limits before saving.')
            else:
                state['step'] = 'deploy_access_inspect'
            return
        if step not in ('deploy_access_inspect', 'deploy_access_save'):
            raise SetupPending('Unknown deployment access phase; preserve this operation.')
        base = {key: value for key, value in values.items() if key not in _DEPLOY_KEYS}
        binding = {'MAVEN_DEPLOY_MODE': mode}
        if mode == 'role':
            binding['MAVEN_DEPLOY_ROLE_ARN'] = answers['deploy_role']['role_arn']
        elif mode == 'profile':
            profile, cache = answers['deploy_identity']['profile'], Path(answers['deploy_identity']['cache']).expanduser()
            if not cache.is_absolute() or str(cache) == values.get('MAVEN_AWS_LOGIN_CACHE'):
                raise SetupPending('Select a separate absolute private deployment login cache.')
            if profile == values.get('MAVEN_AWS_PROFILE'):
                raise SetupPending('Select a separate named login profile, not the daily profile.')
            binding.update(MAVEN_DEPLOY_PROFILE=profile, MAVEN_DEPLOY_LOGIN_CACHE=str(cache))
        approved = state['approvals'][-1]['review'] if step == 'deploy_access_save' else None
        if approved:
            self._approved(state, 'deployment_binding', approved)
            if (_digest(base) != approved['environment_digest'] or
                    previous not in (approved['previous_binding'], approved['deployment_binding'])):
                raise SetupPending('Settings changed after deployment access review. Review the current binding before replacing it.')
        identity = None
        if mode != 'deferred':
            if mode == 'profile':
                private_directory(cache)
            candidate = {**base, **binding}
            if account.isdigit():
                candidate.setdefault('MAVEN_ACCOUNT_ID', account)
            if mode == 'profile' and approved:
                candidate['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'] = approved['identity']['Arn']
            elif mode == 'profile' and previous.get('MAVEN_DEPLOY_PROFILE') == profile and previous.get('MAVEN_DEPLOY_EXPECTED_PRINCIPAL'):
                candidate['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'] = previous['MAVEN_DEPLOY_EXPECTED_PRINCIPAL']
            try:
                actual = verify_deployment_access(candidate)
            except Exception as error:
                if not self._expired_login_handoff(state, error, values=candidate, deploy=True):
                    raise
                state.update(state='blocked', error=_error_code(error))
                return
            if account.isdigit() and actual['Account'] != account:
                raise SetupPending('The deployment account differs from this installation; no settings were changed.')
            identity = {key: actual[key] for key in ('Account', 'Arn', 'UserId') if key in actual}
            if mode == 'profile':
                binding['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'] = identity['Arn']
        review = {'previous_binding': approved['previous_binding'] if approved else previous,
                  'deployment_binding': binding, 'mode': mode, 'identity': identity,
                  'daily_source': {key: base.get('MAVEN_AWS_' + key) for key in ('PROFILE', 'EXPECTED_PRINCIPAL', 'LOGIN_CACHE')},
                  'environment_digest': _digest(base), 'region': region, 'home': str(self.home),
                  'check': 'No AWS check requested; deployment disabled.' if mode == 'deferred' else
                           'STS identity and cloudformation:DescribeAccountLimits passed. Write permissions not tested.'}
        state['live_state_checked'] = identity is not None
        if not approved:
            self._review(state, 'deployment_binding', 'Review deployment access — replace local binding only',
                         review, 'deploy_access_save')
            return
        self._approved(state, 'deployment_binding', review)
        if previous != binding:
            wizard.settings(binding, remove=_DEPLOY_KEYS)
        state['effect_sequence'] = state.get('effect_sequence', 0) + 1
        self._receipt(state, 'deployment_binding', review)
        if state['kind'] == 'deployment_access':
            state.update(state='completed', outcome={'mode': mode, 'identity': identity, 'check': review['check']})
        else:
            state['deploy_identity_actual'] = identity
            state['step'] = state.pop('identity_return_step', 'provision')

    def _preflight(self, state, values, task):
        from tools.setup_common import permission_preflight
        report = permission_preflight(values, task)
        state['preflight'] = report
        self._receipt(state, 'preflight', report)
        if report['status'] == 'Missing access':
            failed = '\n'.join(item['status'] + ': ' + item['check'] for item in report['checks']
                               if item['status'] != 'Passed')
            raise SetupPending('Missing access for ' + task + '.\n' + failed + '\n' +
                               ' '.join(report['limitations']) + '\n' + report['remediation'])
        return report

    def _complete_setup(self, state, wizard, verified):
        from tools.setup_common import expected_account
        state.update(state='completed', pending_interaction=None, outcome={
            'home': str(self.home), 'account': expected_account(wizard.values),
            'region': wizard.values.get('MAVEN_AWS_REGION', 'us-east-1'),
            'instance_id': wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'),
            'profile': wizard.values.get('MAVEN_AWS_PROFILE'),
            'instance_ready': True, 'secrets_ready': bool(state.get('secrets_ready')),
            'connection_verified': verified, 'project_configured': False,
            'binding': dict(wizard.values)})


    def _setup(self, state):
        from tools.memory import bootstrap
        from tools.memory.environment import load_environment, selected_aws_session, transport_ca_bundle
        from tools.setup import Wizard
        setup_directory = self.home / 'runtime' / 'setup'
        private_directory(setup_directory)
        with setup_lock(setup_directory):
            wizard = self._wizard(state)
            if state['step'].startswith('secrets_'):
                from tools.secret_setup import advance
                advance(self, state, wizard)
                return
            step, answers = state['step'], state['answers']
            if step.startswith('deploy_access_') or step == 'deploy_identity':
                self._deploy_access_phase(state, wizard)
                return
            from tools.setup_common import expected_account
            deploy = (step.startswith('deploy_identity') or
                      step in ('discover', 'discovery_select', 'provision', 'prepare', 'plan', 'apply_review', 'apply',
                               'migration_stack', 'update_service', 'update_service_apply') or
                      (step == 'connect_review' and not wizard.values.get('MAVEN_MEMORY_INSTANCE_ID')))
            prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
            actual = state.get(('deploy_identity' if deploy else 'identity') + '_actual') or {}
            state['presentation'] = {'title': 'Update MAVEN' if state['parameters'].get('mode') == 'update' else 'MAVEN instance setup',
                'purpose': ('Maintenance access — CloudFormation create/update or necessary metadata discovery'
                            if deploy else 'Daily-use access — connect to memory; SQL/authority require separate approvals'),
                'account': actual.get('Account') or expected_account(wizard.values),
                'profile': wizard.values.get('MAVEN_AWS_PROFILE' if deploy and wizard.values.get('MAVEN_DEPLOY_MODE') == 'role'
                                             else prefix + 'PROFILE', 'Not selected'),
                'principal': actual.get('Arn') or wizard.values.get(prefix + 'ROLE_ARN') or wizard.values.get(prefix + 'EXPECTED_PRINCIPAL', 'Unknown'),
                'verified': bool(actual), 'region': wizard.values.get('MAVEN_AWS_REGION', 'us-east-1'),
                'target': (wizard.values.get('MAVEN_STACK_NAME', '') + '; instance ' +
                           (wizard.values.get('MAVEN_MEMORY_INSTANCE_ID') or state.get('new_instance_id', 'Not selected'))),
                'description': 'Target home: ' + str(self.home) + '. Project enablement is a separate task.',
                'details': ''}
            if (state.get('new_instance_id') and wizard.values.get('MAVEN_MEMORY_INSTANCE_ID')
                    and wizard.values['MAVEN_MEMORY_INSTANCE_ID'] != state['new_instance_id']):
                raise SetupPending('Current connection differs from the originally prepared instance; no initialization was dispatched.')
            has_target = bool(wizard.values.get('MAVEN_MEMORY_INSTANCE_ID') or
                              wizard.values.get('MAVEN_STACK_NAME'))
            if step == 'connect_review' and not has_target:
                # Resume old blank-target failures through confirmed discovery,
                # never by guessing a stack or discarding prior receipts.
                state.update(step='deploy_identity' if state.get('identity_actual') else 'identity',
                             identity_return_step='discover')
                return
            if state['parameters'].get('mode') == 'update' and step.startswith('update_'):
                self._update(state, wizard, step, answers)
                return
            if step == 'start' and state['parameters'].get('mode') == 'update':
                if not wizard.values.get('MAVEN_MEMORY_INSTANCE_ID') or not wizard.values.get('MAVEN_STACK_NAME'):
                    raise SetupPending('This installation is not connected to a MAVEN instance. Run setup to connect '
                                       'or create one first; update only changes an existing installation.')
                state['step'] = 'update_local'
                return
            if step == 'start':
                mode = state['parameters'].get('mode')
                fields = [] if mode else [
                    Field('mode', 'Instance', 'existing', kind='select',
                          choices=(('Connect existing', 'existing'), ('Provision new', 'new')))]
                if mode != 'new':
                    fields.append(Field('connection', 'Connection override (optional): .env path or stack name',
                        required=False,
                        help=('Blank reuses this installation’s saved connection.' if has_target else
                              'Leave blank to sign in with maintenance access and search AWS for MAVEN stacks. You will choose the instance; nothing is automatically deployed.')))
                fields.append(Field('region', 'AWS region', wizard.values.get('MAVEN_AWS_REGION', 'us-east-1'),
                    help='The reviewed infrastructure supports us-east-1. This pins the target region, not credentials.'))
                title = 'Provision new memory' if mode == 'new' else 'Connect existing memory' if mode else 'Choose instance setup'
                state['presentation']['title'] = title
                self._question(state, 'selection', title, fields, 'selection',
                    description=('Selected task: ' + (mode or 'choose existing or new') +
                        '. Confirm the region and any optional connection override; no external changes occur here.'))
            elif step == 'selection':
                selection = answers['selection']
                if selection['region'] != 'us-east-1':
                    raise SetupPending('The reviewed infrastructure supports us-east-1 only.')
                if selection['mode'] == 'new' and wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                    raise SetupPending('Existing connection cannot be redeployed; choose a separate home.')
                if selection['mode'] == 'new':
                    wizard.settings({'MAVEN_AWS_REGION': selection['region']})
                    state['step'] = 'identity'
                    return
                review = dict(selection)
                connection = selection['connection']
                if selection['mode'] == 'existing' and connection.startswith('/'):
                    from tools.environment_migrations import load_import
                    imported = {key: value for key, value in load_import(self.home, Path(connection)).items()
                                if key not in _DEPLOY_KEYS and key != 'MAVEN_AWS_LOGIN_CACHE'}
                    _connection_config(imported)
                    review['imported_settings'] = imported
                    review['target_settings'] = dict(wizard.values)
                    state['presentation'].update(account=expected_account(imported),
                        target=imported.get('MAVEN_MEMORY_INSTANCE_ID', connection))
                self._review(state, 'connection', 'Review exact connection selection; existing mode never deploys or initializes', review, 'connection')
            elif step == 'connection':
                review = state['approvals'][-1]['review']
                self._approved(state, 'connection', review)
                updates = {'MAVEN_AWS_REGION': answers['selection']['region']}
                if 'imported_settings' in review:
                    from tools.environment_migrations import load_import
                    imported = {key: value for key, value in load_import(self.home, Path(review['connection'])).items()
                                if key not in _DEPLOY_KEYS and key != 'MAVEN_AWS_LOGIN_CACHE'}
                    if imported != review['imported_settings']:
                        raise SetupPending('Imported private settings changed after review.')
                    conflicts = [key for key in imported if key in wizard.values and imported[key] != wizard.values[key]]
                    if conflicts:
                        raise SetupPending('Imported settings conflict with this home; nothing was overwritten.')
                    updates.update(imported)
                elif review['connection']:
                    updates['MAVEN_STACK_NAME'] = review['connection']
                target = review.get('target_settings')
                if target is not None and wizard.values not in (target, {**target, **updates}):
                    raise SetupPending('Local settings changed after import review; preserve this operation and review the new exact target.')
                wizard.settings(updates)
                state['step'] = 'identity'
            elif step == 'identity':
                prefix = 'MAVEN_AWS_'
                state['presentation'].update(verified=False, account=expected_account(wizard.values),
                    principal=wizard.values.get(prefix + 'EXPECTED_PRINCIPAL', 'Unknown'))
                self._question(state, step, 'Select daily-use access', [
                    Field('profile', 'Named AWS login-only profile', wizard.values.get(prefix + 'PROFILE', ''),
                          help='A local AWS CLI profile name selects who signs in, not a permission grant. Daily-use accesses memory; maintenance manages infrastructure or reads required stack metadata. Never enter access keys.'),
                    Field('cache', 'Private sign-in cache directory', wizard.values.get(prefix + 'LOGIN_CACHE',
                          str(self.home / 'runtime' / 'aws-login-cache')),
                          help='An absolute owner-private local folder for AWS login tokens. Use separate folders for daily-use and maintenance so sign-ins cannot replace each other.')], step + '_inspect',
                    description=state['presentation']['purpose'] + '\nExpected account: ' + expected_account(wizard.values) +
                                '\nExpected principal: ' + wizard.values.get(prefix + 'EXPECTED_PRINCIPAL', 'Unknown — confirm actual STS identity after sign-in'))
            elif step == 'deploy_identity_inspect':
                # An unfinished older form selected a profile, not a role. Preserve the
                # answer and require the new verified binding review before continuing.
                answers['deploy_access'] = {'mode': 'profile'}
                state['step'] = 'deploy_access_inspect'
            elif step == 'identity_inspect':
                from tools.setup_common import inspect_login_identity
                key, prefix = 'identity', 'MAVEN_AWS_'
                selected = answers[key]
                if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', selected['profile']):
                    raise SetupPending('Select a valid named AWS profile, not credentials.')
                cache = Path(selected['cache']).expanduser()
                if not cache.is_absolute():
                    raise SetupPending('Login cache must be an absolute private directory.')
                other = wizard.values.get('MAVEN_DEPLOY_LOGIN_CACHE')
                if other and cache == Path(other).expanduser():
                    raise SetupPending('Routine and deployment caches must be separate.')
                private_directory(cache)
                wizard.settings({prefix + 'PROFILE': selected['profile'], prefix + 'LOGIN_CACHE': str(cache)})
                selected['cache'] = str(cache)
                state['presentation'].update(verified=False, profile=selected['profile'],
                    account=expected_account(wizard.values),
                    principal=wizard.values.get(prefix + 'EXPECTED_PRINCIPAL', 'Unknown'))
                try:
                    identity = inspect_login_identity(wizard.values)
                    from tools.setup_common import check_login_identity
                    check_login_identity(wizard.values, identity)
                except SetupPending as error:
                    self._question(state, key, 'Correct the selected login identity: ' + str(error), [
                        Field('profile', 'Named AWS login-only profile', selected['profile'],
                              help='Select a profile for the saved account/principal; no credentials or privilege fallback.'),
                        Field('cache', 'Absolute private login-cache directory', str(cache),
                              help='Private local token folder; daily-use and maintenance folders must remain separate.')], step,
                        description=str(error) + '\nExpected account: ' + expected_account(wizard.values))
                    return
                except Exception as error:
                    if not self._expired_login_handoff(state, error, values=wizard.values, deploy=False):
                        raise
                    state['state'] = 'blocked'
                    return
                state[key + '_actual'] = identity
                state['presentation'].update(account=identity['Account'], principal=identity['Arn'],
                                             profile=selected['profile'], verified=True)
                state['live_state_checked'] = True
                self._review(state, key + '_approval', 'Confirm actual selected AWS principal',
                             {'identity': identity, 'profile': selected['profile'], 'cache': str(cache), 'region': wizard.values['MAVEN_AWS_REGION']}, key + '_accept')
            elif step in ('identity_accept', 'deploy_identity_accept'):
                from tools.setup_common import inspect_login_identity, verify_deployment_access
                deploy = step.startswith('deploy')
                key = 'deploy_identity' if deploy else 'identity'
                identity = verify_deployment_access(wizard.values) if deploy else inspect_login_identity(wizard.values)
                from tools.setup_common import check_login_identity
                check_login_identity(wizard.values, identity, deploy=deploy)
                review = {'identity': identity, **answers[key], 'region': wizard.values['MAVEN_AWS_REGION']}
                self._approved(state, key + '_approval', review)
                wizard.settings({('MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_') + 'EXPECTED_PRINCIPAL': identity['Arn']})
                state['effect_sequence'] = state.get('effect_sequence', 0) + 1
                if deploy and state.get('identity_return_step'):
                    state['step'] = state.pop('identity_return_step')
                    if state['step'] in ('discover', 'identity'):
                        state['connection_maintenance_confirmed'] = True
                elif not deploy and state['parameters'].get('mode') == 'update':
                    state['step'] = 'update_schema'
                elif deploy:
                    state['step'] = 'provision' if answers['selection']['mode'] == 'new' else 'connect_review'
                elif answers['selection']['mode'] == 'new':
                    state.update(step='deploy_identity', identity_return_step='provision')
                elif not wizard.values.get('MAVEN_MEMORY_INSTANCE_ID') and not state.get('connection_maintenance_confirmed'):
                    state.update(step='deploy_identity', identity_return_step=(
                        'connect_review' if wizard.values.get('MAVEN_STACK_NAME') else 'discover'))
                elif not any(key in wizard.values for key in _DEPLOY_KEYS):
                    state.update(step='deploy_identity', identity_return_step='connect_review')
                else:
                    state['step'] = 'connect_review'
            elif step == 'discover':
                candidates = _discover_stacks(wizard.values)
                state['discovered_stacks'] = candidates
                state['live_state_checked'] = True
                self._question(state, 'discovered_stack', 'Choose MAVEN memory found in AWS', [
                    Field('stack_id', 'MAVEN CloudFormation stack', kind='select',
                          choices=tuple((f'{item["name"]} — instance {item["instance_id"]} — {item["status"]}',
                                         item['stack_id']) for item in candidates),
                          help='Choose explicitly, even if only one instance was found. Only ready stacks with valid MAVEN outputs are listed.')],
                    'discovery_select',
                    description=f'Account: {expected_account(wizard.values)} | Region: {wizard.values["MAVEN_AWS_REGION"]}. '
                                'Read-only discovery is complete. Continue saves the selected stack locator locally, '
                                'then shows the complete connection details for review before binding memory. '
                                'No deployment, database initialization or authority replacement is authorized.',
                    details=json.dumps(candidates, indent=2))
            elif step == 'discovery_select':
                selected = next(item for item in state['discovered_stacks']
                                if item['stack_id'] == answers['discovered_stack']['stack_id'])
                # Pin the ARN, not a reusable name. Connection review and binding
                # both re-read this exact stack using confirmed maintenance access.
                wizard.settings({'MAVEN_STACK_NAME': selected['stack_id']})
                state['effect_sequence'] = state.get('effect_sequence', 0) + 1
                self._receipt(state, step, selected)
                state['step'] = 'connect_review' if state.get('identity_actual') else 'identity'
            elif step == 'connect_review':
                if wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                    config = _connection_config(wizard.values)
                else:
                    self._preflight(state, wizard.values, 'metadata')
                    cloud = selected_aws_session(wizard.values, deploy=True, region=wizard.values['MAVEN_AWS_REGION']).client('cloudformation', verify=transport_ca_bundle())
                    try:
                        stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                        if len(stacks) != 1:
                            raise SetupPending('Select exactly one existing stack.')
                        config = bootstrap._outputs(stacks[0])
                    finally:
                        cloud.close()
                expected_account({**wizard.values, **{'MAVEN_MEMORY_' + key.upper(): value
                    for key, value in config.items() if key.endswith('_arn')}})
                self._review(state, 'connect', 'Bind existing instance without deployment or initialization', config, 'connect')
            elif step == 'connect':
                review = state['approvals'][-1]['review']
                self._approved(state, 'connect', review)
                if not wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                    bootstrap.connect(wizard.values, wizard.env, confirm_instance=review['instance_id'], reviewed_config=review)
                    wizard.values = load_environment(wizard.env)
                if _connection_config(wizard.values) != review:
                    raise SetupPending('Selected connection changed after review.')
                self._receipt(state, step, {'instance_id': review['instance_id'], 'connected': True})
                from tools.memory.validation import SCHEMA_VERSION
                self._question(state, 'upgrade', 'Optional database schema upgrade', [Field(
                    'migrate', 'Review a database schema upgrade (SQL changes) before finishing setup', False, kind='bool',
                    help='Leave unchecked to finish setup without changing the database.')], 'upgrade_choice',
                    description=f'Instance {review["instance_id"]} records schema version {review["schema_version"]}; '
                                f'this MAVEN release uses schema version {SCHEMA_VERSION}.\n\n'
                                'Unchecked: skip the upgrade and verify the connection with a read-only status '
                                'request. No database or cloud changes.\n\n'
                                'Checked: run a read-only SQL permission check, then show the exact migration SQL '
                                'for review. That SQL changes the database schema, but only after you approve '
                                'that separate review; you can still decline there.\n\n'
                                'Continue on this screen only records your choice.')
            elif step == 'upgrade_choice':
                state['step'] = 'migration_review' if answers['upgrade']['migrate'] else 'verify'
            elif step == 'provision':
                routine, deploy = state['identity_actual'], state['deploy_identity_actual']
                if (routine['Account'] != deploy['Account'] or not re.fullmatch(
                        r'arn:aws:iam::' + re.escape(routine['Account']) + r':user/[A-Za-z0-9+=,.@_/-]+', routine['Arn'])):
                    raise SetupPending('Fresh provisioning requires an exact same-account IAM user routine principal and deployment identity.')
                from tools.setup_common import provision_fields
                self._question(state, 'provision', 'Choose new instance settings',
                               provision_fields(), 'prepare',
                               description='Editable suggestions for us-east-1; verify actual zone/engine availability and prices. Continue prepares local artifacts and requests a CloudFormation change-set preview. It does not deploy billable resources; deployment requires the next approval.',
                               submit='Prepare deployment review')
            elif step == 'prepare':
                wizard.settings({**answers['provision'], 'MAVEN_ACCOUNT_ID': state['identity_actual']['Account'],
                                 'MAVEN_CALLER_PRINCIPAL_ARN': state['identity_actual']['Arn'],
                                 'MAVEN_INSTANCE_ID': wizard.values.get('MAVEN_INSTANCE_ID') or str(uuid4()),
                                 'MAVEN_RECOVERY_EPOCH': wizard.values.get('MAVEN_RECOVERY_EPOCH') or str(uuid4())})
                candidate = _digest(answers['provision'])
                if state.get('package_candidate') != candidate:
                    state['package_path'] = 'package' if not state.get('package_candidate') else 'package.' + candidate
                    state['package_candidate'] = candidate
                package = wizard.directory / state['package_path']
                if not package.exists():
                    bootstrap.prepare(wizard.values, package)
                manifest, template, parameters = bootstrap._package(package)
                state['new_instance_id'] = manifest['deployment_inputs']['InstanceId']
                review = Wizard.package_review(package, manifest, template, parameters)
                state['presentation']['target'] = wizard.values['MAVEN_STACK_NAME'] + '; instance ' + state['new_instance_id']
                self._preflight(state, wizard.values, 'infrastructure')
                if state.get('preparation_requested') == _digest(answers['provision']):
                    state['prepared_plan'] = _digest(review)
                    state['step'] = 'plan'
                else:
                    self._review(state, 'plan', 'Prepare the requested deployment preview; do not deploy resources', review, 'plan')
            elif step in ('plan', 'apply_review', 'apply'):
                package, path = wizard.directory / state.get('package_path', 'package'), wizard.directory / 'change-set.json'
                manifest, template, parameters = bootstrap._package(package)
                body = Wizard.package_review(package, manifest, template, parameters)
                if step == 'plan':
                    if state.get('preparation_requested') == _digest(answers.get('provision', {})):
                        if state.get('prepared_plan') != _digest(body):
                            raise SetupPending('Prepared package changed before preview; revise the unsubmitted inputs rather than reuse this request.')
                    else:
                        self._approved(state, 'plan', body)
                    result = bootstrap.plan(wizard.values, package, path, change_type='CREATE')
                    self._receipt(state, step, result)
                    state['step'] = 'apply_review'
                else:
                    review = {'package': body, 'change_set': read_private(path)}
                    if step == 'apply_review':
                        self._preflight(state, wizard.values, 'infrastructure')
                        self._review(state, 'apply', 'Execute exact billable CloudFormation change set', review, 'apply')
                    else:
                        self._approved(state, 'apply', review)
                        result = bootstrap.apply(wizard.values, package, path, wizard.env,
                                                 confirm_review=review['change_set']['review_sha256'])
                        self._receipt(state, step, result)
                        state['step'] = 'initialize_review'
            elif step in ('initialize_review', 'migration_review', 'initialize', 'migrate'):
                upgrade = step.startswith('migra')
                from importlib.resources import files
                from tools.memory.admin import MIGRATION_FILES
                if upgrade and (state['parameters'].get('mode') != 'update'
                                or state.get('update_return_step') == 'update_service'):
                    # A pre-cutover update can resume at an old SQL review. Do not
                    # seed an erasure ledger until its bucket policy and serving gate exist.
                    from tools.update import schema_status
                    if '026_reviewed_redaction.sql' in schema_status(wizard.values).get('pending', ()):
                        if state['parameters'].get('mode') != 'update':
                            raise SetupPending('The redaction gate and bucket protections must be deployed before this schema. '
                                               'Use the reviewed setup update; this operation made no SQL change.')
                        state.pop('update_return_step')
                        state.update(step='update_service', service_return_step='migration_review')
                        return
                review = {'instance': _connection_config(wizard.values), 'environment': dict(wizard.values), 'upgrade': upgrade,
                          'sql': {name: files('tools.memory').joinpath('migrations', name).read_text(encoding='utf-8') for name in MIGRATION_FILES}}
                # Corrected pending SQL needs a new exact review, not the old approval or a new operation.
                changed_upgrade = upgrade and state['answers'].get('schema', {}).get('review_digest') != _digest(review)
                if step.endswith('_review') or changed_upgrade:
                    self._preflight(state, wizard.values, 'sql')
                    self._review(state, 'schema', 'Review exact SQL ' + ('upgrade' if upgrade else 'initialization'), review, 'migrate' if upgrade else 'initialize')
                else:
                    self._approved(state, 'schema', review)
                    # Older ordered upgrades verify after service cutover; redaction cutover deploys the gate first.
                    result = bootstrap.initialize(wizard.values, confirm_instance=review['instance']['instance_id'], upgrade=upgrade,
                                                  verify_service=not state.get('update_return_step'))
                    self._receipt(state, step, result)
                    if upgrade and state.get('update_return_step'):
                        state['step'] = state.pop('update_return_step')
                    elif upgrade and wizard.values.get('MAVEN_MEMORY_SCHEMA_VERSION') == '1':
                        state['step'] = 'migration_reconnect'
                    else:
                        state['step'] = 'verify'
            elif step == 'migration_reconnect':
                if not wizard.values.get('MAVEN_STACK_NAME'):
                    self._question(state, 'upgraded_stack', 'Migration completed; select actual upgraded service stack', [
                        Field('stack', 'Exact CloudFormation stack name or ARN')], 'migration_stack')
                else:
                    state['step'] = 'migration_stack'
            elif step == 'migration_stack':
                if 'upgraded_stack' in answers:
                    wizard.settings({'MAVEN_STACK_NAME': answers['upgraded_stack']['stack']})
                if not state.get('deploy_identity_actual'):
                    state['identity_return_step'] = 'migration_stack'
                    state['step'] = 'deploy_identity'
                    return
                self._preflight(state, wizard.values, 'metadata')
                cloud = selected_aws_session(wizard.values, deploy=True, region=wizard.values['MAVEN_AWS_REGION']).client('cloudformation', verify=transport_ca_bundle())
                try:
                    stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                    if len(stacks) != 1:
                        raise SetupPending('Select exactly one upgraded stack.')
                    config = bootstrap._outputs(stacks[0])
                finally:
                    cloud.close()
                expected_account({**wizard.values, **{'MAVEN_MEMORY_' + key.upper(): value
                    for key, value in config.items() if key.endswith('_arn')}})
                self._review(state, 'reconnect', 'Review upgraded service outputs; migration alone does not deploy the service', config, 'migration_bind')
            elif step == 'migration_bind':
                config = state['approvals'][-1]['review']
                self._approved(state, 'reconnect', config)
                result = bootstrap.connect(wizard.values, wizard.env, confirm_instance=config['instance_id'], reviewed_config=config)
                if _connection_config(load_environment(wizard.env)) != config:
                    raise SetupPending('Upgraded stack outputs changed after review.')
                self._receipt(state, step, result)
                state['step'] = state.pop('update_return_step', 'verify')
            elif step == 'verify':
                # Optional secret-store access is verified only when that capability is selected.
                verified = wizard.verify_connection()
                self._receipt(state, step, {'connection_verified': verified, 'instance_ready': True,
                                            'secrets_ready': bool(state.get('secrets_ready'))})
                self._complete_setup(state, wizard, verified)
            elif step == 'optional_project' or step.startswith('project_'):
                if step == 'project_apply' or any(receipt['step'].startswith('project_') for receipt in state['receipts']):
                    raise SetupPending('Legacy project effects may be submitted or unresolved. Preserve their exact receipts and reconcile the project separately; installer setup will not create or enable a project.')
                state['step'] = 'verify'
            else:
                raise SetupPending('Unknown setup phase; retain evidence and use the matching implementation.')

    def _refresh_service_candidate(self, state, wizard, directory):
        pending = directory / 'service-change-set.json.pending'
        if state.get('service_apply_started') or (pending.exists() and not (directory / 'service-change-set.json').exists()):
            raise SetupPending('The original service request is submitted or unresolved. Reconcile its exact change set before refreshing.')
        review_path = directory / 'service-change-set.json'
        if review_path.exists():
            from tools.memory import bootstrap
            expected = read_private(review_path)['review']['change_set']
            cloud = bootstrap._cloud(wizard.values)
            try:
                change = bootstrap._describe_changes(cloud, expected['ChangeSetId'], expected['StackId'])
            finally:
                cloud.close()
            if change.get('ExecutionStatus') != 'AVAILABLE' or bootstrap._review_fields(change) != expected:
                raise SetupPending('The original change set is not verified unexecuted and unchanged. Reconcile it before refreshing.')
        number = state.get('service_candidate_revision', 0) + 1
        state.setdefault('service_candidates', []).append(str(directory))
        state.update(service_candidate_revision=number, service_candidate_directory=f'service-candidate-{number}',
                     step='update_service', pending_interaction=None)
        self._save(state)

    def _update(self, state, wizard, step, answers):
        """Update an existing installation's local software, schema and service."""
        from tools import update
        from tools.memory import bootstrap
        from tools.memory.environment import load_environment
        service_directory = wizard.directory / state.get('service_candidate_directory', '')
        if step in ('update_local', 'update_local_wait'):
            checkout = state['parameters'].get('checkout') or _installation_source(self.home)
            report = update.local_report(self.home, Path(checkout).expanduser())
            state['update_report'] = {**state.get('update_report', {}), 'local': report}
            if not report['current'] and step == 'update_local':
                software = report['software']
                lines = [f'Checkout: {report["checkout"]}',
                         f'Software files changed/added/removed: {len(software["changed"])}/'
                         f'{len(software["added"])}/{len(software["removed"])}',
                         f'Harness skills to update/add: {len(report["skills"]["stale"])}/{len(report["skills"]["missing"])}',
                         'Harnesses: ' + (', '.join(report['harnesses']) or 'none configured')]
                git = report['git']
                if git.get('repository') and git.get('behind'):
                    lines.append(f'The checkout is {git["behind"]} commit(s) behind {git["upstream"]} as of the last '
                                 'fetch; update the checkout first if you want those changes.')
                self._question(state, 'local_update', 'Update local MAVEN software', [
                    Field('skip', 'Continue without updating local software (cloud steps use the installed version)',
                          False, kind='bool')], 'update_local_choice',
                    description='\n'.join(lines) + '\n\nContinue to get the exact installer command. It runs in a '
                                'separate terminal because it replaces the software this process is using.',
                    details=json.dumps(report, indent=2))
                return
            if not report['current']:
                command = update.install_command(self.home, Path(report['checkout']))
                self._handoff(state, 'Run this installer in a separate terminal and choose "Update local MAVEN '
                              'software". Then restart this harness (or rerun maven-setup) so it loads the new '
                              'software, and advance this same operation. Its Review screen lists every change.',
                              command)
                state['handoff']['type'] = 'local_update'
                return
            if update.running_software_stale():
                raise SetupPending('MAVEN was updated after this process started, so it is still running the old '
                                   'software. Restart the harness (or rerun maven-setup), then advance this same operation.')
            self._receipt(state, 'update_local', {'current': True, 'checkout': report['checkout']})
            state['step'] = 'identity'
        elif step == 'update_local_choice':
            if answers['local_update']['skip']:
                self._receipt(state, step, {'local_update': 'skipped by owner'})
                state['step'] = 'identity'
            else:
                state['step'] = 'update_local_wait'
                self._update(state, wizard, 'update_local_wait', answers)
        elif step == 'update_schema':
            status = update.schema_status(wizard.values)
            self._receipt(state, step, status)
            if status['state'] == 'incompatible':
                raise SetupPending('The deployed database schema is not an ordered prefix of this version\'s migrations '
                                   '(applied: ' + ', '.join(status['applied']) + '). Nothing was changed; this '
                                   'software cannot upgrade it. Use the MAVEN version that matches the instance.')
            if '026_reviewed_redaction.sql' in status.get('pending', ()):
                state.update(step='update_service', service_return_step='migration_review')
            elif status['state'] == 'pending':
                state['update_return_step'] = 'update_service'
                state['step'] = 'migration_review'
            else:
                state['step'] = 'update_service'
        elif step == 'update_service':
            if not state.get('deploy_identity_actual'):
                state.update(identity_return_step='update_service', step='deploy_identity')
                return
            self._preflight(state, wizard.values, 'metadata')
            cloud = bootstrap._cloud(wizard.values)
            try:
                stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                if len(stacks) != 1 or stacks[0].get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
                    raise SetupPending('The service stack is not in a completed state (' +
                                       str(stacks[0].get('StackStatus') if stacks else 'not found') +
                                       '). Resolve it in CloudFormation, then advance this operation.')
                stack = stacks[0]
                deployed = cloud.get_template(StackName=stack['StackId'], TemplateStage='Original')['TemplateBody']
            finally:
                cloud.close()
            deployed = json.loads(deployed) if isinstance(deployed, str) else deployed
            private_directory(service_directory)
            package = service_directory / 'service-package'
            if not package.exists():
                try:
                    update.build_service_package(stack, package, wizard.values['MAVEN_MEMORY_INSTANCE_ID'])
                except ValueError as error:
                    raise SetupPending('Cannot rebuild the service with the stack\'s deployment inputs: ' + str(error)) from None
            manifest, template, _ = bootstrap._package(package)
            if any(update.stack_parameters(stack).get(key) != value
                   for key, value in manifest['deployment_inputs'].items()):
                self._refresh_service_candidate(state, wizard, service_directory)
                return
            if json.loads(template) == deployed:
                live = bootstrap._outputs(stack)
                self._receipt(state, step, {'service': 'current', 'stack': stack['StackId']})
                if live != _connection_config(wizard.values):
                    state['update_return_step'] = state.pop('service_return_step', 'verify')
                    self._review(state, 'reconnect', 'Review current service outputs to reconnect this installation',
                                 live, 'migration_bind')
                else:
                    state['step'] = state.pop('service_return_step', 'verify')
                return
            review_path = service_directory / 'service-change-set.json'
            bootstrap.plan(wizard.values, package, review_path, change_type='UPDATE')
            document = read_private(review_path)
            risks = update.stateful_risks(document)
            if risks:
                raise SetupPending('The service update would remove or replace resources that hold memory data or '
                                   'credentials: ' + ', '.join(f'{row["logical_id"]} ({row["type"]})' for row in risks) +
                                   '. Nothing was executed. The unexecuted change set is kept for inspection; '
                                   'this update must be redesigned before it can be applied safely.')
            review = {'stack': stack['StackId'], 'instance_id': wizard.values['MAVEN_MEMORY_INSTANCE_ID'],
                      'review_sha256': document['review_sha256'], 'changes': update.change_summary(document),
                      'change_set': document['review']}
            self._review(state, 'service_update', 'Review MAVEN service update (CloudFormation change set)',
                         review, 'update_service_apply')
        elif step == 'update_service_apply':
            review = state['approvals'][-1]['review']
            self._approved(state, 'service_update', review)
            if not state.get('service_apply_started'):
                manifest, _, _ = bootstrap._package(service_directory / 'service-package')
                cloud = bootstrap._cloud(wizard.values)
                try:
                    stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                finally:
                    cloud.close()
                if len(stacks) != 1:
                    raise SetupPending('Select exactly one reviewed service stack.')
                if any(update.stack_parameters(stacks[0]).get(key) != value
                       for key, value in manifest['deployment_inputs'].items()):
                    self._refresh_service_candidate(state, wizard, service_directory)
                    return
                state['service_apply_started'] = True
                self._save(state)
            result = bootstrap.apply(wizard.values, service_directory / 'service-package',
                                     service_directory / 'service-change-set.json', wizard.env,
                                     confirm_review=review['review_sha256'])
            wizard.values = load_environment(wizard.env)
            self._receipt(state, step, result)
            state['step'] = state.pop('service_return_step', 'verify')
        else:
            raise SetupPending('Unknown update phase; retain evidence and use the matching implementation.')


    def database_status(self):
        """Read-only database wake probe for this home's saved connection; never advances an operation."""
        from tools.memory.environment import load_environment
        from tools.setup_common import database_status
        return database_status(load_environment(self.home / '.env'))

    def _project_binding(self):
        from tools.memory.environment import load_environment, memory_config
        values = load_environment(self.home / '.env')
        return {'connection': memory_config(values), 'environment': values}

    def _project(self, state):
        step = state['step']
        if step in ('start', 'project_selection'):
            self._question(state, 'project', 'Select, switch or rename an explicit project; Git is not required', [
                Field('root', 'Absolute existing project directory', state['parameters'].get('root', '')),
                Field('mode', 'Project selection', 'existing', kind='select', choices=(
                    ('Existing project', 'existing'), ('Create project', 'create'),
                    ('Create cloud project only; leave this folder association unchanged', 'create_cloud'),
                    ('Rebind this folder to an exact project and instance (preserve the old local records)', 'switch'),
                    ('Rename this folder’s existing cloud project across all machines', 'rename'))),
                Field('project_id', 'Existing project UUID (blank when creating)', required=False),
                Field('name', 'New project name (create, create_cloud or rename only)', required=False)], 'project_review')
        elif step == 'project_review':
            review = {'selection': state['answers']['project'], 'binding': self._project_binding()}
            if review['selection']['mode'] == 'create':
                review['new_project_id'] = str(uuid5(UUID(state['operation_id']), 'new-project'))
            if review['selection']['mode'] == 'switch':
                from tools.project_switch import prepare_switch
                review['switch'] = prepare_switch(
                    self.home, review['selection']['root'], review['selection']['project_id'],
                    state['operation_id'], review['binding'])
            elif review['selection']['mode'] == 'rename':
                from tools.project_rename import prepare_rename
                selected = review['selection']
                review['rename'] = prepare_rename(
                    self.home, selected['root'], selected['project_id'], selected['name'],
                    state['operation_id'], review['binding'])
            elif review['selection']['mode'] == 'create_cloud':
                from tools.project_create import prepare_create
                selected = review['selection']
                review['create_cloud'] = prepare_create(
                    self.home, selected['root'], selected['name'], state['operation_id'], review['binding'])
            values = review['binding']['environment']
            from tools.setup_common import expected_account
            state['presentation'] = {'title': 'Standalone project enablement', 'purpose': 'Daily-use memory access',
                'account': expected_account(values), 'profile': values.get('MAVEN_AWS_PROFILE', 'Not selected'),
                'principal': values.get('MAVEN_AWS_EXPECTED_PRINCIPAL', 'Unknown'), 'verified': False,
                'region': values.get('MAVEN_AWS_REGION', 'us-east-1'),
                'target': review['binding']['connection']['instance_id'], 'description': '', 'details': ''}
            self._preflight(state, values, 'routine')
            title = ('Approve exact cloud project rename across all machines; UUID, folder, memory and Home selection unchanged'
                     if review['selection']['mode'] == 'rename' else
                     'Approve exact cloud-only project creation; local association unchanged until a separately reviewed switch'
                     if review['selection']['mode'] == 'create_cloud' else
                     'Approve exact project, instance and selected AWS identity; no Git mutation')
            self._review(state, 'project', title, review, 'project_apply')
        elif step == 'project_apply':
            from tools.project_setup import setup_project
            review = state['approvals'][-1]['review']
            selected = review['selection']
            binding = self._project_binding()
            source = self._approved(state, 'project', {**review, 'binding': binding})
            if selected['mode'] == 'create' and not review.get('new_project_id'):
                raise SetupPending('Legacy review lacks a pinned project UUID; no creation request was sent.')
            if selected['mode'] == 'create_cloud':
                from tools.project_create import apply_create
                result = apply_create(self.home, review['create_cloud'], binding, source,
                                      state.setdefault('create_cloud_progress', {}), lambda: self._save(state))
                self._receipt(state, step, result)
                state.update(state='completed', outcome=result, live_state_checked=True)
                return
            if selected['mode'] == 'rename':
                from tools.project_rename import apply_rename
                result = apply_rename(self.home, review['rename'], binding, source,
                                      state.setdefault('rename_progress', {}), lambda: self._save(state))
                self._receipt(state, step, result)
                state.update(state='completed', outcome=result, live_state_checked=True)
                return
            if selected['mode'] == 'switch':
                from tools.project_switch import apply_switch
                result = apply_switch(self.home, review['switch'], binding, source)
                self._receipt(state, step, result)
                state.update(state='completed', outcome=result)
                return
            if not Path(selected['root']).is_absolute():
                raise SetupPending('Project root must be absolute.')
            result = setup_project(argparse.Namespace(
                root=Path(selected['root']), env=self.home / '.env', non_interactive=True,
                new_project_id=review.get('new_project_id'),
                create=selected['mode'] == 'create', project_id=selected['project_id'] or None,
                name=selected['name'] or None, source_origin=source['origin'] if selected['mode'] == 'create' else None,
                source_excerpt=source['excerpt'] if selected['mode'] == 'create' else None,
                remote=None, accepted_url=None, branch=None, check=[],
                reviewed_environment=binding['environment']))
            self._receipt(state, step, result)
            state.update(state='completed', outcome=result)

    def _home_selection(self, answer):
        """Normalize one Home form answer; local checks only, no cloud access."""
        from tools.home import HomeError, validate_root
        from tools.project import load_project
        mode = answer.get('mode')
        if mode not in ('enabled', 'existing', 'create', 'switch'):
            raise HomeError('Choose how the Home folder is selected.')
        root = Path(str(answer.get('root', '')).strip()).expanduser()
        if not root.is_absolute():
            raise HomeError('Home folder must be an absolute path')
        if mode == 'create' and not root.exists() and not root.is_symlink():
            parent = root.parent
            if not parent.is_dir():
                raise HomeError('The parent folder of a new Home folder must exist')
            # A new child of the user home is allowed; any other parent gets the full Home checks.
            if parent.resolve() != Path.home().resolve():
                parent = validate_root(self.home, parent)
            root = parent.resolve() / root.name
        else:
            root = validate_root(self.home, root)
        project_id = str(answer.get('project_id', '')).strip()
        enabled = (root / '.maven/project.json').exists() or (root / '.maven/project.json').is_symlink()
        if mode == 'enabled':
            if not enabled:
                raise HomeError('This folder is not an enabled MAVEN project; choose another option.')
            from tools.project import require_binding
            project = load_project(root)
            require_binding(project, self._project_binding()['connection'])
            current = project['project_id']
            if project_id and project_id != current:
                raise HomeError('This folder belongs to a different MAVEN project than the UUID entered.')
            project_id = current
        elif mode == 'switch':
            from tools.project import _uuid
            if not enabled:
                raise HomeError('Switch requires an already enabled MAVEN project folder.')
            _uuid(project_id)
        elif enabled:
            raise HomeError('This folder is already enabled; use its current project or explicitly choose Switch to a different existing project.')
        elif mode == 'existing':
            try:
                project_id = str(UUID(project_id))
            except ValueError:
                raise HomeError('Enter the exact existing MAVEN project UUID.') from None
        else:
            if project_id:
                raise HomeError('A new project gets its UUID when it is created; leave the project UUID blank.')
            if root.exists() and any(root.iterdir()):
                raise HomeError('A new Home project needs a new or empty folder.')
            project_id = None
        label = str(answer.get('label', '')).strip() or root.name
        return {'mode': mode, 'root': str(root), 'project_id': project_id, 'label': label,
                'replace': answer.get('replace') is True}

    def _home_client(self, values=None):
        from tools.memory.environment import configured_client
        return configured_client(self.home / '.env', values=values)

    def _home_projects(self):
        """Universal project entities on this instance that carry the maven-home tag."""
        client = self._home_client()
        try:
            records = []
            seen = set()
            request = {'kind': 'entity', 'tag': HOME_TAG, 'scope': 'universal', 'limit': 20}
            for _ in range(100):
                page = client.invoke('/v1/memory/search', request)['data']
                rows = page.get('records', [])
                identifiers = {row['record_id'] for row in rows}
                if len(identifiers) != len(rows) or seen & identifiers:
                    raise SetupPending('Home discovery did not advance; no complete replacement review is available.')
                for row in rows:
                    record = client.invoke('/v1/memory/read', {'record_id': row['record_id']})['data']
                    if record['record_id'] != row['record_id'] or record['revision'] != row['revision']:
                        raise SetupPending('Home discovery changed during review; refresh the candidate inventory.')
                    records.append(record)
                seen.update(identifiers)
                cursor = page['next_after_id']
                if cursor is None:
                    break
                if not rows or cursor != rows[-1]['record_id']:
                    raise SetupPending('Home discovery returned an inconsistent continuation.')
                request['after_id'] = cursor
            else:
                raise SetupPending('Home discovery exceeds the bounded review; narrow the tagged project inventory before replacement.')
        finally:
            client.close()
        return sorted(({'project_id': record['record_id'], 'name': record.get('data', {}).get('name')}
                       for record in records
                       if record.get('data', {}).get('entity_type') == 'project'
                       and HOME_TAG in (record.get('tags') or [])), key=lambda item: item['project_id'])

    @staticmethod
    def _home_conflicts(project_id, homes, current):
        """Other projects already serving as Home: tagged on the instance or selected on this machine."""
        others = {item['project_id'] for item in homes if item['project_id'] != project_id}
        if current and 'invalid' not in current and current['project_id'] != project_id:
            others.add(current['project_id'])
        return sorted(others)

    def _tag_home(self, state, project_id, source, values):
        """Write the next entity revision with the maven-home tag; kind, scope and data unchanged."""
        client = self._home_client(values)
        try:
            record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
            if (record.get('record_id') != project_id or record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                    or record.get('project_id') is not None
                    or record.get('data', {}).get('entity_type') != 'project'
                    or 'archived' in record.get('tags', []) or record.get('data', {}).get('state') == 'archived'):
                raise SetupPending('The exact active Home project was not found; it was not tagged.')
            tags = list(record.get('tags') or [])
            if HOME_TAG in tags:
                return {'status': 'unchanged', 'record_id': project_id, 'revision': record['revision']}
            client.invoke('/v1/memory/write', {
                'request_key': str(uuid5(UUID(state['operation_id']), f'home-tag:{project_id}:{record["revision"]}')),
                'record_id': project_id, 'expected_revision': record['revision'], 'kind': 'entity',
                'classification': 'Sensitive', 'data_scope': record['data_scope'], 'data': record['data'],
                'tags': [*tags, HOME_TAG], 'source': {'origin': source['origin'], 'excerpt': source['excerpt']},
                'change_reason': 'Mark the owner-approved MAVEN Home project',
            })
        finally:
            client.close()
        return {'status': 'tagged', 'record_id': project_id, 'previous_revision': record['revision']}

    def _home(self, state):
        from tools.home import HOME_FILE, HomeError, load_home, remove_home, save_home, validate_root
        from tools.project import load_project
        step, parameters = state['step'], state['parameters']
        path = str(self.home / HOME_FILE)
        if parameters.get('action', 'enable') == 'disable':
            from tools.uninstall.common import read_private as read_selection
            current = load_home(self.home)
            review = {'action': 'disable', 'remove': [path], 'current_home': current,
                      'unchanged': 'Cloud memory, the project entity and its maven-home tag, and the project folder are not changed.'}
            review['selection_digest'] = _digest(read_selection(Path(path))) if current is not None else None
            if step == 'start':
                if current is None:
                    state.update(state='completed', pending_interaction=None, outcome={
                        'status': 'unchanged', 'removed': [], 'reason': 'MAVEN Home is not set up on this machine.'})
                    return
                state['presentation'] = {'title': 'Turn off MAVEN Home', 'purpose': 'Local Home selection only',
                                         'target': path, 'description': '', 'details': ''}
                self._review(state, 'home_disable', 'Turn off MAVEN Home on this machine: remove only ' + path,
                             review, 'home_disable')
            elif step == 'home_disable':
                approved = state['approvals'][-1]['review']
                self._approved(state, 'home_disable', approved)
                same = current == approved['current_home'] and review['selection_digest'] == approved.get('selection_digest')
                if not same and not (current is None and state.get('home_remove_intent')):
                    raise SetupPending('The local Home selection changed after review; nothing was removed.')
                if not state.get('home_remove_intent'):
                    state['home_remove_intent'] = {'current_home': current, 'path': path}
                    self._save(state)
                remove_home(self.home)
                result = {'status': 'removed', 'removed': [path], 'cloud_changed': False}
                self._receipt(state, step, result)
                state.update(state='completed', outcome=result)
            return
        if step in ('start', 'home_selection'):
            from tools.memory.client import MemoryClientError
            try:
                homes, lookup = self._home_projects(), ''
            except (MemoryClientError, SetupPending, OSError) as error:
                homes, lookup = [], ' The optional Home lookup was unavailable (' + _error_code(error) + ').'
            root = parameters.get('root', '')
            enabled = bool(root) and Path(root).is_absolute() and (Path(root) / '.maven/project.json').is_file()
            mode = 'enabled' if enabled else 'existing' if homes else 'create'
            found = ''.join(f'\nExisting MAVEN Home on this instance: {item["name"] or "unnamed"} ({item["project_id"]})'
                            for item in homes)
            self._question(state, 'home', 'Choose the MAVEN Home folder', [
                Field('mode', 'Home folder', mode, kind='select', choices=(
                    ('Use an already enabled project folder', 'enabled'),
                    ('Rebind this folder to an exact MAVEN project and instance', 'switch'),
                    ('Enable an existing folder for an existing MAVEN project (another machine)', 'existing'),
                    ('Create a new project in a new or empty folder', 'create'))),
                Field('root', 'Absolute Home folder, outside the MAVEN installation', root),
                Field('project_id', 'Existing project UUID (blank for an enabled folder or a new project)',
                      parameters.get('project_id') or (homes[0]['project_id'] if len(homes) == 1 else ''),
                      required=False),
                Field('label', 'Home label (blank uses the folder name)', parameters.get('label', ''), required=False),
                Field('replace', 'Replace a different project that is already MAVEN Home', False, kind='bool',
                      help='Required when another project is already Home. Its maven-home tag is left unchanged.'),
            ], 'home_review', description='MAVEN Home is a distinct, explicitly selected project on this machine. '
                'Nothing is changed until you approve the exact review.' + found + lookup)
        elif step == 'home_review':
            try:
                new_project_id = str(uuid5(UUID(state['operation_id']), 'new-home'))
                selection = self._home_selection(state['answers']['home'])
            except (ValueError, OSError) as error:
                raise SetupPending(str(error) if isinstance(error, ValueError) else 'Home folder is not accessible.') from None
            homes, current = self._home_projects(), load_home(self.home)
            if current and 'invalid' in current:
                raise SetupPending('Existing Home selection is invalid; review its explicit removal before replacing it.')
            others = self._home_conflicts(selection['project_id'], homes, current)
            if others and not selection['replace']:
                raise SetupPending('A different project is already MAVEN Home (' + ', '.join(others) + '). '
                                   'Go back and select it, or explicitly choose Replace.')
            binding = self._project_binding()
            instance = binding['connection']['instance_id']
            consent = (f'Use {selection["root"]} as MAVEN Home ("{selection["label"]}") on this machine for '
                       + (f'new project {new_project_id}' if selection['mode'] == 'create'
                          else f'project {selection["project_id"]}')
                       + f' on MAVEN instance {instance}. Home memory is distinct from other projects. '
                       f'This approval is recorded in {path} and the project entity '
                       'gets the maven-home tag; its data is unchanged.')
            review = {'action': 'enable', 'selection': selection, 'instance_id': instance, 'home_file': path,
                      'consent': consent, 'replaces': others, 'current_home': current, 'binding': binding}
            if selection['mode'] == 'create':
                review['new_project_id'] = new_project_id
            if selection['mode'] == 'switch':
                from tools.project_switch import prepare_switch
                review['switch'] = prepare_switch(
                    self.home, selection['root'], selection['project_id'], state['operation_id'],
                    binding, home_selection=selection)
            values = binding['environment']
            from tools.setup_common import expected_account
            state['presentation'] = {'title': 'MAVEN Home', 'purpose': 'Daily-use memory access',
                'account': expected_account(values), 'profile': values.get('MAVEN_AWS_PROFILE', 'Not selected'),
                'principal': values.get('MAVEN_AWS_EXPECTED_PRINCIPAL', 'Unknown'), 'verified': False,
                'region': values.get('MAVEN_AWS_REGION', 'us-east-1'), 'target': selection['root'],
                'description': '', 'details': ''}
            self._preflight(state, values, 'routine')
            self._review(state, 'home_review', 'Approve MAVEN Home: exact folder, project, instance and consent',
                         review, 'home_apply')
        elif step == 'home_apply':
            from tools.project_setup import setup_project
            reviewed = state['approvals'][-1]['review']
            source = self._approved(state, 'home_review', {**reviewed, 'binding': self._project_binding()})
            selection = reviewed['selection']
            root = Path(selection['root'])
            prior = next((receipt['result'] for receipt in state['receipts'] if receipt['step'] == 'home_project'), None)
            project_id = selection['project_id'] or reviewed.get('new_project_id')
            if not project_id or (prior and project_id != prior.get('project_id')):
                raise SetupPending('Home selection lacks an exact matching project UUID; preserve the original review.')
            current = load_home(self.home)
            intent = state.get('home_write_intent')
            own_selection = intent is not None and current == intent
            switching = selection['mode'] == 'switch' and state.get('home_transition_started')
            if not own_selection and not switching:
                if current != reviewed['current_home']:
                    raise SetupPending('The local Home selection changed after review; preserve it and review the new exact target.')
                others = self._home_conflicts(selection['project_id'], self._home_projects(), current)
                if others != reviewed['replaces']:
                    raise SetupPending('The Home replacement conflicts changed after review; review the new exact targets.')
            if not state.get('home_transition_started'):
                state['home_transition_started'] = True
                self._save(state)
            try:
                if prior is None:
                    if selection['mode'] == 'enabled':
                        from tools.project import require_binding
                        project = load_project(root)
                        require_binding(project, reviewed['binding']['connection'])
                        if project['project_id'] != project_id:
                            raise SetupPending('The reviewed folder now belongs to a different project; nothing was changed.')
                        result = {'status': 'unchanged', 'root': str(root)}
                    elif selection['mode'] == 'switch':
                        from tools.project_switch import apply_switch
                        result = apply_switch(self.home, reviewed['switch'], reviewed['binding'], source)
                    else:
                        create = selection['mode'] == 'create'
                        if create and not root.exists():
                            root.mkdir()
                        validate_root(self.home, root)
                        result = setup_project(argparse.Namespace(
                            root=root, env=self.home / '.env', non_interactive=True, create=create,
                            new_project_id=reviewed.get('new_project_id'),
                            project_id=None if create else project_id, name=selection['label'] if create else None,
                            source_origin=source['origin'] if create else None,
                            source_excerpt=source['excerpt'] if create else None,
                            remote=None, accepted_url=None, branch=None, check=[],
                            reviewed_environment=reviewed['binding']['environment']))
                        if project_id != result['project']['project_id']:
                            raise SetupPending('Home setup returned a different project than the reviewed UUID; no Home was selected.')
                    prior = {'project_id': project_id, 'setup': result}
                    self._receipt(state, 'home_project', prior)
                    self._save(state)
                if selection['mode'] == 'switch':
                    record = load_home(self.home)
                    if (not record or 'invalid' in record or record['project_id'] != project_id
                            or record['root'] != str(root) or record['label'] != selection['label']
                            or record['instance_id'] != reviewed['instance_id'] or record['consent'] != source):
                        raise SetupPending('Switched Home selection changed; recover the exact original switch.')
                else:
                    if intent is None:
                        from datetime import datetime, timezone
                        intent = {'schema': 1, 'root': str(root), 'project_id': project_id,
                                  'instance_id': reviewed['instance_id'], 'label': selection['label'],
                                  'recovery_epoch': reviewed['binding']['connection']['recovery_epoch'],
                                  'consent': source, 'enabled_at': datetime.now(timezone.utc).isoformat()}
                        state['home_write_intent'] = intent
                        self._save(state)
                    if load_home(self.home) not in (reviewed['current_home'], intent):
                        raise SetupPending('The local Home selection changed before replacement; preserve both operation receipts.')
                    record = save_home(self.home, root=root, project_id=project_id, instance_id=reviewed['instance_id'],
                                       recovery_epoch=intent['recovery_epoch'],
                                       label=selection['label'], consent=source, enabled_at=intent['enabled_at'])
            except HomeError as error:
                raise SetupPending(str(error)) from None
            self._receipt(state, 'home_file', record)
            self._save(state)
            tag = self._tag_home(state, project_id, source, reviewed['binding']['environment'])
            self._receipt(state, 'home_tag', tag)
            state.update(state='completed', outcome={'status': 'ready', 'home': record, 'entity_tag': tag})

    def _cleanup_admin(self):
        """The separately assumed administration role, obtained exactly as the schema migration does."""
        from tools.memory.client import selected_session
        from tools.memory.environment import load_environment, memory_config
        values = load_environment(self.home / '.env')
        config = memory_config(values)
        return values, config, selected_session(config, admin=True, environment=values)

    def _cleanup_roots(self):
        """Project roots known to this machine's session registry, plus the Home folder."""
        from tools.coordination import private_lock
        from tools.home import load_home
        from tools.memory.boundary import private_path, read_private
        roots = set()
        if private_path(self.home, 'runtime/sessions.json').exists():
            with private_lock(self.home, 'runtime/registry.lock'):
                registry = read_private(self.home, 'runtime/sessions.json')
            sessions = registry.get('sessions') if registry.get('schema') == 1 else None
            if not isinstance(sessions, dict):
                raise SetupPending('The local session registry is malformed; preserve it for recovery. Nothing was swept.')
            roots.update(item['root'] for item in sessions.values()
                         if isinstance(item, dict) and isinstance(item.get('root'), str))
        home = load_home(self.home)
        if home and 'invalid' not in home:
            roots.add(home['root'])
        return sorted(root for root in roots if Path(root).is_absolute() and Path(root).is_dir())

    def _cleanup_blockers(self, ids):
        """Unresolved local requests for these records; read-only."""
        return [blocker for root in self._cleanup_roots() for session in _session_directories(root)
                for blocker in _sweep_session(root, session, frozenset(ids), ERASED_LOCAL)[1]]

    def _sweep_administration(self, ids, *, current=None, apply=False):
        """Scrub resolved review copies, never unresolved request/approval evidence."""
        ids = frozenset(ids)

        def contains_record(value):
            if isinstance(value, list):
                return any(contains_record(item) for item in value)
            if isinstance(value, dict):
                return value.get('record_id') in ids or any(contains_record(item) for item in value.values())
            return False

        def scrub(value, content_free=False):
            if isinstance(value, list):
                return [scrub(item, content_free) for item in value]
            if not isinstance(value, dict):
                return value
            if value.get('record_id') in ids:
                return {**{key: value[key] for key in
                           ('record_id', 'revision', 'expected_revision', 'request_key', 'operation_id',
                            'receipt_id', 'approval_digest', 'review_digest', 'kind', 'versions')
                           if key in value}, 'erased': True}
            content_free = content_free or (value.get('kind') == 'cleanup' and contains_record(value))
            result = {}
            for key, item in value.items():
                if content_free and key in ('source', 'reason', 'actor'):
                    result[key + '_sha256'] = _digest(item)
                else:
                    result[key] = scrub(item, content_free)
            if content_free and 'review_digest' in value:
                result['content_redacted'] = True
            return result

        updates = []
        for path in sorted(self.directory.glob('*.json')):
            if current is not None and path == self._path(current['operation_id']):
                continue
            document = read_private(path, maximum=64 * 1024 * 1024)
            cleaned = scrub(document)
            if cleaned == document:
                continue
            original = document.get('original', {}).get('operation', document)
            if original.get('state') != 'completed' and not (
                    document.get('completed') is True and original.get('state') == 'abandoned'):
                raise SetupPending('Administration evidence ' + path.name +
                                   ' contains unresolved selected work; reconcile it before erasure. Its original request is preserved.')
            updates.append((path, cleaned))
        if apply:
            for path, cleaned in updates:
                save_json(path, cleaned)
            if current is not None and current.get('state') == 'completed':
                cleaned = scrub(current)
                current.clear()
                current.update(cleaned)
        return [str(path) for path, _ in updates]

    @staticmethod
    def _cleanup_receipt(state, step):
        return next((receipt['result'] for receipt in reversed(state['receipts']) if receipt['step'] == step), None)

    def _cleanup_selection(self, state):
        parameters, answer = state['parameters'], state['answers'].get('cleanup_selection')
        if answer is not None:
            return (_cleanup_ids(re.split(r'[\s,]+', answer['record_ids'].strip())),
                    _cleanup_reason(answer['reason']))
        return _cleanup_ids(parameters['record_ids']), _cleanup_reason(parameters['reason'])

    def _cleanup(self, state):
        """Clean-up review, Forget, Restore and Erase under the administration role.

        Inspection never sweeps files. Forget, Restore and Erase preserve their original
        approval-bound SQL receipts; prior verified erasures may be swept during those actions.
        """
        from tools.memory import admin
        from tools.setup_common import expected_account
        step, parameters = state['step'], state['parameters']
        action = parameters['action']
        if _private_cleanup(state):
            from tools import redaction_administration
            return redaction_administration.advance(self, state)
        if step == 'start':
            values, config, session = self._cleanup_admin()
            state['presentation'] = {
                'title': 'Memory clean-up', 'purpose': 'Administration role: ' + action,
                'account': expected_account(values), 'profile': values.get('MAVEN_AWS_PROFILE', 'Not selected'),
                'principal': values.get('MAVEN_AWS_EXPECTED_PRINCIPAL', 'Unknown'), 'verified': False,
                'region': values.get('MAVEN_AWS_REGION', 'us-east-1'), 'target': config['instance_id'],
                'description': '', 'details': ''}
            self._preflight(state, values, 'sql')
            if action == 'review':
                found = admin.cleanup_candidates(config, session)
                groups = {}
                for entry in found['candidates']:
                    groups.setdefault(entry['suggested'], []).append(entry)
                outcome = {
                    'status': 'ready', 'action': 'review', 'read_only': True, 'cloud_changed': False,
                    'total': len(found['candidates']), 'truncated': bool(found.get('truncated')),
                    'groups': groups, 'scope': 'legacy service records, not reconciled project memory',
                    'next': 'Present the candidates. Forget, Restore and Erase require a separate reviewed '
                            'cleanup operation with the owner-chosen record IDs and reason.'}
                self._receipt(state, 'cleanup_candidates', {'total': outcome['total'],
                                                            'truncated': outcome['truncated']})
                state.update(state='completed', pending_interaction=None, outcome=outcome)
                return
            step = state['step'] = 'cleanup_selection'
        if step == 'cleanup_selection':
            if {'record_ids', 'reason'} <= parameters.keys():
                step = state['step'] = 'cleanup_review'
            else:
                self._question(state, 'cleanup_selection', f'Select the records to {action}', [
                    Field('record_ids', f'Record UUIDs, one per line (at most {CLEANUP_LIMIT})',
                          '\n'.join(parameters.get('record_ids', [])), kind='multiline'),
                    Field('reason', 'Reason recorded in the clean-up receipt (never the content itself)',
                          parameters.get('reason', ''))], 'cleanup_review',
                    description='Nothing is changed until you approve the exact review of these records.')
                return
        if step == 'cleanup_review':
            ids, reason = self._cleanup_selection(state)
            _, config, session = self._cleanup_admin()
            described = {entry['record_id']: entry for entry in admin.cleanup_describe(config, session, ids)}
            missing = [identifier for identifier in ids if not described.get(identifier, {}).get('found')]
            if missing:
                raise SetupPending('These records were not found on this instance: ' + ', '.join(missing)
                                   + '. Begin a new clean-up with a corrected selection; nothing was changed.')
            refused = [f'{identifier} ({described[identifier]["kind"]})' for identifier in ids
                       if described[identifier]['kind'] in _LIFECYCLE_KINDS]
            if refused:
                raise SetupPending('Session, checkpoint and operation records are not cleaned up: '
                                   + ', '.join(refused) + '. Nothing was changed.')
            erased = [identifier for identifier in ids if described[identifier].get('erased')]
            if erased:
                raise SetupPending('Already erased records cannot be forgotten, restored or erased again: '
                                   + ', '.join(erased) + '. Nothing was changed.')
            if action == 'erase':
                blockers = self._cleanup_blockers(ids)
                self._sweep_administration(ids, current=state)
                if blockers:
                    raise SetupPending('Erase refused before review; resolve first:\n' + '\n'.join(blockers))
            records = [described[identifier] for identifier in ids]
            effect = {
                'forget': 'Set each record to forgotten: it disappears from search, startup context and every '
                          'read, including revision reads. History is kept; Restore reverses it.',
                'restore': 'Set each forgotten record back to visible.',
                'erase': 'Rewrite every revision of each record, its current content, its sources\' excerpts, '
                         'fact values and memory receipt results to a content-free tombstone. Irreversible.',
            }[action]
            review = {'action': action, 'instance_id': config['instance_id'], 'reason': reason,
                      'record_ids': ids, 'records': records,
                      'prior_erasure_cache_sweep_ids': sorted(admin.cleanup_erased_ids(config, session)),
                      'versions_affected': sum(record.get('versions', 0) for record in records),
                      'effect': effect, 'caveats': list(ERASE_CAVEATS) if action == 'erase' else [],
                      'receipt': 'An immutable clean-up receipt records the record ids, version counts, actor, '
                                 'reason and this review digest; never content.'}
            self._review(state, 'cleanup_review', f'Approve {action} of {len(ids)} record(s): exact records, '
                         'versions and caveats', review, 'cleanup_apply')
        elif step == 'cleanup_apply':
            if state.get('legacy_cleanup_uncertain'):
                raise SetupPending('This legacy cleanup may have submitted without a stable operation receipt. Preserve its original approval and reconcile that exact legacy receipt before any new cleanup; no replacement request was sent.')
            values, config, session = self._cleanup_admin()
            reviewed = state['approvals'][-1]['review']
            self._approved(state, 'cleanup_review', {**reviewed, 'instance_id': config['instance_id']})
            approval_digest = state['answers']['cleanup_review']['review_digest']
            ids = reviewed['record_ids']
            request = state.get('cleanup_request')
            if request is None:
                current = {entry['record_id']: entry for entry in admin.cleanup_describe(config, session, ids)}
                changed = [record['record_id'] for record in reviewed['records']
                           if (current.get(record['record_id']) or {}).get('revision') != record['revision']
                           or current[record['record_id']].get('kind') != record['kind']]
                if changed:
                    raise SetupPending('Reviewed candidate changed (' + ', '.join(changed) + '); approval is invalid. '
                                       'Begin a new clean-up review. Nothing was changed.')
            if self._cleanup_receipt(state, 'cleanup_local_sweep') is None:
                erased = reviewed.get('prior_erasure_cache_sweep_ids', [])
                self._sweep_administration(erased, current=state)
                sweep = sweep_erased(self._cleanup_roots(), erased, ERASED_LOCAL)
                sweep['administration_files_touched'] = self._sweep_administration(erased, current=state, apply=True)
                self._receipt(state, 'cleanup_local_sweep', {**sweep, 'erased_records': len(erased)})
            actor = (values.get('MAVEN_AWS_EXPECTED_PRINCIPAL') or 'owner') + ' via maven_admin cleanup ' + state['operation_id']
            if request is None:
                if action == 'erase':
                    blockers = self._cleanup_blockers(ids)
                    self._sweep_administration(ids, current=state)
                    if blockers:
                        raise SetupPending('Erase refused; resolve first:\n' + '\n'.join(blockers))
                request = {'operation_id': state['operation_id'], 'expected_revisions': {
                               record['record_id']: record['revision'] for record in reviewed['records']},
                           'actor': actor, 'reason': reviewed['reason'], 'approval_digest': approval_digest}
                state['cleanup_request'] = request
                self._save(state)
            if action == 'erase':
                result = admin.erase_records(config, session, ids, **request)
                self._receipt(state, 'cleanup_apply', result)
                state['step'] = 'cleanup_sweep'
                self._save(state)
                self._cleanup_sweep(state)
            else:
                result = admin.forget_records(config, session, ids, restore=action == 'restore', **request)
                self._receipt(state, 'cleanup_apply', result)
                state.update(state='completed', pending_interaction=None, outcome={
                    'status': 'completed', 'action': action, 'receipt_id': result['receipt_id'],
                    'changed': result.get('changed', []), 'unchanged': result.get('unchanged', []),
                    'local_sweep': self._cleanup_receipt(state, 'cleanup_local_sweep')})
        elif step == 'cleanup_sweep':
            self._cleanup_sweep(state)
        else:
            raise SetupPending('Unknown clean-up phase; retain evidence and use the matching implementation.')

    def _cleanup_sweep(self, state):
        applied = self._cleanup_receipt(state, 'cleanup_apply')
        reviewed = state['approvals'][-1]['review']
        self._sweep_administration(reviewed['record_ids'], current=state)
        sweep = sweep_erased(self._cleanup_roots(), reviewed['record_ids'], applied.get('tombstone') or ERASED_LOCAL)
        self._receipt(state, 'cleanup_sweep', sweep)
        state.update(state='completed', pending_interaction=None, outcome={
            'status': 'completed', 'action': 'erase', 'receipt_id': applied.get('receipt_id'),
            'records': applied.get('records', []), 'local_sweep_before': self._cleanup_receipt(state, 'cleanup_local_sweep'),
            'local_sweep': sweep, 'caveats': reviewed['caveats']})
        touched = self._sweep_administration(reviewed['record_ids'], current=state, apply=True)
        state['outcome']['local_sweep']['administration_files_touched'] = touched

    def _removal_access(self, state, home):
        from tools.memory.environment import load_environment, deployment_mode
        from tools.setup_common import inspect_login_identity, verify_deployment_access, expected_account, atomic_private
        path = Path(home) / '.env'
        values = load_environment(path)
        before = dict(values)
        mode = deployment_mode(values)
        prefix = 'MAVEN_AWS_' if mode == 'role' else 'MAVEN_DEPLOY_'
        selected = state['answers'].get('removal_identity', {})
        if selected and mode == 'profile':
            cache = Path(selected['cache']).expanduser()
            if not cache.is_absolute() or str(cache) == values.get('MAVEN_AWS_LOGIN_CACHE'):
                raise SetupPending('Maintenance cache must be absolute and separate from daily-use login.')
            private_directory(cache)
            values.update(MAVEN_DEPLOY_PROFILE=selected['profile'], MAVEN_DEPLOY_LOGIN_CACHE=str(cache))
        state['presentation'] = {'title': 'Cloud removal', 'purpose': 'Maintenance access — inspect and remove only reviewed cloud resources',
            'account': expected_account(values), 'profile': values.get(prefix + 'PROFILE', 'Not selected'),
            'principal': values.get('MAVEN_DEPLOY_ROLE_ARN' if mode == 'role' else
                                    'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'Unknown'), 'verified': False,
            'region': values.get('MAVEN_AWS_REGION', 'us-east-1'),
            'target': values.get('MAVEN_STACK_NAME', '') + '; instance ' + values.get('MAVEN_MEMORY_INSTANCE_ID', ''),
            'description': 'Home: ' + str(home) + '. No resources are removed until the exact removal plan is approved.', 'details': ''}
        if mode == 'deferred' or (mode == 'profile' and not all(values.get(prefix + key) for key in ('PROFILE', 'LOGIN_CACHE'))):
            raise SetupPending('Configure deployment access first: ' + shlex.join(
                ['maven-setup', '--home', str(home), '--deployment-access']) +
                '. Then resume this exact removal operation; no removal was dispatched.')
        try:
            identity = inspect_login_identity(values, deploy=True)
        except SetupPending:
            raise
        except Exception as error:
            if not self._expired_login_handoff(state, error, values=values, deploy=True):
                raise
            state['state'] = 'blocked'
            return False
        state['presentation'].update(account=identity['Account'], principal=identity['Arn'], verified=True)
        review = {'identity': identity, 'home': str(home), 'profile': values[prefix + 'PROFILE'],
                  'cache': values[prefix + 'LOGIN_CACHE'], 'purpose': state['presentation']['purpose']}
        if mode == 'role':
            review.update(role_arn=values['MAVEN_DEPLOY_ROLE_ARN'], source_principal=values['MAVEN_AWS_EXPECTED_PRINCIPAL'])
        if 'removal_identity_approval' not in state['answers']:
            self._review(state, 'removal_identity_approval', 'Confirm actual maintenance identity before removal planning', review, state['step'])
            return False
        self._approved(state, 'removal_identity_approval', review)
        if mode == 'profile':
            values['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'] = identity['Arn']
        self._preflight(state, values, 'removal')
        if before != values:
            if verify_deployment_access(values) != identity or load_environment(path) != before:
                raise SetupPending('Deployment identity or settings changed; nothing was saved.')
            atomic_private(path, ''.join(key + '=' + json.dumps(value) + '\n' for key, value in sorted(values.items())))
        return True

    def _project_lifecycle(self, state):
        """Rootless, exact-reviewed cloud transition using the authoritative lifecycle transaction."""
        from tools.uninstall import lifecycle
        from tools.uninstall.common import UninstallError
        try:
            if state['step'] == 'start':
                parameters = state['parameters']
                action = lifecycle.plan_project_id(
                    self.home, parameters['project_id'], action=parameters['action'],
                    operation_id=state['operation_id'])
                details = action['details']
                project = details['project']
                review = {
                    'operation_id': state['operation_id'], 'action': parameters['action'],
                    'instance_id': details['instance_id'], 'recovery_epoch': details['recovery_epoch'],
                    'account': details['admin_identity']['account'], 'region': details['config']['region'],
                    'identity': details['admin_identity'], 'project_id': details['project_id'],
                    'project_name': project['name'],
                    'current_state': 'archived' if (project['lifecycle'] or {}).get('archived') else 'active',
                    'history_retained': True, 'affects_all_machines': True,
                    'local_binding_changed': False, 'local_detach_performed': False,
                    'effects': ('Hide project and project-owned content by default; explicit archived-history '
                                'consultation only; block cloud writes.' if parameters['action'] == 'archive'
                                else 'Restore default visibility and cloud write eligibility; forgotten or erased '
                                     'content remains unavailable. Local attachment remains an explicit choice.'),
                    'work': details['work'], 'lifecycle_action': action,
                }
                state['lifecycle_review'] = review
                state['presentation'] = {'account': review['account'], 'region': review['region'],
                                         'target': project['name'] + ' (' + details['project_id'] + ')'}
                self._review(state, 'project_lifecycle', 'Review exact cloud project lifecycle change',
                             review, 'project_lifecycle_apply',
                             {'writers_quiescent': details['project_id']})
            elif state['step'] == 'project_lifecycle_apply':
                review = state['lifecycle_review']
                self._approved(state, 'project_lifecycle', review)
                progress = state.setdefault('lifecycle_progress', {})
                result = lifecycle.execute(review['lifecycle_action'], progress, lambda: self._save(state))
                self._receipt(state, 'project_lifecycle_apply', result)
                state.update(state='completed', outcome=result, live_state_checked=True)
        except UninstallError as error:
            # Lifecycle errors are authored safe messages; provider errors have already been redacted.
            state.update(state='uncertain' if state.get('lifecycle_progress', {}).get('outcome_unresolved')
                         else 'blocked', error=str(error),
                         recovery='Inspect status, resolve the prerequisite without abandoning work, then advance '
                                  'this same operation. Its exact reviewed action and request identity are retained.')

    def _uninstall(self, state):
        from tools.uninstall import cli
        if state['step'] == 'start' and not any(state['parameters'].get(key)
                for key in ('plan_path', 'project', 'reactivate_project', 'machine', 'cloud')):
            self._question(state, 'removal_scopes', 'Select independent removal scopes; nothing is implicitly selected', [
                Field('projects', 'Project roots, one per line; blank leaves projects unchanged', kind='multiline', required=False),
                Field('machine', 'Remove this installation and its owned integrations', False, kind='bool'),
                Field('cloud', 'Remove cloud instance independently', False, kind='bool'),
                Field('cloud_mode', 'Cloud removal mode', 'retire', kind='select', choices=(('Retire (retain data)', 'retire'), ('Purge', 'purge'))),
                Field('offline', 'Offline local detach (no cloud removal)', False, kind='bool')], 'removal_plan')
            return
        if state['step'] in ('start', 'removal_plan'):
            parameters = dict(state['parameters'])
            if 'removal_scopes' in state['answers']:
                selected = dict(state['answers']['removal_scopes'])
                parameters.update(selected)
                parameters['project'] = [line.strip() for line in parameters.pop('projects').splitlines() if line.strip()]
                if not parameters['cloud']:
                    parameters.pop('cloud_mode')
            if parameters.get('plan_path'):
                path, plan = cli._load_plan(parameters['plan_path'])
                targets = [target for target in plan['targets'] if target.get('scope') == 'cloud']
                if targets and not self._removal_access(state, targets[0]['home']):
                    return
                result = {'plan_path': str(path), 'plan': plan}
            else:
                args = cli.parser().parse_args(['plan'])
                for key, value in parameters.items():
                    setattr(args, key, value)
                if not args.home:
                    args.home = [str(self.home)]
                if args.cloud and not self._removal_access(state, args.cloud_home or args.home[0]):
                    return
                result = cli.build_plan(args)
            state['plan_path'] = result['plan_path']
            self._review(state, 'removal', 'Review independent removal scopes, every target, retained resource and barrier',
                         result['plan'], 'removal_apply', result['plan']['confirmations'])
        elif state['step'] == 'removal_apply':
            path, plan = cli._load_plan(state['plan_path'])
            self._approved(state, 'removal', plan)
            cloud_targets = [target for target in plan['targets'] if target.get('scope') == 'cloud']
            if cloud_targets and not self._removal_access(state, cloud_targets[0]['home']):
                return
            cli._verify_runner(plan, path.parent)
            arguments = ['resume', '--plan', str(path), '--confirm', plan['plan_digest']]
            for key, flag in (('purge', '--confirm-purge'), ('abandon', '--confirm-abandon')):
                if plan['confirmations'].get(key):
                    arguments.extend([flag, plan['confirmations'][key]])
            command = [sys.executable, '-B', str(path.parent / 'runner' / 'run.py'), *arguments]
            required = set(plan['requires'])
            attestations = required & {'harnesses-stopped', 'writers-quiescent', 'harness-config-risk'}
            if plan['machine_homes'] or attestations:
                self._handoff(state, 'Run the pinned removal externally and supply only truthful required attestations: '
                              'writers-quiescent means actual writers stopped; harness-config-risk acknowledges that '
                              'running harnesses may rewrite removed configuration and need restart. A legacy '
                              'harnesses-stopped requirement still means affected harnesses are closed.', command)
                state['handoff']['required_external_attestations'] = sorted(attestations)
                return
            # Never import execute handlers into the process being removed. The pinned
            # runner validates its own snapshot and keeps every per-effect receipt.
            environment = dict(os.environ)
            for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
                environment.pop(key, None)
            result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            receipt = cli.status(path)
            self._receipt(state, 'removal_apply', receipt)
            state['state'] = 'completed' if receipt['status'] == 'completed' else 'blocked'
            if result.returncode and state['state'] != 'completed':
                state['error'] = 'Pinned removal worker did not complete; inspect preserved removal receipts.'

    def _install(self, state):
        parameters = state['parameters']
        source = state['answers'].get('install', {}).get('source') or parameters.get('source')
        if not source or not Path(source).expanduser().is_absolute():
            self._question(state, 'install', 'Select the reviewed local MAVEN checkout for installation', [
                Field('source', 'Absolute checkout containing install.py')], 'install_handoff')
            return
        self._install_handoff(state, source)

    def _install_handoff(self, state, source):
        source = Path(source).expanduser()
        if not (source / 'install.py').is_file():
            raise SetupPending('Selected checkout does not contain install.py.')
        command = [sys.executable, str(source / 'install.py'), '--home', str(self.home)]
        for harness in state['parameters'].get('harness', []):
            command.extend(['--harness', harness])
        if state['parameters'].get('profile'):
            command.extend(['--skill-dir', state['parameters']['profile']])
        self._handoff(state, 'Run the reviewed installer in an external terminal. Review each selected integration target and its actual overwrite/reload or stopped-writer requirements; this conversation supplies no shutdown attestation.', command)

    def _recovery(self, state):
        parameters = state['parameters']
        target = parameters.get('operation_id')
        if not target or target == state['operation_id']:
            raise SetupPending('Select an original administration operation_id, not this recovery operation.')
        observed = self.status(target)
        if state['step'] == 'start':
            self._receipt(state, 'diagnosis', observed)
            if parameters.get('action', 'diagnose') == 'diagnose':
                state.update(state='completed', outcome=observed)
            elif parameters['action'] == 'recover':
                self._review(state, 'recover', 'Explicit recovery may reconcile original external requests; status alone never retries',
                             {'selection': parameters, 'observed': observed}, 'recover')
            else:
                raise SetupPending('Recovery action must be diagnose or recover.')
        elif state['step'] == 'recover':
            if not state.get('recovery_started'):
                self._approved(state, 'recover', {'selection': parameters, 'observed': observed})
                state['recovery_started'] = True
                self._save(state)
            original = self._load(target)
            if original['kind'] == 'recovery' or original['state'] in ('abandoned', 'cancelled', 'failed'):
                raise SetupPending('Select a nonterminal original administration operation; its receipts are preserved.')
            self._advance_locked(original)
            outcome = self.status(target)
            state.update(state=outcome['state'] if outcome['state'] in ('blocked', 'uncertain', 'failed') else 'completed',
                         outcome=outcome)
            if state['state'] != 'completed':
                for key in ('error', 'error_detail', 'handoff', 'recovery'):
                    if key in outcome:
                        state[key] = outcome[key]
