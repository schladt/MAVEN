"""Durable administration checkpoints shared by conversation and terminal presenters.

Status is local observation. Only advance executes a named backend phase; responses
never replay a Python stack. Approval evidence is the presenter's supplied owner
statement, bound to the complete persisted review, not an invented terminal click.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID, uuid4

from tools.interaction import Field
from tools.setup_common import SetupPending, private_directory, read_private, save_json, setup_lock


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _source(value):
    if (not isinstance(value, dict) or set(value) != {'origin', 'excerpt'}
            or any(not isinstance(value[key], str) or not value[key].strip() for key in value)):
        raise ValueError('Supply actual owner provenance as source.origin and source.excerpt; never credentials.')
    if len(value['origin']) > 160 or len(value['excerpt']) > 2000:
        raise ValueError('Approval origin/excerpt exceed the cloud provenance limits (160/2000 characters).')
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


class AdminOperations:
    """Explicit, restartable setup/install/project/removal/recovery operations."""

    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('MAVEN_HOME', '~/.maven')).expanduser().absolute()
        self.directory = self.home / 'runtime' / 'administration'

    def _prepare(self):
        for directory in (self.home, self.home / 'runtime', self.directory):
            private_directory(directory)

    def _path(self, identifier):
        if str(UUID(identifier)) != identifier:
            raise ValueError('Select the exact administration operation UUID.')
        return self.directory / (identifier + '.json')

    def _load(self, identifier):
        path = self._path(identifier)
        if path.absolute() != path.resolve():
            raise SetupPending('Administration evidence must not traverse symlinks.')
        state = read_private(path, maximum=32 * 1024 * 1024)
        if state.get('schema') != 1 or state.get('home') != str(self.home):
            raise SetupPending('Administration state/home mismatch; preserve the original evidence.')
        return state

    def _save(self, state):
        state['revision'] += 1
        save_json(self._path(state['operation_id']), state)

    def begin(self, kind, parameters):
        allowed = {
            'setup': {'mode', 'project_root', 'source'},
            'project': {'root'},
            'install': {'source', 'harness', 'profile'},
            'uninstall': {'project', 'reactivate_project', 'project_home', 'home', 'machine',
                          'all_homes', 'cloud', 'cloud_home', 'cloud_mode', 'archive_project',
                          'offline', 'export', 'optional_resource', 'retained_resource',
                          'abandon_sessions', 'output', 'adopt_legacy', 'plan_path'},
            'recovery': {'operation_id', 'root', 'session_id', 'action'},
        }
        if kind not in allowed or not isinstance(parameters, dict) or set(parameters) - allowed[kind]:
            raise ValueError('Unsupported administration kind or parameters.')
        if kind == 'setup' and parameters.get('mode') not in (None, 'existing', 'new'):
            raise ValueError('Setup mode must be existing or new.')
        if kind == 'install' and (not isinstance(parameters.get('harness', []), list)
                or any(not isinstance(item, str) for item in parameters.get('harness', []))):
            raise ValueError('Install harness must be an explicit list of names.')
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
        if state.get('plan_path'):
            from tools.uninstall.cli import status
            result['removal'] = status(state['plan_path'])
            if result['removal']['status'] == 'completed':
                result['state'] = 'completed'
        result['next_actions'] = (['respond'] if state['pending_interaction'] else
                                  [] if result['state'] in ('completed', 'cancelled') else ['advance'])
        return result

    def respond(self, operation_id, interaction_id, revision, response):
        self._prepare()
        with setup_lock(self.directory):
            state = self._load(operation_id)
            question = state['pending_interaction']
            if (not question or question['interaction_id'] != interaction_id
                    or question['revision'] != revision):
                raise ValueError('Stale interaction; inspect the current full review before responding.')
            if not isinstance(response, dict):
                raise ValueError('A structured response is required.')
            if question['kind'] == 'review':
                if set(response) - {'approved', 'review_digest', 'source', 'confirmations'}:
                    raise ValueError('Unsupported approval response fields.')
                if response.get('review_digest') != question['review_digest']:
                    raise ValueError('Approval must name the complete current review digest.')
                if type(response.get('approved')) is not bool:
                    raise ValueError('Approval must be explicitly true or false.')
                if not response['approved']:
                    question = {**question, 'interaction_id': str(uuid4()), 'revision': state['revision'] + 1}
                    state.update(state='paused', pending_interaction=question)
                    state.setdefault('responses', []).append({'interaction_id': interaction_id, 'revision': revision, 'response': response})
                    self._save(state)
                    return self.status(operation_id)
                _source(response.get('source'))
                if response.get('confirmations', {}) != question.get('confirmations', {}):
                    raise ValueError('Separate purge/abandon confirmations must match the exact reviewed instances.')
                state['approvals'].append({'interaction_id': interaction_id, 'revision': revision,
                                           'review_digest': question['review_digest'], 'source': response['source'],
                                           'review': question['review'],
                                           'confirmations': response.get('confirmations', {})})
                state['answers'][question['key']] = response
            else:
                names = {field['name'] for field in question['fields']}
                if set(response) != names:
                    raise ValueError('Answer exactly the displayed fields; never send credentials.')
                for field in question['fields']:
                    value = response[field['name']]
                    if field['kind'] == 'bool':
                        valid = type(value) is bool
                    else:
                        valid = isinstance(value, str) and (not field['required'] or bool(value.strip()))
                        if field['choices']:
                            valid = valid and value in [choice[1] for choice in field['choices']]
                    if not valid:
                        raise ValueError('Invalid answer for ' + field['name'])
                key = question['key']
                if key in ('identity', 'deploy_identity'):
                    if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', response['profile']) or not Path(response['cache']).expanduser().is_absolute():
                        raise ValueError('Select a named profile and an absolute private login-cache directory.')
                if key == 'selection':
                    if response['region'] != 'us-east-1':
                        raise ValueError('The reviewed deployment supports us-east-1 only.')
                    if response['mode'] == 'new' and response['connection']:
                        raise ValueError('New provisioning cannot select an existing connection.')
                    if response['connection'].startswith('/'):
                        from tools.memory.environment import load_environment
                        _connection_config(load_environment(Path(response['connection'])))
                if key == 'project':
                    if not Path(response['root']).is_absolute() or not Path(response['root']).is_dir():
                        raise ValueError('Select an absolute existing project directory.')
                    if response['mode'] == 'create':
                        if not response['name'].strip() or response['project_id']:
                            raise ValueError('Creation requires a name and no existing project UUID.')
                    elif str(UUID(response['project_id'])) != response['project_id'] or response['name']:
                        raise ValueError('Select an exact existing project UUID without a new name.')
                if key == 'optional_project' and response['configure']:
                    if not Path(response['root']).is_absolute() or not Path(response['root']).is_dir():
                        raise ValueError('Select an absolute existing project directory.')
                if key == 'install' and (not Path(response['source']).is_absolute()
                        or not (Path(response['source']) / 'install.py').is_file()):
                    raise ValueError('Select an absolute checkout containing install.py.')
                state['answers'][question['key']] = response
            state.setdefault('responses', []).append({'interaction_id': interaction_id, 'revision': revision, 'response': response})
            state.update(step=question['next_step'], pending_interaction=None, state='pending')
            self._save(state)
        return self.status(operation_id)

    def _question(self, state, key, title, fields, next_step):
        state.update(state='awaiting_decision', pending_interaction={
            'interaction_id': str(uuid4()), 'revision': state['revision'] + 1,
            'kind': 'form', 'key': key, 'title': title, 'fields': [asdict(field) for field in fields],
            'next_step': next_step})

    def _review(self, state, key, title, review, next_step, confirmations=None):
        state.update(state='awaiting_decision', pending_interaction={
            'interaction_id': str(uuid4()), 'revision': state['revision'] + 1,
            'kind': 'review', 'key': key, 'title': title, 'review': review,
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

    def advance(self, operation_id):
        self._prepare()
        with setup_lock(self.directory):
            state = self._load(operation_id)
            if state['pending_interaction'] or state['state'] in ('completed', 'cancelled'):
                return self.status(operation_id)
            state.update(state='working', live_state_checked=False)
            state.pop('error', None)
            state.pop('handoff', None)
            self._save(state)  # Persist the phase before any potentially uncertain effect.
            try:
                with redirect_stdout(sys.stderr):
                    getattr(self, '_' + state['kind'])(state)
            except Exception as error:
                # SDK and subprocess messages can contain credentials. Preserve safe codes only.
                code = getattr(error, 'code', type(error).__name__)
                state['state'] = 'uncertain' if any(word in code for word in ('unknown', 'uncertain', 'unavailable', 'timeout')) else 'blocked'
                state['error'] = str(error) if isinstance(error, SetupPending) else code
                state['recovery'] = 'Inspect status, correct the prerequisite, then advance this same operation. Original request artifacts are retained.'
            if state['state'] == 'working':
                state['state'] = 'pending'
            self._save(state)
        return self.status(operation_id)

    def _wizard(self, state):
        from tools.setup import Wizard
        wizard = Wizard(self.home, state['parameters'].get('project_root'))
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
                    raise SetupPending('Existing setup ownership is unresolved. Resume its exact administration UUID; cancellation alone does not resolve its effects.')
                # Archive before replacing the shared owner pointer. A crash between
                # these writes may retry the identical archive, never overwrite it.
                archive = self.directory / ('setup-' + owner + '.json')
                if archive.exists() or archive.is_symlink():
                    if read_private(archive) != wizard.state:
                        raise SetupPending('Archived setup evidence differs; preserve both states and reconcile the original operation.')
                else:
                    save_json(archive, wizard.state)
                wizard.state = {'schema': 1, 'administration_operation': state['operation_id'],
                                'administration_artifacts': state['operation_id']}
            else:
                if (wizard.state != {'schema': 1}
                        or any(path.name not in ('state.json', 'lock') for path in wizard.directory.iterdir())):
                    raise SetupPending('Existing legacy setup evidence has no verified terminal operation. Resume the original maven-setup terminal flow; do not replace its receipts.')
                wizard.state['administration_operation'] = state['operation_id']
            wizard.save()
        artifacts = wizard.state.get('administration_artifacts')
        if artifacts is not None:
            if artifacts != state['operation_id']:
                raise SetupPending('Setup artifact ownership differs from this operation; preserve the original evidence.')
            # Prior root-level or operation-scoped requests remain untouched and
            # cannot be mistaken for this operation's package or starter approvals.
            wizard.directory = wizard.directory / artifacts
            private_directory(wizard.directory)
        return wizard

    def _setup(self, state):
        from tools.memory import bootstrap
        from tools.memory.environment import load_environment, selected_aws_session, transport_ca_bundle
        from tools.setup import Wizard
        setup_directory = self.home / 'runtime' / 'setup'
        private_directory(setup_directory)
        with setup_lock(setup_directory):
            wizard = self._wizard(state)
            step, answers = state['step'], state['answers']
            if (state.get('new_instance_id') and wizard.values.get('MAVEN_MEMORY_INSTANCE_ID')
                    and wizard.values['MAVEN_MEMORY_INSTANCE_ID'] != state['new_instance_id']):
                raise SetupPending('Current connection differs from the original prepared instance; no initialization or starter request was dispatched.')
            if step == 'start':
                self._question(state, 'selection', 'Select instance setup; never enter credentials', [
                    Field('mode', 'Instance', state['parameters'].get('mode') or 'existing', kind='select',
                          choices=(('Connect existing', 'existing'), ('Provision new', 'new'))),
                    Field('connection', 'Existing private .env path or exact stack name (blank uses saved settings)', required=False),
                    Field('region', 'AWS region', wizard.values.get('MAVEN_AWS_REGION', 'us-east-1'))], 'selection')
            elif step == 'selection':
                selection = answers['selection']
                if selection['region'] != 'us-east-1':
                    raise SetupPending('The reviewed infrastructure supports us-east-1 only.')
                if selection['mode'] == 'new' and wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                    raise SetupPending('Existing connection cannot be redeployed; choose a separate home.')
                review = dict(selection)
                connection = selection['connection']
                if selection['mode'] == 'existing' and connection.startswith('/'):
                    imported = load_environment(Path(connection))
                    imported.pop('MAVEN_AWS_LOGIN_CACHE', None)
                    imported.pop('MAVEN_DEPLOY_LOGIN_CACHE', None)
                    _connection_config(imported)
                    review['imported_settings'] = imported
                self._review(state, 'connection', 'Review exact connection selection; existing mode never deploys or initializes', review, 'connection')
            elif step == 'connection':
                review = state['approvals'][-1]['review']
                self._approved(state, 'connection', review)
                updates = {'MAVEN_AWS_REGION': answers['selection']['region']}
                if 'imported_settings' in review:
                    imported = load_environment(Path(review['connection']))
                    imported.pop('MAVEN_AWS_LOGIN_CACHE', None)
                    imported.pop('MAVEN_DEPLOY_LOGIN_CACHE', None)
                    if imported != review['imported_settings']:
                        raise SetupPending('Imported private settings changed after review.')
                    conflicts = [key for key in imported if key in wizard.values and imported[key] != wizard.values[key]]
                    if conflicts:
                        raise SetupPending('Imported settings conflict with this home; nothing was overwritten.')
                    updates.update(imported)
                elif review['connection']:
                    updates['MAVEN_STACK_NAME'] = review['connection']
                wizard.settings(updates)
                state['step'] = 'identity'
            elif step in ('identity', 'deploy_identity'):
                deploy = step == 'deploy_identity'
                prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
                self._question(state, step, 'Select explicit ' + ('deployment/metadata' if deploy else 'routine') + ' login identity', [
                    Field('profile', 'Named AWS login-only profile', wizard.values.get(prefix + 'PROFILE', '')),
                    Field('cache', 'Absolute private login-cache directory', wizard.values.get(prefix + 'LOGIN_CACHE',
                          str(self.home / 'runtime' / ('aws-deploy-login-cache' if deploy else 'aws-login-cache'))))], step + '_inspect')
            elif step in ('identity_inspect', 'deploy_identity_inspect'):
                from tools.setup_common import inspect_login_identity
                deploy = step.startswith('deploy')
                key = 'deploy_identity' if deploy else 'identity'
                prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
                selected = answers[key]
                if not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', selected['profile']):
                    raise SetupPending('Select a valid named AWS profile, not credentials.')
                cache = Path(selected['cache']).expanduser()
                if not cache.is_absolute():
                    raise SetupPending('Login cache must be an absolute private directory.')
                other = wizard.values.get(('MAVEN_AWS_' if deploy else 'MAVEN_DEPLOY_') + 'LOGIN_CACHE')
                if other and cache == Path(other).expanduser():
                    raise SetupPending('Routine and deployment caches must be separate.')
                private_directory(cache)
                wizard.settings({prefix + 'PROFILE': selected['profile'], prefix + 'LOGIN_CACHE': str(cache)})
                selected['cache'] = str(cache)
                try:
                    identity = inspect_login_identity(wizard.values, deploy=deploy)
                except SetupPending as error:
                    self._question(state, key, 'Correct the selected login identity: ' + str(error), [
                        Field('profile', 'Named AWS login-only profile', selected['profile']),
                        Field('cache', 'Absolute private login-cache directory', str(cache))], step)
                    return
                except Exception:
                    self._handoff(state, 'Complete login privately in an external terminal, then advance this same operation. No login is started by chat.',
                                  ['aws', 'login', '--profile', selected['profile'], '--region', wizard.values['MAVEN_AWS_REGION']],
                                  environment={'AWS_LOGIN_CACHE_DIRECTORY': str(cache)})
                    return
                state[key + '_actual'] = identity
                state['live_state_checked'] = True
                self._review(state, key + '_approval', 'Confirm actual selected AWS principal',
                             {'identity': identity, 'profile': selected['profile'], 'cache': str(cache), 'region': wizard.values['MAVEN_AWS_REGION']}, key + '_accept')
            elif step in ('identity_accept', 'deploy_identity_accept'):
                from tools.setup_common import inspect_login_identity
                deploy = step.startswith('deploy')
                key = 'deploy_identity' if deploy else 'identity'
                identity = inspect_login_identity(wizard.values, deploy=deploy)
                review = {'identity': identity, **answers[key], 'region': wizard.values['MAVEN_AWS_REGION']}
                self._approved(state, key + '_approval', review)
                wizard.settings({('MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_') + 'EXPECTED_PRINCIPAL': identity['Arn']})
                if deploy and state.get('identity_return_step'):
                    state['step'] = state.pop('identity_return_step')
                elif not deploy and (answers['selection']['mode'] == 'new' or not wizard.values.get('MAVEN_MEMORY_INSTANCE_ID')):
                    state['step'] = 'deploy_identity'
                else:
                    state['step'] = 'provision' if answers['selection']['mode'] == 'new' else 'connect_review'
            elif step == 'connect_review':
                if wizard.values.get('MAVEN_MEMORY_INSTANCE_ID'):
                    config = _connection_config(wizard.values)
                else:
                    cloud = selected_aws_session(wizard.values, deploy=True, region=wizard.values['MAVEN_AWS_REGION']).client('cloudformation', verify=transport_ca_bundle())
                    try:
                        stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                        if len(stacks) != 1:
                            raise SetupPending('Select exactly one existing stack.')
                        config = bootstrap._outputs(stacks[0])
                    finally:
                        cloud.close()
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
                self._question(state, 'upgrade', 'Existing instance schema', [Field('migrate', 'Review and explicitly apply schema migration?', False, kind='bool')], 'upgrade_choice')
            elif step == 'upgrade_choice':
                state['step'] = 'migration_review' if answers['upgrade']['migrate'] else 'verify'
            elif step == 'provision':
                routine, deploy = state['identity_actual'], state['deploy_identity_actual']
                if (routine['Account'] != deploy['Account'] or not re.fullmatch(
                        r'arn:aws:iam::' + re.escape(routine['Account']) + r':user/[A-Za-z0-9+=,.@_/-]+', routine['Arn'])):
                    raise SetupPending('Fresh provisioning requires an exact same-account IAM user routine principal and deployment identity.')
                self._question(state, 'provision', 'Billable new instance inputs; review actual supported availability zones and prices', [
                    Field('MAVEN_STACK_NAME', 'New stack name'), Field('MAVEN_INSTANCE_NAME', 'Instance name'),
                    Field('MAVEN_NOTIFICATION_EMAIL', 'Notification email'),
                    Field('MAVEN_AVAILABILITY_ZONES', 'Two distinct us-east-1 zones, comma-separated'),
                    Field('MAVEN_ENGINE_VERSION', 'Reviewed Aurora PostgreSQL 18 patch >=18.3', '18.6')], 'prepare')
            elif step == 'prepare':
                wizard.settings({**answers['provision'], 'MAVEN_ACCOUNT_ID': state['identity_actual']['Account'],
                                 'MAVEN_CALLER_PRINCIPAL_ARN': state['identity_actual']['Arn'],
                                 'MAVEN_INSTANCE_ID': wizard.values.get('MAVEN_INSTANCE_ID') or str(uuid4()),
                                 'MAVEN_RECOVERY_EPOCH': wizard.values.get('MAVEN_RECOVERY_EPOCH') or str(uuid4())})
                package = wizard.directory / 'package'
                if not package.exists():
                    bootstrap.prepare(wizard.values, package)
                manifest, template, parameters = bootstrap._package(package)
                state['new_instance_id'] = manifest['deployment_inputs']['InstanceId']
                review = Wizard.package_review(package, manifest, template, parameters)
                self._review(state, 'plan', 'Create/reconcile one CREATE change set; do not execute it', review, 'plan')
            elif step in ('plan', 'apply_review', 'apply'):
                package, path = wizard.directory / 'package', wizard.directory / 'change-set.json'
                manifest, template, parameters = bootstrap._package(package)
                body = Wizard.package_review(package, manifest, template, parameters)
                if step == 'plan':
                    self._approved(state, 'plan', body)
                    result = bootstrap.plan(wizard.values, package, path, change_type='CREATE')
                    self._receipt(state, step, result)
                    state['step'] = 'apply_review'
                else:
                    review = {'package': body, 'change_set': read_private(path)}
                    if step == 'apply_review':
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
                review = {'instance': _connection_config(wizard.values), 'environment': dict(wizard.values), 'upgrade': upgrade,
                          'sql': {name: files('tools.memory').joinpath('migrations', name).read_text(encoding='utf-8') for name in MIGRATION_FILES}}
                if step.endswith('_review'):
                    self._review(state, 'schema', 'Review exact SQL ' + ('upgrade' if upgrade else 'initialization'), review, 'migrate' if upgrade else 'initialize')
                else:
                    self._approved(state, 'schema', review)
                    result = bootstrap.initialize(wizard.values, confirm_instance=review['instance']['instance_id'], upgrade=upgrade)
                    self._receipt(state, step, result)
                    if not upgrade:
                        state['starter_binding'] = dict(wizard.values)
                    if upgrade and wizard.values.get('MAVEN_MEMORY_SCHEMA_VERSION') == '1':
                        state['step'] = 'migration_reconnect'
                    else:
                        state['step'] = 'verify' if upgrade else 'starter_prepare_review'
            elif step == 'migration_reconnect':
                if not wizard.values.get('MAVEN_STACK_NAME'):
                    self._question(state, 'upgraded_stack', 'Migration completed; select actual upgraded service stack', [
                        Field('stack', 'Exact CloudFormation stack name or ARN')], 'migration_stack')
                else:
                    state['step'] = 'migration_stack'
            elif step == 'migration_stack':
                if 'upgraded_stack' in answers:
                    wizard.settings({'MAVEN_STACK_NAME': answers['upgraded_stack']['stack']})
                if not all(wizard.values.get('MAVEN_DEPLOY_' + key) for key in ('PROFILE', 'EXPECTED_PRINCIPAL', 'LOGIN_CACHE')):
                    state['identity_return_step'] = 'migration_stack'
                    state['step'] = 'deploy_identity'
                    return
                cloud = selected_aws_session(wizard.values, deploy=True, region=wizard.values['MAVEN_AWS_REGION']).client('cloudformation', verify=transport_ca_bundle())
                try:
                    stacks = cloud.describe_stacks(StackName=wizard.values['MAVEN_STACK_NAME'])['Stacks']
                    if len(stacks) != 1:
                        raise SetupPending('Select exactly one upgraded stack.')
                    config = bootstrap._outputs(stacks[0])
                finally:
                    cloud.close()
                self._review(state, 'reconnect', 'Review upgraded service outputs; migration alone does not deploy the service', config, 'migration_bind')
            elif step == 'migration_bind':
                config = state['approvals'][-1]['review']
                self._approved(state, 'reconnect', config)
                result = bootstrap.connect(wizard.values, wizard.env, confirm_instance=config['instance_id'], reviewed_config=config)
                if _connection_config(load_environment(wizard.env)) != config:
                    raise SetupPending('Upgraded stack outputs changed after review.')
                self._receipt(state, step, result)
                state['step'] = 'verify'
            elif step.startswith('starter'):
                self._starter(state, wizard)
            elif step == 'verify':
                generation = wizard.verify_authority()
                self._receipt(state, step, {'authority_generation': generation, 'instance_ready': True})
                self._question(state, 'optional_project', 'Instance verified; configure an actual project?', [
                    Field('configure', 'Configure project now', False, kind='bool'),
                    Field('root', 'Absolute project directory', state['parameters'].get('project_root') or '', required=False)], 'optional_project')
            elif step == 'optional_project':
                if answers['optional_project']['configure']:
                    if not Path(answers['optional_project']['root']).is_absolute():
                        raise SetupPending('Select an absolute existing project root.')
                    state['project_root'] = answers['optional_project']['root']
                    state['step'] = 'project_selection'
                else:
                    state.update(state='completed', outcome='Instance ready; project not configured in this operation.')
            elif step.startswith('project_'):
                self._project(state)
            else:
                raise SetupPending('Unknown setup phase; retain evidence and use the matching implementation.')

    def _starter(self, state, wizard):
        from tools.memory import onboarding
        from tools.memory.environment import configured_client
        from tools.memory.validation import digest, validate_create, validate_resolve
        step = state['step']
        directory = wizard.directory / 'starter'
        if state.get('starter_binding') != wizard.values:
            raise SetupPending('Starter connection or selected credentials differ from the original initialization; restore the exact binding before any lookup, submission, challenge or activation.')
        if step == 'starter_prepare_review':
            self._review(state, 'starter_prepare', 'Prepare generic starter proposals offline, not activate them',
                         {'binding': state['starter_binding'], 'action': 'prepare starter proposals'}, 'starter_prepare')
            return
        if step == 'starter_prepare':
            source = self._approved(state, 'starter_prepare',
                                    {'binding': state['starter_binding'], 'action': 'prepare starter proposals'})
            if not directory.exists():
                onboarding.prepare(directory, source_origin=source['origin'], source_excerpt=source['excerpt'])
            state['step'] = 'starter_review'
            return
        manifest = read_private(directory / 'manifest.json')
        index = state.get('starter_index', 0)
        if index == len(manifest['pages']):
            state['step'] = 'verify'
            return
        page = manifest['pages'][index]
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', page['slug']) or page['file'] != page['slug'] + '.json':
            raise SetupPending('Invalid starter artifact path.')
        path = directory / page['file']
        draft = validate_create(read_private(path))
        if (digest(draft['candidate']) != page['candidate_hash'] or
                any(draft[key] != page[key] for key in ('proposal_id', 'page_id', 'request_key'))):
            raise SetupPending('Starter candidate or original request identity changed.')
        round_number = state.get('starter_round', 0)
        suffix = '.chat.' + str(round_number)
        challenge_path = directory / (page['slug'] + suffix + '.challenge.json')
        approval_path = directory / (page['slug'] + suffix + '.approval.json')
        if step == 'starter_review':
            self._review(state, 'starter_submit', 'Propose exact candidate, not activation', draft, 'starter_submit')
        elif step == 'starter_submit':
            self._approved(state, 'starter_submit', draft)
            client = configured_client(wizard.env, values=wizard.values)
            try:
                wizard.before_request(client, draft['request_key'])
                result = client.invoke('/v1/proposals/create', draft)
                actual = client.read_full({'proposal_id': draft['proposal_id']})['data']
                if actual.get('content_hash') != page['candidate_hash']:
                    raise SetupPending('Cloud proposal does not match reviewed candidate.')
            finally:
                client.close()
            self._receipt(state, step, result)
            state['step'] = 'starter_challenge'
        elif step == 'starter_challenge':
            client = configured_client(wizard.env, approval=True, values=wizard.values)
            try:
                challenge = client.invoke('/v1/proposals/challenge', {'proposal_id': draft['proposal_id']})
            finally:
                client.close()
            # A fresh challenge is not an activation request; the persisted review
            # below must be answered before any approval artifact is generated.
            save_json(challenge_path, challenge)
            self._review(state, 'starter_activate', 'Activate this exact candidate through the separate approval role; instructions become operative',
                         {'candidate': draft, 'challenge': challenge}, 'starter_activate')
        elif step == 'starter_activate':
            review = {'candidate': draft, 'challenge': read_private(challenge_path)}
            source = self._approved(state, 'starter_activate', review)
            if not approval_path.exists():
                from tools.memory.client import MemoryClientError
                try:
                    onboarding.approval(path, challenge_path, approval_path,
                                        approval_origin=source['origin'], approval_excerpt=source['excerpt'])
                except MemoryClientError as error:
                    if error.code != 'starter_challenge_expired_or_invalid':
                        raise
                    self._review(state, 'starter_renew', 'Challenge expired before submission; authorize a new challenge for the SAME candidate',
                                 {'candidate': draft, 'expired_challenge': review['challenge']}, 'starter_renew')
                    return
            request = validate_resolve(read_private(approval_path))
            if request['candidate_hash'] != page['candidate_hash'] or request['proposal_id'] != draft['proposal_id']:
                raise SetupPending('Preserved approval differs from the reviewed candidate.')
            client = configured_client(wizard.env, approval=True, values=wizard.values)
            try:
                wizard.before_request(client, request['request_key'])
                result = client.invoke('/v1/proposals/resolve', request)
            except Exception as error:
                if getattr(error, 'code', None) != 'approval_invalid':
                    raise
                self._review(state, 'starter_renew', 'Service definitively rejected approval; preserve it and authorize a new challenge for the SAME candidate',
                             {'candidate': draft, 'rejected_request': request}, 'starter_renew')
                return
            finally:
                client.close()
            self._receipt(state, step, result)
            state['starter_index'] = index + 1
            state['starter_round'] = 0
            state['step'] = 'starter_review'
        elif step == 'starter_renew':
            state['starter_round'] = round_number + 1
            state['step'] = 'starter_challenge'

    def _project_binding(self):
        from tools.memory.environment import load_environment, memory_config
        values = load_environment(self.home / '.env')
        return {'connection': memory_config(values), 'environment': values}

    def _project(self, state):
        step = state['step']
        if step in ('start', 'project_selection'):
            self._question(state, 'project', 'Enable memory in an explicit project; Git is not required', [
                Field('root', 'Absolute existing project directory', state.get('project_root') or state['parameters'].get('root', '')),
                Field('mode', 'Project selection', 'existing', kind='select', choices=(('Existing project', 'existing'), ('Create project', 'create'))),
                Field('project_id', 'Existing project UUID (blank when creating)', required=False),
                Field('name', 'New project name (blank when selecting)', required=False)], 'project_review')
        elif step == 'project_review':
            review = {'selection': state['answers']['project'], 'binding': self._project_binding()}
            self._review(state, 'project', 'Approve exact project, instance and selected AWS identity; no Git mutation', review, 'project_apply')
        elif step == 'project_apply':
            from tools.project_setup import setup_project
            review = state['approvals'][-1]['review']
            selected = review['selection']
            binding = self._project_binding()
            source = self._approved(state, 'project', {'selection': selected, 'binding': binding})
            if not Path(selected['root']).is_absolute():
                raise SetupPending('Project root must be absolute.')
            result = setup_project(argparse.Namespace(
                root=Path(selected['root']), env=self.home / '.env', non_interactive=True,
                create=selected['mode'] == 'create', project_id=selected['project_id'] or None,
                name=selected['name'] or None, source_origin=source['origin'] if selected['mode'] == 'create' else None,
                source_excerpt=source['excerpt'] if selected['mode'] == 'create' else None,
                remote=None, accepted_url=None, branch=None, check=[],
                reviewed_environment=binding['environment']))
            self._receipt(state, step, result)
            state.update(state='completed', outcome=result)

    def _uninstall(self, state):
        from tools.uninstall import cli
        if state['step'] == 'start' and not any(state['parameters'].get(key)
                for key in ('plan_path', 'project', 'reactivate_project', 'machine', 'cloud')):
            self._question(state, 'removal_scopes', 'Select independent removal scopes; nothing is implicitly selected', [
                Field('projects', 'Project roots, one per line; blank leaves projects unchanged', kind='multiline', required=False),
                Field('machine', 'Remove this installation and its owned integrations', False, kind='bool'),
                Field('cloud', 'Remove cloud instance independently', False, kind='bool'),
                Field('cloud_mode', 'Cloud removal mode', 'retire', kind='select', choices=(('Retire (retain data)', 'retire'), ('Purge', 'purge'))),
                Field('offline', 'Offline local detach (no cloud removal)', False, kind='bool'),
                Field('abandon_sessions', 'Explicitly abandon unresolved cloud sessions', False, kind='bool')], 'removal_plan')
            return
        if state['step'] in ('start', 'removal_plan'):
            parameters = dict(state['parameters'])
            if 'removal_scopes' in state['answers']:
                selected = dict(state['answers']['removal_scopes'])
                parameters.update(selected)
                parameters['project'] = [line.strip() for line in parameters.pop('projects').splitlines() if line.strip()]
                if not parameters['cloud']:
                    parameters.pop('cloud_mode')
                    parameters.pop('abandon_sessions')
            if parameters.get('plan_path'):
                path, plan = cli._load_plan(parameters['plan_path'])
                result = {'plan_path': str(path), 'plan': plan}
            else:
                args = cli.parser().parse_args(['plan'])
                for key, value in parameters.items():
                    setattr(args, key, value)
                if not args.home:
                    args.home = [str(self.home)]
                result = cli.build_plan(args)
            state['plan_path'] = result['plan_path']
            self._review(state, 'removal', 'Review independent removal scopes, every target, retained resource and barrier',
                         result['plan'], 'removal_apply', result['plan']['confirmations'])
        elif state['step'] == 'removal_apply':
            path, plan = cli._load_plan(state['plan_path'])
            self._approved(state, 'removal', plan)
            cli._verify_runner(plan, path.parent)
            arguments = ['resume', '--plan', str(path), '--confirm', plan['plan_digest']]
            for key, flag in (('purge', '--confirm-purge'), ('abandon', '--confirm-abandon')):
                if plan['confirmations'].get(key):
                    arguments.extend([flag, plan['confirmations'][key]])
            command = [sys.executable, '-B', str(path.parent / 'runner' / 'run.py'), *arguments]
            required = set(plan['requires'])
            if plan['machine_homes'] or required & {'harnesses-stopped', 'writers-quiescent'}:
                self._handoff(state, 'Stop affected harnesses/writers outside this conversation. Only that external owner can attest the required shutdown; append the indicated flags after doing so.', command)
                state['handoff']['required_external_attestations'] = sorted(required & {'harnesses-stopped', 'writers-quiescent'})
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
        self._handoff(state, 'Installer changes runtime/harness integrations. Close affected harnesses and run the reviewed installer in an external terminal; no shutdown attestation is accepted from the running harness.', command)

    def _recovery(self, state):
        parameters = state['parameters']
        target = parameters.get('operation_id')
        if target:
            if target == state['operation_id']:
                raise SetupPending('Recovery cannot target itself.')
            observed = self.status(target)
        elif parameters.get('root') and parameters.get('session_id'):
            from tools.memory.lifecycle import SessionService
            service = SessionService(parameters['root'], parameters['session_id'], env=self.home / '.env')
            try:
                observed = service.status()
            finally:
                service.close()
        else:
            raise SetupPending('Select an exact administration operation_id or root and session_id for diagnosis.')
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
            self._approved(state, 'recover', {'selection': parameters, 'observed': observed})
            if target:
                original = self._load(target)
                if original['kind'] == 'recovery':
                    raise SetupPending('Select the original operation, not another recovery wrapper.')
                if not original['pending_interaction'] and original['state'] not in ('completed', 'cancelled'):
                    original['state'] = 'working'
                    self._save(original)
                    try:
                        getattr(self, '_' + original['kind'])(original)
                    except Exception as error:
                        original['state'] = 'blocked'
                        original['error'] = str(error) if isinstance(error, SetupPending) else getattr(error, 'code', type(error).__name__)
                    if original['state'] == 'working':
                        original['state'] = 'pending'
                    self._save(original)
                state.update(state='completed', outcome=self.status(target))
            else:
                from tools.memory.lifecycle import SessionService
                service = SessionService(parameters['root'], parameters['session_id'], env=self.home / '.env')
                try:
                    result = service.recover()
                finally:
                    service.close()
                self._receipt(state, 'recover', result)
                state.update(state='completed', outcome=result)
