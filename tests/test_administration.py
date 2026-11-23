"""Restartable administration behavior with temporary evidence and simulated AWS."""
from argparse import Namespace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminOperations
from tools.memory.client import MemoryClientError
from tools.memory.validation import MemoryError
from tools.setup import Wizard, _present
from tools.setup_common import (SetupPending, read_private, save_json, expected_account,
                                check_login_identity, permission_preflight as inspect_permissions,
                                inspect_login_identity)
from tools.uninstall.common import save_private


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / 'home'
        self.controller = AdminOperations(self.home)
        self.source = {'origin': 'owner chat message 42', 'excerpt': 'I approve this exact candidate.'}
        self.environment = patch.dict(os.environ, {'HOME': str(self.base)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.project_binding = {'connection': {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4()),
                                               'endpoint': 'https://instance-a.example'},
                                'environment': {'MAVEN_AWS_PROFILE': 'owner-a',
                                                'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/owner-a'}}
        binding = patch.object(AdminOperations, '_project_binding', side_effect=lambda: self.project_binding)
        binding.start()
        self.addCleanup(binding.stop)
        preflight = patch('tools.setup_common.permission_preflight', return_value={
            'status': 'Passed', 'checks': [{'check': 'Fixture access', 'status': 'Passed'}],
            'limitations': ['Fixture result; not live AWS evidence.'], 'remediation': 'Inspect IAM permissions.'})
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        deployment = patch('tools.setup_common.verify_deployment_access', return_value=identity)
        self.verify_deployment = deployment.start()
        self.addCleanup(deployment.stop)
        self.role = 'arn:aws:iam::123456789012:role/maintenance/aws-admin'
        for target, value in (
                ('inspect_login_identity', identity),
                ('identities', ({'daily': identity, 'maintenance': identity}, {'daily': Mock(), 'maintenance': Mock()})),
                ('inspect_store', {'bucket': 'fixture-secrets', 'wrapping_parameter': {'Name': '/fixture/keys/0123456789abcdef'}}),
                ('verify_use', {'status': 'read_access_verified', 'write_access_verified': False})):
            fixture = patch('tools.secret_setup.' + target, return_value=value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def respond(self, result, values=None):
        question = result['pending_interaction']
        if question['kind'] == 'review':
            values = {'approved': True, 'review_digest': question['review_digest'],
                      'source': self.source, 'confirmations': question['confirmations']}
        elif question['key'] == 'selection':
            names = {field['name'] for field in question['fields']}
            values = {key: value for key, value in values.items() if key in names}
        elif question['key'] == 'secrets_source':
            values = {'mode': 'manual'}
        elif question['key'] == 'secrets_details':
            values = {'MAVEN_SECRETS_BUCKET': 'fixture-secrets', 'MAVEN_SECRETS_KEY_ID': '0123456789abcdef',
                      'MAVEN_SECRETS_PARAMETER_PREFIX': '/fixture/values/',
                      'MAVEN_SECRETS_FILE_KEY_PREFIX': '/fixture/keys/', 'MAVEN_SECRETS_OBJECT_PREFIX': 'files/'}
        elif question['key'] == 'secrets_identity':
            values = {'account': '123456789012', 'region': 'us-east-1',
                      'daily_profile': 'owner', 'daily_cache': str(self.home / 'runtime/login'),
                      'maintenance_profile': 'maintainer', 'maintenance_cache': str(self.home / 'runtime/deploy-login')}
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'], values)

    def advance_to_question(self, result):
        for _ in range(25):
            if result['pending_interaction'] or result['state'] in ('completed', 'blocked', 'uncertain'):
                return result
            result = self.controller.advance(result['operation_id'])
        self.fail('Explicit phase sequence did not reach a decision or outcome')

    def test_restart_preserves_question_and_rejects_stale_response_without_effect(self):
        result = self.controller.begin('project', {'root': str(self.base)})
        question = result['pending_interaction']
        self.controller = AdminOperations(self.home)
        self.assertEqual(self.controller.status(result['operation_id'])['pending_interaction'], question)
        with patch('tools.project_setup.setup_project') as backend:
            with self.assertRaisesRegex(ValueError, 'Stale'):
                self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'] + 1, {})
            result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'Example', 'project_id': ''})
            result = self.advance_to_question(result)
            review = result['pending_interaction']
            with self.assertRaisesRegex(ValueError, 'digest'):
                self.controller.respond(result['operation_id'], review['interaction_id'], review['revision'],
                                        {'approved': True, 'review_digest': 'wrong', 'source': self.source})
            result = self.respond(result)
            backend.assert_not_called()
            backend.return_value = {'status': 'ready', 'project': {'root': str(self.base)}}
            result = AdminOperations(self.home).advance(result['operation_id'])
            self.assertEqual(result['state'], 'completed')
            args = backend.call_args.args[0]
            self.assertTrue(args.non_interactive)
            self.assertIsNone(args.remote)
            self.assertEqual(args.source_origin, self.source['origin'])
            self.assertEqual(args.source_excerpt, self.source['excerpt'])
        self.assertEqual((self.controller.directory / (result['operation_id'] + '.json')).stat().st_mode & 0o777, 0o600)

    def test_memory_session_recovery_and_stopped_session_selection_are_not_supported(self):
        for kind, params in (('recovery', {'root': str(self.base), 'session_id': str(uuid4())}),
                             ('project', {'root': str(self.base), 'stopped_session_ids': []})):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.controller.begin(kind, params)

    def test_existing_setup_reaches_verified_readiness_without_provisioning(self):
        wizard = Wizard(self.home)
        instance = str(uuid4())
        wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': instance, 'MAVEN_AWS_REGION': 'us-east-1'})
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        config = {'instance_id': instance, 'schema_version': 2}
        with patch('tools.administration._connection_config', return_value=config), \
                patch('tools.setup_common.inspect_login_identity', return_value=identity), \
                patch.object(Wizard, 'verify_connection', return_value={'capability': 'project_reconcile_v1'}), \
                patch('tools.memory.bootstrap.prepare') as prepare, \
                patch('tools.memory.bootstrap.apply') as apply, \
                patch('tools.memory.bootstrap.initialize') as initialize:
            result = self.controller.begin('setup', {})
            answers = {'selection': {'mode': 'existing', 'connection': '', 'region': 'us-east-1'},
                       'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                       'deploy_access': {'mode': 'deferred'},
                       'upgrade': {'migrate': False}}
            for _ in range(30):
                result = self.advance_to_question(result)
                if result['state'] == 'completed':
                    break
                self.assertNotIn(result['state'], ('blocked', 'uncertain'), result)
                key = result['pending_interaction']['key']
                result = self.respond(result, answers.get(key))
                self.controller = AdminOperations(self.home)
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(any(receipt['result'].get('instance_ready') for receipt in result['receipts']))
            self.assertEqual(result['outcome']['home'], str(self.home))
            self.assertEqual(result['outcome']['account'], identity['Account'])
            self.assertTrue(result['outcome']['instance_ready'])
            self.assertFalse(result['outcome']['secrets_ready'])
            self.assertFalse(any(receipt['step'] == 'secrets_verify' for receipt in result['receipts']))
            self.assertFalse(result['outcome']['project_configured'])
            self.assertNotIn('optional_project', result['answers'])
            self.assertNotIn('deploy_identity', result['answers'])
            self.verify_deployment.assert_not_called()
            self.assertEqual(Wizard(self.home).values['MAVEN_DEPLOY_MODE'], 'deferred')
            prepare.assert_not_called()
            apply.assert_not_called()
            initialize.assert_not_called()

        original_path = self.controller._path(result['operation_id'])
        original_operation = original_path.read_bytes()
        wizard = Wizard(self.home)
        wizard.state['submitted_requests'] = [str(uuid4())]
        wizard.save()
        original_setup = wizard.state_path.read_bytes()
        stale_request = wizard.directory / 'change-set.json'
        save_json(stale_request, {'request_key': str(uuid4()), 'status': 'completed'})
        original_request = stale_request.read_bytes()

        next_operation = AdminOperations(self.home).begin('setup', {})
        self.assertEqual(next_operation['state'], 'awaiting_decision')
        self.assertEqual(next_operation['pending_interaction']['key'], 'selection')
        self.assertNotEqual(next_operation['operation_id'], result['operation_id'])
        self.assertEqual(next_operation['receipts'], [])
        fresh = self.controller._wizard(next_operation)
        self.assertNotIn('submitted_requests', fresh.state)
        self.assertEqual(fresh.values, wizard.values)
        self.assertFalse((fresh.directory / stale_request.name).exists())
        self.assertEqual(stale_request.read_bytes(), original_request)
        self.assertEqual(original_path.read_bytes(), original_operation)
        self.assertEqual((self.controller.directory / ('setup-' + result['operation_id'] + '.json')).read_bytes(),
                         original_setup)
        self.assertEqual(self.controller.advance(result['operation_id'])['state'], 'completed')
        self.assertEqual(original_path.read_bytes(), original_operation)

    def update_fixture(self, stack_changes):
        """Patches for an update run: current local software, one pending migration, a changed service."""
        wizard = Wizard(self.home)
        instance = str(uuid4())
        wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': instance, 'MAVEN_AWS_REGION': 'us-east-1',
                         'MAVEN_STACK_NAME': 'maven-stack-01'})
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        cloud = Mock()
        cloud.describe_stacks.return_value = {'Stacks': [{
            'StackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/maven-stack-01/1',
            'StackStatus': 'UPDATE_COMPLETE', 'Parameters': [{'ParameterKey': 'InstanceId', 'ParameterValue': instance}]}]}
        cloud.get_template.return_value = {'TemplateBody': {'deployed': 'old'}}
        change_set = {'ChangeSetId': 'reviewed-change', 'StackId': cloud.describe_stacks.return_value['Stacks'][0]['StackId'],
                      'ChangeSetName': 'reviewed-change', 'StackName': 'maven-stack-01',
                      'Parameters': [], 'Capabilities': [], 'RoleARN': None, 'IncludeNestedStacks': None,
                      'OnStackFailure': None, 'Changes': [{'ResourceChange': change} for change in stack_changes]}
        document = {'review': {'change_set': change_set}, 'review_sha256': 'f' * 64}
        patches = [
            patch('tools.administration._connection_config', return_value={'instance_id': instance, 'schema_version': 2}),
            patch('tools.setup_common.inspect_login_identity', return_value=identity),
            patch.object(Wizard, 'verify_connection', return_value={'capability': 'project_reconcile_v1'}),
            patch('tools.update.local_report', return_value={'current': True, 'checkout': str(self.base)}),
            patch('tools.update.running_software_stale', return_value=False),
            patch('tools.update.schema_status', return_value={'state': 'pending', 'applied': 11,
                                                              'pending': ['012_agentic_memory.sql']}),
            patch('tools.update.build_service_package', side_effect=lambda stack, path, instance: path.mkdir()),
            patch('tools.memory.bootstrap._cloud', return_value=cloud),
            patch('tools.memory.bootstrap._describe_changes', return_value={**change_set, 'ExecutionStatus': 'AVAILABLE'}),
            patch('tools.memory.bootstrap._package', return_value=(
                {'deployment_inputs': {'InstanceId': instance}}, json.dumps({'deployed': 'new'}), [])),
            patch('tools.memory.bootstrap.plan', side_effect=lambda values, package, path, change_type: save_json(path, document)),
            patch('tools.memory.environment.configured_client'),
        ]
        mocks = {}
        for item in patches:
            mocks[item.attribute] = item.start()
            self.addCleanup(item.stop)
        return mocks

    def run_update(self, answers, *, stop_at=None):
        result = self.controller.begin('setup', {'mode': 'update', 'checkout': str(self.base)})
        for _ in range(40):
            result = self.advance_to_question(result)
            if result['state'] in ('completed', 'blocked', 'uncertain'):
                return result
            key = result['pending_interaction']['key']
            if key == stop_at:
                return result
            result = self.respond(result, answers.get(key))
        self.fail('Update did not finish')

    def saved_root_binding(self):
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_ACCOUNT_ID': '123456789012', 'MAVEN_AWS_REGION': 'us-east-1',
            'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/daily',
            'MAVEN_AWS_PROFILE': 'daily', 'MAVEN_AWS_LOGIN_CACHE': str(self.home / 'runtime/daily'),
            'MAVEN_DEPLOY_PROFILE': 'aws-root', 'MAVEN_DEPLOY_LOGIN_CACHE': str(self.home / 'runtime/maintenance'),
            'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:root'})
        return wizard

    def choose_role(self):
        result = self.controller.begin('deployment_access', {})
        result = self.advance_to_question(self.respond(result, {'mode': 'role'}))
        return self.advance_to_question(self.respond(result, {'role_arn': self.role}))

    def test_role_cutover_is_independent_of_pre_cloud_update_and_removes_only_old_deployment_binding(self):
        wizard = self.saved_root_binding()
        wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': str(uuid4()), 'MAVEN_STACK_NAME': 'example'})
        update = self.controller.begin('setup', {'mode': 'update'})
        state = self.controller._load(update['operation_id'])
        state.update(step='identity', state='pending', pending_interaction=None, approvals=[],
                     answers={'local_update': {'skip': False}}, receipts=[{'step': 'update_local', 'result': {'current': True}}])
        self.controller._save(state)
        preserved = self.controller._path(update['operation_id']).read_bytes()
        before = wizard.env.read_bytes()
        self.verify_deployment.return_value = {'Arn': self.role, 'Account': '123456789012'}
        review = self.choose_role()
        self.assertEqual(review['pending_interaction']['review']['previous_binding']['MAVEN_DEPLOY_PROFILE'], 'aws-root')
        self.assertEqual(before, wizard.env.read_bytes())
        approved = self.respond(review)
        result = self.controller.advance(approved['operation_id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.controller._path(update['operation_id']).read_bytes(), preserved)
        actual = Wizard(self.home).values
        self.assertEqual(actual, {**{k: v for k, v in wizard.values.items() if not k.startswith('MAVEN_DEPLOY_')},
                                  'MAVEN_DEPLOY_MODE': 'role', 'MAVEN_DEPLOY_ROLE_ARN': self.role})
        self.assertEqual(self.verify_deployment.call_count, 2)
        self.assertEqual(self.verify_deployment.call_args.args[0], actual)
        self.controller.advance(approved['operation_id'])
        self.assertEqual(self.verify_deployment.call_count, 2)

    def test_binding_change_refuses_update_with_deployment_evidence(self):
        wizard = self.saved_root_binding()
        result = self.controller.begin('setup', {'mode': 'update'})
        active = self.controller._load(result['operation_id'])
        active.update(step='identity', state='pending', pending_interaction=None, service_apply_started=True)
        self.controller._save(active)
        before = wizard.env.read_bytes()
        changed = self.controller.begin('deployment_access', {})
        self.assertEqual(changed['state'], 'blocked')
        self.assertIn(active['operation_id'], changed['error'])
        self.assertEqual(before, wizard.env.read_bytes())
        self.verify_deployment.assert_not_called()

    def test_failed_role_preflight_never_uses_old_root_or_changes_settings(self):
        wizard = self.saved_root_binding()
        before = wizard.env.read_bytes()
        self.verify_deployment.side_effect = MemoryClientError('deployment_elevation_denied')
        result = self.choose_role()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['handoff']['command'], ['aws', 'login', '--profile', 'daily', '--region', 'us-east-1'])
        self.assertEqual(result['handoff']['expected_principal'], wizard.values['MAVEN_AWS_EXPECTED_PRINCIPAL'])
        self.assertFalse(result['handoff']['deploy_profile'])
        self.assertNotIn('MAVEN_DEPLOY_PROFILE', self.verify_deployment.call_args.args[0])
        self.assertEqual(before, wizard.env.read_bytes())
        self.verify_deployment.side_effect = MemoryClientError('not_authorized')
        denied = self.controller.advance(result['operation_id'])
        self.assertNotIn('handoff', denied)
        self.assertEqual(denied['state'], 'blocked')
        self.assertEqual(before, wizard.env.read_bytes())

    def test_changed_candidate_or_failed_final_probe_preserves_original_binding(self):
        wizard = self.saved_root_binding()
        self.verify_deployment.return_value = {'Arn': self.role, 'Account': '123456789012'}
        result = self.respond(self.choose_role())
        before = wizard.env.read_bytes()
        self.verify_deployment.side_effect = MemoryClientError('not_authorized')
        self.assertEqual(self.controller.advance(result['operation_id'])['state'], 'blocked')
        self.assertEqual(before, wizard.env.read_bytes())
        wizard.settings({'MAVEN_STACK_NAME': 'changed-between-review-and-save'})
        revised = wizard.env.read_bytes()
        self.verify_deployment.reset_mock()
        stale = self.controller.advance(result['operation_id'])
        self.assertIn('changed after deployment access review', stale['error'])
        self.verify_deployment.assert_not_called()
        self.assertEqual(revised, wizard.env.read_bytes())

    def test_explicit_defer_removes_privileged_binding_offline(self):
        wizard = self.saved_root_binding()
        result = self.controller.begin('deployment_access', {})
        review = self.advance_to_question(self.respond(result, {'mode': 'deferred'}))
        self.assertEqual(review['pending_interaction']['key'], 'deployment_binding')
        self.assertIn('MAVEN_DEPLOY_PROFILE', Wizard(self.home).values)
        finished = self.controller.advance(self.respond(review)['operation_id'])
        self.assertEqual(finished['outcome']['mode'], 'deferred')
        self.assertEqual(Wizard(self.home).values,
                         {**{k: v for k, v in wizard.values.items() if not k.startswith('MAVEN_DEPLOY_')},
                          'MAVEN_DEPLOY_MODE': 'deferred'})
        self.verify_deployment.assert_not_called()

    def test_role_removal_reviews_daily_source_without_persisting_a_profile(self):
        wizard = self.saved_root_binding()
        self.verify_deployment.return_value = {'Arn': self.role, 'Account': '123456789012'}
        self.controller.advance(self.respond(self.choose_role())['operation_id'])
        before = wizard.env.read_bytes()
        with patch('tools.setup_common.inspect_login_identity', return_value=self.verify_deployment.return_value), \
                patch('tools.uninstall.cli.build_plan') as plan:
            result = self.controller.begin('uninstall', {'cloud': True})
        plan.assert_not_called()
        question = result['pending_interaction']
        self.assertEqual(question['key'], 'removal_identity_approval')
        self.assertEqual(question['review']['role_arn'], self.role)
        self.assertEqual(question['review']['profile'], 'daily')
        self.assertEqual(question['review']['source_principal'], wizard.values['MAVEN_AWS_EXPECTED_PRINCIPAL'])
        self.assertEqual(before, wizard.env.read_bytes())

    def test_changed_revision_after_login_does_not_advance_or_replay(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        changed = self.respond(result, {'connection': '', 'region': 'us-east-1'})
        with patch.object(self.controller, '_advance_locked') as dispatch:
            observed = self.controller.advance(result['operation_id'], expected_revision=result['revision'])
            self.assertEqual(changed['revision'], observed['revision'])
            dispatch.assert_not_called()

    def test_update_migrates_schema_then_updates_service_with_reviews(self):
        mocks = self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction',
                                      'ResourceType': 'AWS::Lambda::Function', 'Replacement': 'False'}])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_access': {'mode': 'profile'},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')}}
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}) as initialize, \
                patch('tools.memory.bootstrap.apply', return_value={'status': 'connected'}) as apply:
            result = self.run_update(answers)
        self.assertEqual(result['state'], 'completed', result)
        reviews = [approval['review'] for approval in result['approvals']]
        self.assertTrue(any(review.get('upgrade') is True for review in reviews))  # schema review
        service = next(review for review in reviews if 'changes' in review)
        self.assertEqual(service['changes'][0]['logical_id'], 'MemoryFunction')
        initialize.assert_called_once()
        self.assertTrue(initialize.call_args.kwargs['upgrade'])
        apply.assert_called_once()
        self.assertEqual(apply.call_args.kwargs['confirm_review'], 'f' * 64)
        mocks['plan'].assert_called_once()
        self.assertEqual(mocks['plan'].call_args.kwargs['change_type'], 'UPDATE')
        self.assertTrue(result['outcome']['instance_ready'])
        self.assertFalse(result['outcome']['secrets_ready'])

    def test_redaction_upgrade_deploys_protections_before_ledger_genesis(self):
        mocks = self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'JournalBucketPolicy',
                                      'ResourceType': 'AWS::S3::BucketPolicy', 'Replacement': 'False'}])
        mocks['schema_status'].return_value = {'state': 'pending', 'pending': ['026_reviewed_redaction.sql']}
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_access': {'mode': 'profile'},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')}}
        effects = []
        with patch('tools.memory.bootstrap.apply', side_effect=lambda *a, **k: effects.append('bucket-policy') or {}), \
                patch('tools.memory.bootstrap.initialize', side_effect=lambda *a, **k: effects.append('ledger') or {}):
            result = self.run_update(answers)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(effects, ['bucket-policy', 'ledger'])

    def test_safe_field_diagnostic_does_not_require_environment_or_hide_original_error(self):
        detail = 'Unknown field: extra'
        with patch.object(AdminOperations, '_project', side_effect=MemoryClientError('invalid_request', detail=detail)):
            result = self.controller.begin('project', {'root': str(self.base)})
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['error'], 'invalid_request')
        self.assertEqual(result['error_detail'], detail)
        self.assertNotIn('handoff', result)
        self.assertFalse((self.home / '.env').exists())

    def test_migration_failure_keeps_diagnostics_and_clears_them_on_retry(self):
        self.update_fixture([])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')}}
        result = self.run_update(answers, stop_at='schema')
        result = self.respond(result)
        detail = 'Migration: 020_secret_adapter_grants.sql; statement: 10; SQLSTATE: 42601; rollback confirmed'
        with patch('tools.memory.bootstrap.initialize',
                   side_effect=MemoryError('migration_rejected', 422, detail=detail)):
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['error'], 'migration_rejected')
        self.assertEqual(result['error_detail'], detail)
        restored = AdminOperations(self.home)
        self.assertEqual(restored.status(result['operation_id'])['error_detail'], detail)
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}):
            result = restored.advance(result['operation_id'])
        self.assertNotIn('error_detail', result)
        self.assertNotIn('error', result)
        self.assertNotEqual(result['step'], 'migrate')

    def test_changed_sql_requires_new_review_on_the_original_operation(self):
        from tools.memory.admin import MIGRATION_FILES
        self.update_fixture([])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')}}
        # Model an operation approved under an older bundle, then resumed by corrected software.
        with patch('tools.memory.admin.MIGRATION_FILES', MIGRATION_FILES[:-1]):
            original = self.run_update(answers, stop_at='schema')
            original = self.respond(original)
        identifier = original['operation_id']
        previous_approvals = original['approvals']
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}) as initialize:
            result = AdminOperations(self.home).advance(identifier)
            initialize.assert_not_called()
            question = result['pending_interaction']
            self.assertEqual(question['key'], 'schema')
            self.assertIn('021_secret_adapter_scope_guard.sql', question['review']['sql'])
            self.assertEqual(result['operation_id'], identifier)
            self.assertEqual(result['approvals'], previous_approvals)
            result = self.respond(result)
            result = self.controller.advance(identifier)
        initialize.assert_called_once()
        self.assertEqual(result['operation_id'], identifier)
        self.assertNotEqual(result['step'], 'migrate')

    def test_update_refuses_service_change_that_replaces_data_resources(self):
        self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'DatabaseCluster',
                              'ResourceType': 'AWS::RDS::DBCluster', 'Replacement': 'True'}])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_access': {'mode': 'profile'},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')}}
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}), \
                patch('tools.memory.bootstrap.apply') as apply:
            result = self.run_update(answers)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('DatabaseCluster (AWS::RDS::DBCluster)', result['error'])
        apply.assert_not_called()
        self.assertFalse(any('changes' in approval['review'] for approval in result['approvals']))

    def test_unsubmitted_service_drift_refreshes_same_operation_and_preserves_artifacts(self):
        mocks = self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction',
                                      'ResourceType': 'AWS::Lambda::Function', 'Replacement': 'False'}])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_access': {'mode': 'profile'},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')}}
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}):
            result = self.run_update(answers, stop_at='service_update')
        result = self.respond(result)
        original = self.controller._wizard(result).directory / 'service-change-set.json'
        before = original.read_bytes()
        manifest, template, parameters = mocks['_package'].return_value
        mocks['_package'].return_value = ({'deployment_inputs': {
            **manifest['deployment_inputs'], 'OwnerEmail': 'old@example.invalid'}}, template, parameters)
        stack = mocks['_cloud'].return_value.describe_stacks.return_value['Stacks'][0]
        stack['Parameters'].append({'ParameterKey': 'OwnerEmail', 'ParameterValue': 'new@example.invalid'})
        with patch('tools.memory.bootstrap.apply') as apply:
            refreshed = self.controller.advance(result['operation_id'])
        self.assertEqual(refreshed['operation_id'], result['operation_id'])
        self.assertEqual(refreshed['step'], 'update_service')
        self.assertEqual(refreshed['service_candidate_revision'], 1)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(refreshed['approvals'], result['approvals'])
        apply.assert_not_called()
        mocks['_package'].return_value = ({'deployment_inputs': {
            **manifest['deployment_inputs'], 'OwnerEmail': 'new@example.invalid'}}, template, parameters)
        refreshed = self.controller.advance(result['operation_id'])
        self.assertEqual(refreshed['pending_interaction']['key'], 'service_update')
        self.assertEqual(original.read_bytes(), before)
        self.assertNotEqual(mocks['plan'].call_args.args[2], original)

    def test_service_refresh_refuses_unknown_preview_or_execution(self):
        result = self.controller.begin('setup', {'mode': 'new'})
        state = self.controller._load(result['operation_id'])
        wizard = self.controller._wizard(state)
        save_json(wizard.directory / 'service-change-set.json.pending', {'request_token': str(uuid4())})
        before = self.controller._path(result['operation_id']).read_bytes()
        with self.assertRaisesRegex(SetupPending, 'submitted or unresolved'):
            self.controller._refresh_service_candidate(state, wizard, wizard.directory)
        self.assertEqual(self.controller._path(result['operation_id']).read_bytes(), before)

    def test_optional_secrets_operation_does_not_veto_memory_setup_choices(self):
        wizard = Wizard(self.home)
        secret_owner = str(uuid4())
        wizard.state['secrets_operation'] = secret_owner
        wizard.save()
        result = self.controller.begin('setup', {'mode': 'existing'})
        self.assertEqual(result['pending_interaction']['key'], 'selection')
        self.assertEqual(Wizard(self.home).state['secrets_operation'], secret_owner)

    def test_setup_never_steals_unresolved_or_unverified_ownership(self):
        for phase in ('awaiting_decision', 'working', 'blocked', 'uncertain', 'cancelled', 'completed'):
            with self.subTest(phase=phase):
                home = self.base / phase
                controller = AdminOperations(home)
                original = controller.begin('setup', {})
                saved = controller._load(original['operation_id'])
                saved.update(state=phase, pending_interaction=None)
                controller._save(saved)
                wizard = Wizard(home)
                wizard.state['submitted_requests'] = [str(uuid4())]
                wizard.save()
                evidence = wizard.state_path.read_bytes()
                operation = controller._path(original['operation_id']).read_bytes()
                observed = controller.begin('setup', {})
                self.assertEqual(observed['state'], 'blocked')
                self.assertIsNone(observed['pending_interaction'])
                self.assertEqual(wizard.state_path.read_bytes(), evidence)
                self.assertEqual(controller._path(original['operation_id']).read_bytes(), operation)

    def test_setup_does_not_claim_legacy_or_orphaned_artifacts(self):
        for evidence_kind in ('legacy', 'artifact_only', 'missing_owner'):
            with self.subTest(evidence_kind=evidence_kind):
                home = self.base / evidence_kind
                wizard = Wizard(home)
                if evidence_kind == 'legacy':
                    wizard.state.update(mode='new', ready_project=str(self.base),
                                        submitted_requests=[str(uuid4())])
                elif evidence_kind == 'missing_owner':
                    wizard.state['administration_operation'] = str(uuid4())
                wizard.save()
                artifact = wizard.directory / 'change-set.json.pending'
                save_json(artifact, {'request_key': str(uuid4())})
                state_before, artifact_before = wizard.state_path.read_bytes(), artifact.read_bytes()
                observed = AdminOperations(home).begin('setup', {})
                self.assertEqual(observed['state'], 'blocked')
                self.assertEqual(wizard.state_path.read_bytes(), state_before)
                self.assertEqual(artifact.read_bytes(), artifact_before)

    def test_setup_ownership_transfer_resumes_after_archiving_without_replacing_receipts(self):
        original = self.controller.begin('setup', {})
        saved = self.controller._load(original['operation_id'])
        saved.update(state='completed', pending_interaction=None,
                     receipts=[{'step': 'verify', 'result': {'instance_ready': True, 'authority_generation': 7}}])
        self.controller._save(saved)
        original_operation = self.controller._path(original['operation_id']).read_bytes()
        wizard = Wizard(self.home)
        original_setup = wizard.state_path.read_bytes()
        with patch.object(Wizard, 'save', side_effect=OSError('interrupted owner write')):
            next_operation = self.controller.begin('setup', {})
        self.assertEqual(next_operation['state'], 'blocked')
        self.assertEqual(wizard.state_path.read_bytes(), original_setup)
        archive = self.controller.directory / ('setup-' + original['operation_id'] + '.json')
        self.assertEqual(archive.read_bytes(), original_setup)
        next_operation = AdminOperations(self.home).advance(next_operation['operation_id'])
        self.assertEqual(next_operation['state'], 'awaiting_decision')
        self.assertEqual(Wizard(self.home).state['administration_operation'], next_operation['operation_id'])
        self.assertEqual(archive.read_bytes(), original_setup)
        self.assertEqual(self.controller._path(original['operation_id']).read_bytes(), original_operation)

    def test_explicit_restart_archives_settings_and_unknown_effects_without_replay(self):
        from tools.setup_restart import review_restart, restart
        from tools.administration import _digest
        original = self.controller.begin('setup', {'mode': 'existing'})
        identifier = original['operation_id']
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_AWS_PROFILE': 'old-owner', 'MAVEN_STACK_NAME': 'old-stack'})
        wizard.state['submitted_requests'] = [str(uuid4())]
        wizard.save()
        artifact = wizard.directory / 'change-set.json.pending'
        save_json(artifact, {'request_key': wizard.state['submitted_requests'][0]})
        original_artifact, original_environment = artifact.read_bytes(), wizard.env.read_text()
        state = self.controller._load(identifier)
        state.update(state='uncertain', step='apply', pending_interaction=None)
        self.controller._save(state)
        with patch('tools.memory.bootstrap.apply') as cloud, \
                patch('tools.secret_setup.identities', side_effect=AssertionError('local archive must not log in')):
            review = review_restart(self.home, identifier)
            result = restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
            successor = result['operation_id']
            self.assertNotEqual(successor, identifier)
            self.assertEqual(Wizard(self.home).env.read_text(), original_environment)
            old = self.controller.status(identifier)
            self.assertEqual(old['state'], 'abandoned')
            self.assertEqual(old['next_actions'], [])
            self.assertEqual(self.controller.advance(identifier), old)
            archive = read_private(Path(old['outcome']['archive']))
            self.assertEqual(archive['original']['environment'], original_environment)
            self.assertEqual(archive['original']['setup']['submitted_requests'], wizard.state['submitted_requests'])
            self.assertEqual(artifact.read_bytes(), original_artifact)
            fresh = self.controller.advance(successor)
            self.assertEqual(fresh['pending_interaction']['key'], 'selection')
            self.assertEqual(fresh['approvals'], [])
            self.assertNotIn('submitted_requests', Wizard(self.home).state)
            self.assertEqual(Wizard(self.home).state['administration_artifacts'], successor)
            # Repeating the approved restart must not clear newly entered settings.
            Wizard(self.home).settings({'MAVEN_AWS_PROFILE': 'new-owner'})
            repeated = restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
            self.assertEqual(repeated['operation_id'], successor)
            self.assertEqual(Wizard(self.home).values['MAVEN_AWS_PROFILE'], 'new-owner')
            cloud.assert_not_called()

    def test_local_restart_preserves_legacy_and_conflicting_aws_selectors_offline(self):
        from tools.setup_restart import review_restart, restart
        from tools.administration import _digest
        original = self.controller.begin('setup', {'mode': 'existing'})
        path = self.home / '.env'
        raw = ('MAVEN_AWS_PROFILE="daily"\nMAVEN_ACCOUNT_ID="123456789012"\n'
               'MAVEN_DEPLOY_EXPECTED_PRINCIPAL="arn:aws:iam::210987654321:user/maintenance"\n'
               'MAVEN_GOOGLE_PARAMETER_PREFIX="/legacy/workspace/"\n')
        path.write_text(raw)
        path.chmod(0o600)
        with patch('tools.memory.environment.selected_aws_session', side_effect=AssertionError('offline archive')), \
                patch('tools.secret_setup.identities', side_effect=AssertionError('offline archive')):
            review = review_restart(self.home, original['operation_id'])
            result = restart(self.home, original['operation_id'], confirm_review=_digest(review), source=self.source)
        self.assertEqual(path.read_text(), raw)
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(result['live_state_checked'])
        self.assertEqual(result['restarted_from'], original['operation_id'])

    def test_restart_rejects_stale_review_without_touching_legacy_evidence(self):
        from tools.setup_restart import review_restart, restart
        from tools.administration import _digest
        identifier = self.controller.begin('setup', {})['operation_id']
        review = review_restart(self.home, identifier)
        Wizard(self.home).settings({'MAVEN_STACK_NAME': 'changed-after-review'})
        before = self.controller._path(identifier).read_bytes()
        with self.assertRaisesRegex(SetupPending, 'changed since review'):
            restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
        self.assertEqual(self.controller._path(identifier).read_bytes(), before)
        save_private(self.home / 'runtime/sessions.json', {
            'schema': 1, 'sessions': {'active': {'root': str(self.base)}}, 'reservations': {}})
        original = (self.home / 'runtime/sessions.json').read_bytes()
        review_restart(self.home, identifier)
        self.assertEqual((self.home / 'runtime/sessions.json').read_bytes(), original)
        self.assertEqual(Wizard(self.home).values['MAVEN_STACK_NAME'], 'changed-after-review')

    def test_interrupted_restart_resumes_same_successor_and_retains_original_evidence(self):
        from tools import setup_restart
        from tools.administration import _digest
        identifier = self.controller.begin('setup', {})['operation_id']
        Wizard(self.home).settings({'MAVEN_STACK_NAME': 'original'})
        review = setup_restart.review_restart(self.home, identifier)
        atomic = setup_restart.atomic_private
        def interrupted(path, text):
            atomic(path, text)
            raise OSError('interrupted after clearing settings')
        with patch.object(setup_restart, 'atomic_private', side_effect=interrupted):
            with self.assertRaises(OSError):
                setup_restart.restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
        archive = self.controller.directory / ('restart-' + identifier + '.json')
        journal = read_private(archive)
        self.assertFalse(journal['completed'])
        self.assertIn('original', journal['original']['environment'])
        result = setup_restart.restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
        self.assertEqual(result['operation_id'], journal['replacement']['operation_id'])
        self.assertEqual(Wizard(self.home).state['administration_operation'], result['operation_id'])
        self.assertEqual(read_private(archive)['original'], journal['original'])
        self.assertTrue(read_private(archive)['completed'])

    def test_fresh_setup_needs_no_instruction_upload_and_retains_exact_reviews(self):
        instance = str(uuid4())
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        config = {'instance_id': instance, 'schema_version': 2}
        def prepare_package(values, destination):
            destination.mkdir(mode=0o700)
            return {'status': 'prepared'}

        def apply_package(values, package, review, env, **kwargs):
            Wizard(self.home).settings({'MAVEN_MEMORY_INSTANCE_ID': instance})
            return {'status': 'connected', 'instance_id': instance}

        def plan(values, package, destination, **kwargs):
            save_json(destination, {'review_sha256': 'a' * 64})
            return {'status': 'planned'}

        with patch('tools.setup_common.inspect_login_identity', return_value=identity), \
                patch('tools.administration._connection_config', return_value=config), \
                patch('tools.memory.bootstrap.prepare', side_effect=prepare_package), \
                patch('tools.memory.bootstrap._package', return_value=({'deployment_inputs': {'InstanceId': instance}, 'artifact_sha256': {}}, '{}', [])), \
                patch('tools.memory.bootstrap.plan', side_effect=plan), \
                patch('tools.memory.bootstrap.apply', side_effect=apply_package), \
                patch('tools.memory.bootstrap.initialize', return_value={'status': 'initialized'}), \
                patch('tools.memory.environment.configured_client') as client, \
                patch.object(Wizard, 'verify_connection', return_value={'capability': 'project_reconcile_v1'}):
            result = self.controller.begin('setup', {'mode': 'new'})
            answers = {'selection': {'mode': 'new', 'connection': '', 'region': 'us-east-1'},
                       'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                       'deploy_access': {'mode': 'profile'},
                       'deploy_identity': {'profile': 'deployment', 'cache': str(self.home / 'runtime/deploy-login')},
                       'provision': {'MAVEN_STACK_NAME': 'test', 'MAVEN_INSTANCE_NAME': 'test',
                           'MAVEN_NOTIFICATION_EMAIL': 'owner@example.com', 'MAVEN_AVAILABILITY_ZONES': 'us-east-1a,us-east-1b', 'MAVEN_ENGINE_VERSION': '18.6'}}
            for _ in range(60):
                result = self.advance_to_question(result)
                if result['state'] == 'completed':
                    break
                self.assertNotEqual(result['state'], 'blocked', result)
                result = self.respond(result, answers.get(result['pending_interaction']['key']))
            self.assertEqual(result['state'], 'completed')
        client.assert_not_called()
        self.assertEqual(result['outcome']['connection_verified']['capability'], 'project_reconcile_v1')
        self.assertNotIn('TUI', json.dumps(result['approvals']))
        operation_path = self.controller._path(result['operation_id'])
        operation_before = operation_path.read_bytes()
        wizard = Wizard(self.home)
        setup_before = wizard.state_path.read_bytes()
        artifact_paths = [path for path in wizard.directory.rglob('*') if path.is_file()]
        artifacts_before = {path: path.read_bytes() for path in artifact_paths}
        next_operation = AdminOperations(self.home).begin('setup', {'mode': 'existing'})
        self.assertEqual(next_operation['state'], 'awaiting_decision')
        fresh = self.controller._wizard(next_operation)
        self.assertEqual(fresh.state.get('submitted_requests', []), [])
        self.assertFalse((fresh.directory / 'package').exists())
        self.assertFalse((fresh.directory / 'starter').exists())
        self.assertFalse((fresh.directory / 'change-set.json').exists())
        self.assertEqual(operation_path.read_bytes(), operation_before)
        self.assertEqual((self.controller.directory / ('setup-' + result['operation_id'] + '.json')).read_bytes(),
                         setup_before)
        for path, content in artifacts_before.items():
            if path != wizard.state_path:
                self.assertEqual(path.read_bytes(), content)

    def test_project_only_removal_requires_honest_external_handoff_and_status_never_applies(self):
        root = self.base / 'project'
        root.mkdir()
        binding = root / '.maven/project.json'
        save_private(binding, {'schema': 1, 'root': str(root), 'project_id': str(uuid4()),
                              'git_enabled': False, 'remote': None, 'accepted_url': None, 'branch': None, 'checks': []})
        result = self.controller.begin('uninstall', {'project': [str(root)], 'offline': True,
                                                     'output': str(self.base / 'removal')})
        self.assertEqual(result['pending_interaction']['review']['scopes'], ['project'])
        result = self.respond(result)
        with patch('tools.administration.subprocess.run') as worker:
            result = self.controller.advance(result['operation_id'])
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(set(result['handoff']['required_external_attestations']),
                             {'harness-config-risk', 'writers-quiescent'})
            self.assertNotIn('--harnesses-stopped', result['handoff']['command'])
            self.controller.status(result['operation_id'])
            worker.assert_not_called()
        self.assertTrue(binding.exists())
        self.assertEqual(result['removal']['status'], 'planned')

    def test_install_handoff_resumes_selected_path_without_claiming_installation(self):
        checkout = self.base / 'checkout'
        checkout.mkdir()
        (checkout / 'install.py').write_text('# reviewed fixture\n')
        result = self.controller.begin('install', {})
        result = self.respond(result, {'source': str(checkout)})
        with patch('tools.administration.subprocess.run') as worker:
            result = AdminOperations(self.home).advance(result['operation_id'])
            self.assertEqual(result['state'], 'blocked')
            self.assertFalse(result['handoff']['completion_claimed'])
            self.assertIn(str(checkout / 'install.py'), result['handoff']['command'])
            worker.assert_not_called()

    def test_terminal_presenter_uses_same_durable_project_operation(self):
        from tools.tui.administration import present
        result = self.controller.begin('project', {'root': str(self.base)})
        ui = Mock()
        ui.form.side_effect = [
            {'root': str(self.base), 'mode': 'create', 'name': 'Terminal project', 'project_id': ''},
        ]
        ui.review.return_value = True
        with patch('tools.project_setup.setup_project', return_value={'status': 'ready'}) as backend:
            self.assertEqual(present(ui, self.home, operation_id=result['operation_id']), 0)
        actual = AdminOperations(self.home).status(result['operation_id'])
        self.assertEqual(actual['state'], 'completed')
        self.assertEqual(backend.call_args.args[0].source_origin, 'owner MAVEN administration TUI')

    def test_declined_review_stays_paused_and_requires_a_new_revision(self):
        result = self.controller.begin('project', {'root': str(self.base)})
        result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'Example', 'project_id': ''})
        result = self.advance_to_question(result)
        original = result['pending_interaction']
        result = self.controller.respond(result['operation_id'], original['interaction_id'], original['revision'],
                                         {'approved': False, 'review_digest': original['review_digest']})
        self.assertEqual(result['state'], 'paused')
        self.assertNotEqual(result['pending_interaction']['interaction_id'], original['interaction_id'])
        self.assertEqual(result['pending_interaction']['review'], original['review'])

    def test_changed_instance_or_identity_invalidates_project_approval_before_dispatch(self):
        result = self.controller.begin('project', {'root': str(self.base)})
        result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'Example', 'project_id': ''})
        result = self.advance_to_question(result)
        reviewed = result['pending_interaction']['review']
        self.assertEqual(reviewed['binding'], self.project_binding)
        result = self.respond(result)
        self.project_binding = {
            'connection': {**self.project_binding['connection'], 'instance_id': str(uuid4())},
            'environment': {**self.project_binding['environment'], 'MAVEN_AWS_PROFILE': 'owner-b'},
        }
        with patch('tools.project_setup.setup_project') as backend:
            result = AdminOperations(self.home).advance(result['operation_id'])
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('candidate changed', result['error'])
            backend.assert_not_called()


    def test_explicit_setup_task_is_not_asked_again_or_overridden_by_form(self):
        result = self.controller.begin('setup', {'mode': 'new'})
        question = result['pending_interaction']
        self.assertEqual({field['name'] for field in question['fields']}, {'region'})
        with self.assertRaises(ValueError):
            self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                    {'mode': 'existing', 'region': 'us-east-1'})
        result = self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                         {'region': 'us-east-1'})
        self.assertEqual(result['answers']['selection'], {'mode': 'new', 'connection': '', 'region': 'us-east-1'})
        self.assertEqual(result['responses'][-1]['response'], {'region': 'us-east-1'})

    def stack(self, name, instance_id=None):
        from tests.test_memory_client import CONFIG
        from tools.memory.deployment import OUTPUT_CONFIG
        config = {**CONFIG, 'instance_id': instance_id or str(uuid4())}
        return {'StackName': name,
                'StackId': f'arn:aws:cloudformation:us-east-1:123456789012:stack/{name}/{uuid4()}',
                'StackStatus': 'CREATE_COMPLETE',
                'Outputs': [{'OutputKey': key, 'OutputValue': str(config[field])}
                            for key, field in OUTPUT_CONFIG.items()]}

    def test_discovery_requires_maintenance_confirmation_and_binds_only_selected_stack(self):
        from tools.memory import bootstrap
        first, second = self.stack('first'), self.stack('second')
        failed = {**self.stack('failed'), 'StackStatus': 'ROLLBACK_COMPLETE'}
        unrelated = {'StackStatus': 'CREATE_COMPLETE', 'StackId': first['StackId'], 'Outputs': []}
        malformed = {**self.stack('malformed'), 'Outputs': [
            {'OutputKey': item['OutputKey'], 'OutputValue': 'bad' if item['OutputValue'].startswith('https:') else item['OutputValue']}
            for item in first['Outputs']]}
        cloud = Mock()
        cloud.get_paginator.return_value.paginate.return_value = [
            {'Stacks': [unrelated, failed, malformed, first]}, {'Stacks': [second]}]
        cloud.describe_stacks.return_value = {'Stacks': [second]}
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        with patch('tools.memory.environment.selected_aws_session') as aws, \
                patch.object(bootstrap, 'selected_aws_session', aws), \
                patch('tools.setup_common.inspect_login_identity', return_value=identity):
            aws.return_value.client.return_value = cloud
            result = self.controller.begin('setup', {'mode': 'existing'})
            result = self.advance_to_question(self.respond(result, {'connection': '', 'region': 'us-east-1'}))
            result = self.advance_to_question(self.respond(result))
            self.assertEqual(result['pending_interaction']['key'], 'identity')
            result = self.respond(result, {'profile': 'daily', 'cache': str(self.home / 'runtime/daily')})
            result = self.advance_to_question(self.respond(self.advance_to_question(result)))
            self.assertEqual(result['pending_interaction']['key'], 'deploy_access')
            self.assertNotIn('deferred', [value for _, value in result['pending_interaction']['fields'][0]['choices']])
            result = self.advance_to_question(self.respond(result, {'mode': 'profile'}))
            self.assertEqual(result['pending_interaction']['key'], 'deploy_identity')
            result = self.respond(result, {'profile': 'maintenance', 'cache': str(self.home / 'runtime/maintenance')})
            result = self.advance_to_question(result)
            aws.assert_not_called()
            result = self.advance_to_question(self.respond(result))
            self.assertEqual(result['pending_interaction']['key'], 'discovered_stack')
            self.assertEqual([choice[1] for choice in result['pending_interaction']['fields'][0]['choices']],
                             [first['StackId'], second['StackId']])
            self.assertNotIn('MAVEN_STACK_NAME', Wizard(self.home).values)
            result = self.respond(result, {'stack_id': second['StackId']})
            self.controller = AdminOperations(self.home)
            result = self.advance_to_question(result)
            self.assertEqual(Wizard(self.home).values['MAVEN_STACK_NAME'], second['StackId'])
            self.assertNotIn('MAVEN_MEMORY_INSTANCE_ID', Wizard(self.home).values)
            self.assertEqual(result['pending_interaction']['key'], 'connect')
            from tools.memory.bootstrap import _outputs
            self.assertEqual(result['pending_interaction']['review'], _outputs(second))
            self.assertNotIn('MAVEN_MEMORY_INSTANCE_ID', Wizard(self.home).values)
            result = self.advance_to_question(self.respond(result))
            self.assertEqual(result['pending_interaction']['key'], 'upgrade')
            self.assertEqual(Wizard(self.home).values['MAVEN_MEMORY_INSTANCE_ID'], _outputs(second)['instance_id'])
            self.assertTrue(all(call.kwargs['StackName'] == second['StackId']
                                for call in cloud.describe_stacks.call_args_list))
            self.assertLessEqual({call[0] for call in cloud.method_calls},
                                 {'get_paginator', 'get_paginator().paginate', 'describe_stacks', 'close'})

    def test_discovery_does_not_treat_denied_or_empty_results_as_a_selection(self):
        from botocore.exceptions import ClientError
        for name, failure in [('empty', None), ('denied', ClientError(
                {'Error': {'Code': 'AccessDenied', 'Message': 'private detail'}}, 'DescribeStacks'))]:
            with self.subTest(name=name):
                home = self.base / name
                controller = AdminOperations(home)
                result = controller.begin('setup', {'mode': 'existing'})
                Wizard(home).settings({'MAVEN_AWS_REGION': 'us-east-1',
                    'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/owner'})
                state = controller._load(result['operation_id'])
                state.update(step='discover', pending_interaction=None)
                controller._save(state)
                cloud = Mock()
                cloud.get_paginator.return_value.paginate.side_effect = failure
                cloud.get_paginator.return_value.paginate.return_value = [{'Stacks': []}]
                with patch('tools.memory.environment.selected_aws_session') as aws:
                    aws.return_value.client.return_value = cloud
                    result = controller.advance(result['operation_id'])
                self.assertEqual(result['state'], 'blocked')
                self.assertIsNone(result['pending_interaction'])
                self.assertNotIn('MAVEN_STACK_NAME', Wizard(home).values)
                self.assertNotIn('private detail', result['error'])
                if failure:
                    self.assertIn('cloudformation:DescribeStacks', result['error'])
                else:
                    self.assertIn('123456789012', result['error'])
                    self.assertIn('us-east-1', result['error'])

    def test_blank_target_checkpoint_recovers_without_erasing_receipts(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        identifier = result['operation_id']
        state = self.controller._load(identifier)
        state.update(step='connect_review', state='blocked', pending_interaction=None, error='KeyError')
        state['answers']['selection'] = {'mode': 'existing', 'connection': '', 'region': 'us-east-1'}
        state['approvals'] = [{'review_digest': 'original-identity-approval', 'source': self.source}]
        state['receipts'] = [{'step': 'preflight', 'result': {'status': 'Could not verify'}}]
        self.controller._save(state)
        with patch('tools.memory.environment.selected_aws_session') as aws:
            recovered = AdminOperations(self.home).advance(identifier)
            recovered = self.advance_to_question(recovered)
            self.assertEqual(recovered['pending_interaction']['key'], 'identity')
            self.assertEqual(recovered['identity_return_step'], 'discover')
            self.assertEqual(recovered['operation_id'], identifier)
            self.assertEqual(recovered['approvals'], state['approvals'])
            self.assertEqual(recovered['receipts'], state['receipts'])
            aws.assert_not_called()
            self.preflight.assert_not_called()

    def test_first_use_offers_signin_for_absent_profile_but_not_broken_configuration(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        result = self.respond(result, {'mode': 'existing', 'connection': 'saved-stack', 'region': 'us-east-1'})
        result = self.advance_to_question(result)
        result = self.advance_to_question(self.respond(result))
        question = result['pending_interaction']
        self.assertIn('Unknown', question['description'])
        self.assertTrue(all(field['help'] for field in question['fields']))
        result = self.respond(result, {'profile': 'daily-owner', 'cache': str(self.home / 'runtime/daily-login')})
        config = self.base / 'aws-config'
        environment = {'AWS_CONFIG_FILE': str(config),
                       'AWS_SHARED_CREDENTIALS_FILE': str(self.base / 'absent-credentials')}
        with patch.dict(os.environ, environment), patch('botocore.session.Session.create_client') as network, \
                patch('subprocess.run') as launch:
            result = self.controller.advance(result['operation_id'])
            network.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(result['handoff']['type'], 'aws_login')
        self.assertEqual(result['handoff']['reason'], 'selected_login_profile_missing')
        self.assertEqual(result['handoff']['command'],
                         ['aws', 'login', '--profile', 'daily-owner', '--region', 'us-east-1'])
        self.assertIn('Unknown', result['handoff']['account'])
        self.assertEqual(result['handoff']['environment'],
                         {'AWS_LOGIN_CACHE_DIRECTORY': str(self.home / 'runtime/daily-login')})
        self.assertFalse(result['presentation']['verified'])
        self.assertFalse(config.exists())
        config.write_text('[malformed')
        with patch.dict(os.environ, environment):
            broken = self.controller.advance(result['operation_id'])
        self.assertEqual(broken['error'], 'selected_login_configuration_unavailable')
        self.assertNotIn('handoff', broken)

    def test_saved_account_blocks_other_login_before_confirmation_or_cloud_work(self):
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_MEMORY_CLUSTER_ARN': 'arn:aws:rds:us-east-1:123456789012:cluster:memory'})
        result = self.controller.begin('setup', {})
        result = self.respond(result, {'mode': 'existing', 'connection': 'memory', 'region': 'us-east-1'})
        result = self.advance_to_question(self.respond(self.advance_to_question(result)))
        self.assertIn('123456789012', result['pending_interaction']['description'])
        result = self.respond(result, {'profile': 'other', 'cache': str(self.home / 'runtime/login')})
        with patch('tools.setup_common.inspect_login_identity', return_value={
                'Account': '999999999999', 'Arn': 'arn:aws:iam::999999999999:user/other'}), \
                patch('tools.memory.bootstrap.plan') as plan:
            result = self.controller.advance(result['operation_id'])
        plan.assert_not_called()
        self.assertEqual(result['pending_interaction']['kind'], 'form')
        self.assertNotIn('identity_approval', result['answers'])
        self.assertNotIn('MAVEN_AWS_EXPECTED_PRINCIPAL', Wizard(self.home).values)

    def test_back_revises_unsubmitted_choice_with_fresh_review_and_keeps_approval_history(self):
        result = self.controller.begin('project', {'root': str(self.base)})
        result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'First', 'project_id': ''})
        result = self.advance_to_question(result)
        first = result['pending_interaction']
        result = self.respond(result)
        approvals = result['approvals']
        result = self.controller.back(result['operation_id'])
        self.assertEqual(result['approvals'], approvals)
        self.assertEqual(result['pending_interaction']['key'], 'project')
        self.assertEqual(next(field['default'] for field in result['pending_interaction']['fields']
                              if field['name'] == 'name'), 'First')
        result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'Revised', 'project_id': ''})
        result = self.advance_to_question(result)
        self.assertNotEqual(result['pending_interaction']['review_digest'], first['review_digest'])
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.controller.respond(result['operation_id'], first['interaction_id'], first['revision'],
                                    {'approved': True, 'review_digest': first['review_digest'], 'source': self.source})
        self.assertEqual(result['approvals'], approvals)

    def test_back_refuses_unknown_submitted_effect_without_erasing_evidence(self):
        result = self.controller.begin('project', {'root': str(self.base)})
        result = self.respond(result, {'root': str(self.base), 'mode': 'create', 'name': 'First', 'project_id': ''})
        result = self.respond(self.advance_to_question(result))
        with patch('tools.project_setup.setup_project', side_effect=MemoryClientError('operation_outcome_unknown')):
            result = self.controller.advance(result['operation_id'])
        before = self.controller._path(result['operation_id']).read_bytes()
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn('back', result['next_actions'])
        with self.assertRaisesRegex(SetupPending, 'submitted or unresolved'):
            self.controller.back(result['operation_id'])
        self.assertEqual(self.controller._path(result['operation_id']).read_bytes(), before)

    def test_recovery_uses_original_effect_fence_and_propagates_uncertainty(self):
        target = self.controller.begin('project', {'root': str(self.base)})
        target = self.respond(target, {'root': str(self.base), 'mode': 'create', 'name': 'First', 'project_id': ''})
        target = self.respond(self.advance_to_question(target))
        approvals = target['approvals']
        recovery = self.respond(self.controller.begin('recovery', {
            'operation_id': target['operation_id'], 'action': 'recover'}))
        with patch('tools.project_setup.setup_project', side_effect=MemoryClientError('operation_outcome_unknown')):
            recovery = self.controller.advance(recovery['operation_id'])
        original = self.controller.status(target['operation_id'])
        self.assertEqual((recovery['state'], original['state']), ('uncertain', 'uncertain'))
        self.assertTrue(original['effect_started'])
        self.assertEqual(original['approvals'], approvals)
        self.assertNotIn('back', original['next_actions'])
        with self.assertRaises(SetupPending):
            self.controller.back(target['operation_id'])
        with patch('tools.project_setup.setup_project', return_value={'status': 'ready', 'project': {}}):
            recovery = self.controller.advance(recovery['operation_id'])
        self.assertEqual(recovery['state'], 'completed')
        self.assertEqual(recovery['outcome']['state'], 'completed')

    def test_recovery_never_executes_abandoned_or_failed_operations(self):
        for terminal in ('abandoned', 'failed'):
            target = self.controller.begin('project', {'root': str(self.base)})
            state = self.controller._load(target['operation_id'])
            state.update(state=terminal, step='project_apply', pending_interaction=None)
            self.controller._save(state)
            recovery = self.respond(self.controller.begin('recovery', {
                'operation_id': target['operation_id'], 'action': 'recover'}))
            before = self.controller._path(target['operation_id']).read_bytes()
            with patch.object(AdminOperations, '_project') as execute:
                result = self.controller.advance(recovery['operation_id'])
            execute.assert_not_called()
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(self.controller._path(target['operation_id']).read_bytes(), before)

    def test_old_optional_project_checkpoint_finishes_without_optional_store(self):
        result = self.controller.begin('setup', {})
        state = self.controller._load(result['operation_id'])
        old = {'interaction_id': str(uuid4()), 'revision': state['revision'] + 1,
               'kind': 'form', 'key': 'optional_project', 'title': 'Old project question',
               'fields': [], 'next_step': 'optional_project'}
        state.update(step='verify', pending_interaction=old)
        self.controller._save(state)
        observed = self.controller.status(result['operation_id'])
        self.assertIsNone(observed['pending_interaction'])
        self.assertIn('advance', observed['next_actions'])
        with patch.object(Wizard, 'verify_connection', return_value={'capability': 'project_reconcile_v1'}), \
                patch('tools.project_setup.setup_project') as project:
            result = self.controller.advance(result['operation_id'])
        project.assert_not_called()
        self.assertEqual((result['state'], result['step']), ('completed', 'verify'))
        self.assertTrue(result['outcome']['instance_ready'])
        self.assertEqual(result['retired_interactions'], [old])

    def test_new_reviewed_operation_never_discards_unfinished_setup(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        args = Namespace(operation=None, fresh_operation=True, mode='existing')
        ui = Mock()
        with patch('tools.tui.administration.present') as present:
            with self.assertRaisesRegex(SetupPending, 'unfinished'):
                _present(ui, args, self.home)
            present.assert_not_called()
            ui.review.assert_not_called()
            state = self.controller._load(result['operation_id'])
            state.update(state='completed', pending_interaction=None)
            state['receipts'].append({'step': 'verify', 'result': {'instance_ready': True}})
            self.controller._save(state)
            before = self.controller._path(result['operation_id']).read_bytes()
            ui.review.return_value = True
            _present(ui, args, self.home)
            self.assertIsNone(present.call_args.kwargs['operation_id'])
            self.assertEqual(present.call_args.kwargs['mode'], 'existing')
            self.assertEqual(self.controller._path(result['operation_id']).read_bytes(), before)

    def test_resume_retains_exact_operation_and_summarizes_saved_phase(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        args = Namespace(operation=None, fresh_operation=False, mode='existing')
        ui = Mock()
        with patch('tools.tui.administration.present') as present:
            _present(ui, args, self.home)
        self.assertEqual(present.call_args.kwargs['operation_id'], result['operation_id'])
        self.assertIn(result['operation_id'], ui.message.call_args.args[0])
        ui.review.assert_not_called()

    def test_missing_access_blocks_plan_before_any_cloud_mutation(self):
        result = self.controller.begin('setup', {'mode': 'new'})
        state = self.controller._load(result['operation_id'])
        self.preflight.return_value = {'status': 'Missing access', 'checks': [],
            'limitations': ['Simulation is not a live deployment.'], 'remediation': 'Ask the IAM owner.'}
        with self.assertRaisesRegex(SetupPending, 'Missing access'):
            self.controller._preflight(state, {}, 'infrastructure')
        self.assertEqual(state['receipts'][-1]['result']['status'], 'Missing access')

    def test_revising_unsubmitted_package_preserves_old_artifacts_before_preview(self):
        result = self.controller.begin('setup', {'mode': 'new'})
        state = self.controller._load(result['operation_id'])
        identity = {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/owner'}
        state.update(step='provision', pending_interaction=None, state='pending',
                     identity_actual=identity, deploy_identity_actual=identity)
        state['answers']['selection'] = {'mode': 'new', 'connection': '', 'region': 'us-east-1'}
        self.controller._save(state)
        result = self.controller.advance(result['operation_id'])
        chosen = {'MAVEN_STACK_NAME': 'first-stack', 'MAVEN_INSTANCE_NAME': 'memory',
                  'MAVEN_NOTIFICATION_EMAIL': 'owner@example.com',
                  'MAVEN_AVAILABILITY_ZONES': 'us-east-1a,us-east-1b', 'MAVEN_ENGINE_VERSION': '18.6'}
        instance = str(uuid4())
        prepared = []
        def prepare(values, destination):
            destination.mkdir(mode=0o700)
            marker = destination / 'original.json'
            save_json(marker, {'stack': values['MAVEN_STACK_NAME']})
            prepared.append(marker)
        with patch('tools.memory.bootstrap.prepare', side_effect=prepare), \
                patch('tools.memory.bootstrap._package', return_value=(
                    {'deployment_inputs': {'InstanceId': instance}, 'artifact_sha256': {}}, '{}', [])), \
                patch('tools.memory.bootstrap.plan') as plan:
            result = self.controller.advance(self.respond(result, chosen)['operation_id'])
            first_candidate = result['prepared_plan']
            original = prepared[0].read_bytes()
            result = self.controller.back(result['operation_id'])
            result = self.controller.advance(self.respond(result, {**chosen, 'MAVEN_STACK_NAME': 'revised-stack'})['operation_id'])
        plan.assert_not_called()
        self.assertEqual(result['step'], 'plan')
        self.assertNotEqual(result['prepared_plan'], first_candidate)
        self.assertEqual(prepared[0].read_bytes(), original)
        self.assertEqual(len(prepared), 2)
        self.assertNotEqual(prepared[0].parent, prepared[1].parent)
        self.assertEqual(result['approvals'], [])

    def test_cloud_removal_uses_maintenance_handoff_before_planning(self):
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_STACK_NAME': 'memory',
            'MAVEN_DEPLOY_PROFILE': 'maintainer', 'MAVEN_DEPLOY_LOGIN_CACHE': str(self.home / 'runtime/maintenance'),
            'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/maintainer'})
        with patch('tools.setup_common.inspect_login_identity', side_effect=MemoryClientError('authentication_required')), \
                patch('tools.uninstall.cli.build_plan') as plan, \
                patch('subprocess.run') as launch:
            result = self.controller.begin('uninstall', {'cloud': True})
        plan.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(result['handoff']['type'], 'aws_login')
        self.assertIn('Maintenance', result['handoff']['purpose'])
        self.assertEqual(result['handoff']['account'], '123456789012')

    def test_non_authentication_failure_never_offers_login(self):
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_STACK_NAME': 'memory',
                         'MAVEN_DEPLOY_PROFILE': 'maintainer',
                         'MAVEN_DEPLOY_LOGIN_CACHE': str(self.home / 'runtime/maintenance')})
        with patch('tools.setup_common.inspect_login_identity', side_effect=MemoryClientError('network_unavailable')), \
                patch('tools.uninstall.cli.build_plan') as plan:
            result = self.controller.begin('uninstall', {'cloud': True})
        self.assertEqual(result['error'], 'network_unavailable')
        self.assertNotIn('handoff', result)
        plan.assert_not_called()

    def test_effect_stopped_by_expired_sign_in_offers_that_profiles_exact_sign_in(self):
        from tools.memory.client import MemoryClientError
        result = self.controller.begin('setup', {'mode': 'update'})
        Wizard(self.home).settings({'MAVEN_AWS_REGION': 'us-east-1',
            'MAVEN_AWS_PROFILE': 'daily', 'MAVEN_AWS_LOGIN_CACHE': str(self.home / 'runtime/daily'),
            'MAVEN_DEPLOY_PROFILE': 'maintainer', 'MAVEN_DEPLOY_LOGIN_CACHE': str(self.home / 'runtime/maintenance'),
            'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/maintainer'})
        state = self.controller._load(result['operation_id'])
        state.update(step='update_service_apply', state='pending', pending_interaction=None)
        self.controller._save(state)

        def identity(values, *, deploy=False):
            if deploy:
                raise MemoryClientError('authentication_required')
            return {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/daily'}
        with patch.object(AdminOperations, '_setup', side_effect=MemoryClientError('selected_login_unavailable_or_expired')), \
                patch('tools.setup_common.inspect_login_identity', side_effect=identity):
            result = self.controller.advance(result['operation_id'])
        self.assertEqual((result['state'], result['step']), ('blocked', 'update_service_apply'))
        self.assertEqual(result['handoff']['type'], 'aws_login')
        self.assertEqual(result['handoff']['command'][:4], ['aws', 'login', '--profile', 'maintainer'])
        self.assertEqual(result['handoff']['environment'],
                         {'AWS_LOGIN_CACHE_DIRECTORY': str(self.home / 'runtime/maintenance')})

    def test_completed_explicit_operation_resumes_harness_and_fresh_check_is_opt_in(self):
        from tools.interaction import OperationPaused
        result = self.controller.begin('setup', {'mode': 'existing'})
        wizard = Wizard(self.home)
        wizard.settings({'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_MEMORY_INSTANCE_ID': str(uuid4()),
                         'MAVEN_AWS_PROFILE': 'owner', 'MAVEN_ACCOUNT_ID': '123456789012'})
        state = self.controller._load(result['operation_id'])
        self.controller._complete_setup(state, wizard, 9)
        state['receipts'].append({'step': 'verify', 'result': {'instance_ready': True}})
        self.controller._save(state)
        before = self.controller._path(result['operation_id']).read_bytes()
        args = Namespace(operation=result['operation_id'], fresh_operation=False, mode=None)
        ui = Mock(review=Mock(return_value=False))
        with patch('tools.tui.administration.present') as present, \
                patch('tools.tui.harness.finish_setup') as finish:
            _present(ui, args, self.home)
            finish.assert_called_once()
            present.assert_not_called()
            ui.review.assert_not_called()
            self.assertEqual(self.controller._path(result['operation_id']).read_bytes(), before)
            args.operation, args.fresh_operation = None, True
            with self.assertRaises(OperationPaused):
                _present(ui, args, self.home)
            present.assert_not_called()
            ui.review.return_value = True
            _present(ui, args, self.home)
            self.assertIsNone(present.call_args.kwargs['operation_id'])


class PermissionPreflightTests(unittest.TestCase):
    def values(self):
        return {'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL':
                'arn:aws:iam::123456789012:user/maintenance', 'MAVEN_STACK_NAME': 'memory'}

    def test_actual_reads_and_meaningful_simulation_never_mutate_resources(self):
        cloud, iam, session = Mock(), Mock(), Mock()
        session.client.side_effect = lambda service, **kwargs: {'cloudformation': cloud, 'iam': iam}[service]
        iam.simulate_principal_policy.side_effect = lambda **kwargs: {'EvaluationResults': [
            {'EvalActionName': action, 'EvalDecision': 'allowed'} for action in kwargs['ActionNames']]}
        with patch('tools.memory.environment.selected_aws_session', return_value=session):
            result = inspect_permissions(self.values(), 'infrastructure')
        self.assertEqual(result['status'], 'Passed')
        cloud.describe_account_limits.assert_called_once_with()
        actions = iam.simulate_principal_policy.call_args.kwargs['ActionNames']
        self.assertIn('cloudformation:ExecuteChangeSet', actions)
        self.assertIn('iam:PassRole', actions)
        self.assertIn('rds:CreateDBCluster', actions)
        self.assertEqual({call[0] for call in cloud.method_calls}, {'describe_account_limits', 'close'})
        self.assertEqual({call[0] for call in iam.method_calls}, {'simulate_principal_policy', 'close'})
        self.assertIn('cannot guarantee', ' '.join(result['limitations']))

    def test_denied_read_is_missing_access_not_sts_success(self):
        from botocore.exceptions import ClientError
        cloud, iam, session = Mock(), Mock(), Mock()
        session.client.side_effect = lambda service, **kwargs: {'cloudformation': cloud, 'iam': iam}[service]
        cloud.describe_account_limits.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'DescribeAccountLimits')
        iam.simulate_principal_policy.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'SimulatePrincipalPolicy')
        with patch('tools.memory.environment.selected_aws_session', return_value=session):
            result = inspect_permissions(self.values(), 'infrastructure')
        self.assertEqual(result['status'], 'Missing access')
        self.assertIn('STS identity alone is not proof', result['limitations'][0])

    def test_simulation_access_denial_does_not_claim_target_permissions_are_missing(self):
        from botocore.exceptions import ClientError
        cloud, iam, session = Mock(), Mock(), Mock()
        session.client.side_effect = lambda service, **kwargs: {'cloudformation': cloud, 'iam': iam}[service]
        iam.simulate_principal_policy.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'simulation is not granted'}}, 'SimulatePrincipalPolicy')
        with patch('tools.memory.environment.selected_aws_session', return_value=session):
            result = inspect_permissions(self.values(), 'infrastructure')
        self.assertEqual(result['status'], 'Could not verify')
        self.assertEqual(result['checks'][0]['status'], 'Passed')

    def test_unknown_simulation_remains_explicitly_unverified(self):
        cloud, iam, session = Mock(), Mock(), Mock()
        session.client.side_effect = lambda service, **kwargs: {'cloudformation': cloud, 'iam': iam}[service]
        iam.simulate_principal_policy.side_effect = RuntimeError('unavailable')
        with patch('tools.memory.environment.selected_aws_session', return_value=session):
            result = inspect_permissions(self.values(), 'infrastructure')
        self.assertEqual(result['status'], 'Could not verify')
        self.assertTrue(result['remediation'])

    def test_sql_preflight_uses_existing_daily_principal_admin_trust_and_read_only_sql(self):
        config = {'region': 'us-east-1', 'cluster_arn': 'cluster', 'admin_secret_arn': 'secret',
                  'database': 'maven', 'admin_role_arn': 'admin'}
        values = {'MAVEN_AWS_PROFILE': 'daily', 'MAVEN_DEPLOY_PROFILE': 'maintenance'}
        session = Mock()
        with patch('tools.memory.environment.migration_config', return_value=config), \
                patch('tools.memory.client.selected_session', return_value=session) as selected:
            result = inspect_permissions(values, 'sql')
        selected.assert_called_once_with(config, admin=True, environment=values)
        session.client.return_value.execute_statement.assert_called_once_with(
            resourceArn='cluster', secretArn='secret', database='maven', sql='SELECT 1')
        self.assertEqual(result['status'], 'Passed')
        self.assertIn('trusts are unchanged', ' '.join(result['limitations']))

    def test_expected_saved_account_and_principal_are_independent_login_guards(self):
        values = {'MAVEN_MEMORY_CLUSTER_ARN': 'arn:aws:rds:us-east-1:123456789012:cluster:memory',
                  'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/owner'}
        self.assertEqual(expected_account(values), '123456789012')
        for identity in ({'Account': '999999999999', 'Arn': 'arn:aws:iam::999999999999:user/owner'},
                         {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/other'}):
            with self.assertRaises(SetupPending):
                check_login_identity(values, identity)
        self.assertIn('Unknown', expected_account({}))
        check_login_identity({}, {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/owner'})

    def test_missing_login_credentials_never_fall_back_or_contact_sts(self):
        from tools.memory.client import MemoryClientError
        session = Mock()
        session.get_credentials.return_value = None
        with tempfile.TemporaryDirectory() as cache:
            values = {'MAVEN_AWS_PROFILE': 'daily', 'MAVEN_AWS_LOGIN_CACHE': str(Path(cache).resolve()),
                      'MAVEN_AWS_REGION': 'us-east-1'}
            with patch('boto3.Session', return_value=session):
                with self.assertRaisesRegex(MemoryClientError, 'selected_profile_requires_login'):
                    inspect_login_identity(values)
            session.client.assert_not_called()


class HomeAdministrationTests(unittest.TestCase):
    """MAVEN Home enable/disable with a fake memory service and fake project setup."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / 'maven-home'
        self.home.mkdir(mode=0o700)
        self.source = {'origin': 'owner chat message 7', 'excerpt': 'Yes, make this folder my MAVEN Home.'}
        environment = patch.dict(os.environ, {'HOME': str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.instance = str(uuid4())
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID=' + json.dumps(self.instance) + '\n')
        (self.home / '.env').chmod(0o600)
        self.binding = {'connection': {'instance_id': self.instance, 'recovery_epoch': str(uuid4()),
                                       'endpoint': 'https://instance.example'},
                        'environment': {'MAVEN_AWS_PROFILE': 'owner'}}
        binding = patch.object(AdminOperations, '_project_binding', side_effect=lambda: self.binding)
        binding.start()
        self.addCleanup(binding.stop)
        preflight = patch('tools.setup_common.permission_preflight', return_value={
            'status': 'Passed', 'checks': [], 'limitations': ['Fixture.'], 'remediation': ''})
        preflight.start()
        self.addCleanup(preflight.stop)
        self.entities, self.writes, self.lookup_fails = {}, [], False
        client = Mock()
        client.invoke.side_effect = self.invoke
        started = patch.object(AdminOperations, '_home_client', side_effect=self.client_factory(client))
        started.start()
        self.addCleanup(started.stop)
        self.controller = AdminOperations(self.home)

    def client_factory(self, client):
        def factory(values=None):
            if self.lookup_fails:
                raise MemoryClientError('service_unavailable')
            return client
        return factory

    def entity(self, name, *, tags=()):
        identifier = str(uuid4())
        self.entities[identifier] = {'record_id': identifier, 'revision': 3, 'kind': 'entity',
                                     'classification': 'Sensitive', 'data_scope': 'owner', 'project_id': None,
                                     'data': {'entity_type': 'project', 'name': name, 'aliases': ['x']},
                                     'tags': list(tags)}
        return identifier

    def invoke(self, route, body):
        if route == '/v1/memory/search':
            from tools.memory.validation import validate
            validate(route, body)
            self.assertEqual({key: body[key] for key in ('kind', 'tag', 'scope')},
                             {'kind': 'entity', 'tag': 'maven-home', 'scope': 'universal'})
            ids = sorted(i for i, r in self.entities.items()
                         if 'maven-home' in r['tags'] and i > body.get('after_id', ''))
            page = ids[:body['limit']]
            return {'status': 'ready', 'data': {
                'records': [{'record_id': i, 'revision': self.entities[i]['revision']} for i in page],
                'next_after_id': page[-1] if len(ids) > len(page) else None}}
        if route == '/v1/memory/read':
            return {'status': 'ready', 'data': json.loads(json.dumps(self.entities[body['record_id']]))}
        if route == '/v1/memory/write':
            self.writes.append(body)
            record = self.entities[body['record_id']]
            self.assertEqual(body['expected_revision'], record['revision'])
            record.update(revision=record['revision'] + 1, tags=body['tags'], data=body['data'])
            return {'status': 'ready', 'data': {'record_id': body['record_id'], 'revision': record['revision']}}
        self.fail('Unexpected route ' + route)

    def enable_folder(self, root, project_id):
        root.mkdir(exist_ok=True)
        (root / '.maven').mkdir(mode=0o700, exist_ok=True)
        save_private(root / '.maven/project.json', {
            'schema': 2, 'project_id': project_id, 'root': str(root), 'git_enabled': False,
            'instance_id': self.instance, 'recovery_epoch': self.binding['connection']['recovery_epoch'],
            'remote': None, 'accepted_url': None, 'branch': None, 'checks': []})

    def fake_setup(self, created=None):
        def setup(args):
            project_id = created if args.create else args.project_id
            if args.create:
                self.entities[project_id] = {**self.entities.pop('pending'), 'record_id': project_id}
            self.enable_folder(args.root, project_id)
            return {'status': 'ready', 'project': {'project_id': project_id, 'root': str(args.root)}}
        return setup

    def answer(self, result, **values):
        question = result['pending_interaction']
        self.assertEqual(question['key'], 'home')
        response = {'mode': 'create', 'root': '', 'project_id': '', 'label': '', 'replace': False, **values}
        return self.controller.respond(result['operation_id'], question['interaction_id'],
                                       question['revision'], response)

    def approve(self, result):
        question = result['pending_interaction']
        self.assertEqual(question['kind'], 'review')
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                       {'approved': True, 'review_digest': question['review_digest'],
                                        'source': self.source})

    def test_create_mode_creates_project_saves_consent_and_tags_entity_with_exact_revision(self):
        from tools.home import load_home
        result = self.controller.begin('home', {})
        self.assertEqual(next(field['default'] for field in result['pending_interaction']['fields']
                              if field['name'] == 'mode'), 'create')
        root = self.base / 'Assistant'
        result = self.controller.advance(self.answer(result, root=str(root))['operation_id'])
        review = result['pending_interaction']['review']
        self.assertEqual(review['selection'], {'mode': 'create', 'root': str(root), 'project_id': None,
                                               'label': 'Assistant', 'replace': False})
        self.assertEqual(review['instance_id'], self.instance)
        self.assertIn(str(root), review['consent'])
        self.assertFalse(root.exists())
        result = self.approve(result)
        created = review['new_project_id']
        self.entities['pending'] = {'revision': 1, 'kind': 'entity', 'classification': 'Sensitive',
                                    'data_scope': 'owner', 'project_id': None, 'tags': [],
                                    'data': {'entity_type': 'project', 'name': 'Assistant', 'aliases': []}}
        original = json.loads(json.dumps(self.entities['pending']['data']))
        with patch('tools.project_setup.setup_project', side_effect=self.fake_setup(created)) as setup:
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        args = setup.call_args.args[0]
        self.assertTrue(args.create)
        self.assertEqual((args.name, args.source_origin, args.source_excerpt),
                         ('Assistant', self.source['origin'], self.source['excerpt']))
        home = load_home(self.home)
        self.assertEqual((home['root'], home['project_id'], home['label'], home['consent']),
                         (str(root), created, 'Assistant', self.source))
        self.assertEqual((self.home / 'home.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.writes), 1)
        write = self.writes[0]
        self.assertEqual((write['record_id'], write['expected_revision'], write['kind'], write['data_scope']),
                         (created, 1, 'entity', 'owner'))
        self.assertEqual(write['data'], original)
        self.assertEqual(write['tags'], ['maven-home'])
        self.assertNotIn('project_id', write)
        self.assertEqual(write['source'], self.source)

    def test_second_machine_offers_discovered_home_and_binds_existing_project_without_rewriting_tag(self):
        existing = self.entity('Nexus', tags=['maven-home'])
        result = self.controller.begin('home', {})
        defaults = {field['name']: field['default'] for field in result['pending_interaction']['fields']}
        self.assertEqual((defaults['mode'], defaults['project_id']), ('existing', existing))
        self.assertIn(existing, result['pending_interaction']['description'])
        root = self.base / 'nexus'
        root.mkdir()
        result = self.controller.advance(self.answer(result, mode='existing', root=str(root),
                                                     project_id=existing)['operation_id'])
        self.assertEqual(result['pending_interaction']['review']['replaces'], [])
        result = self.approve(result)
        with patch('tools.project_setup.setup_project', side_effect=self.fake_setup()) as setup:
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        args = setup.call_args.args[0]
        self.assertFalse(args.create)
        self.assertEqual(args.project_id, existing)
        self.assertEqual(self.writes, [])
        self.assertEqual(result['outcome']['entity_tag']['status'], 'unchanged')

    def test_enabled_folder_skips_project_setup_and_unreachable_lookup_is_not_fatal(self):
        project = self.entity('Personal')
        root = self.base / 'personal'
        self.enable_folder(root, project)
        self.lookup_fails = True
        result = self.controller.begin('home', {'root': str(root), 'label': 'Me'})
        self.assertEqual(result['state'], 'awaiting_decision')
        self.assertIn('unavailable', result['pending_interaction']['description'])
        self.lookup_fails = False
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(root), label='Me')['operation_id'])
        self.assertEqual(result['pending_interaction']['review']['selection']['project_id'], project)
        result = self.approve(result)
        with patch('tools.project_setup.setup_project') as setup:
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        setup.assert_not_called()
        self.assertEqual([write['tags'] for write in self.writes], [['maven-home']])
        self.assertEqual(self.entities[project]['data']['aliases'], ['x'])

    def test_unsafe_roots_and_mismatched_modes_are_refused_before_review(self):
        project = self.entity('Personal')
        enabled = self.base / 'enabled'
        self.enable_folder(enabled, project)
        full = self.base / 'full'
        full.mkdir()
        (full / 'notes.txt').write_text('keep')
        cases = [dict(mode='enabled', root=str(self.home)), dict(mode='enabled', root=str(self.base)),
                 dict(mode='create', root='relative/path'), dict(mode='create', root=str(self.home / 'inside')),
                 dict(mode='create', root=str(full)), dict(mode='existing', root=str(enabled), project_id=project),
                 dict(mode='existing', root=str(full), project_id='not-a-uuid'),
                 dict(mode='enabled', root=str(full))]
        for values in cases:
            with self.subTest(values=values):
                result = self.controller.begin('home', {})
                with self.assertRaises(ValueError):
                    self.answer(result, **values)
        self.assertFalse((self.home / 'home.json').exists())
        self.assertFalse((self.home / 'inside').exists())

    def test_second_home_needs_explicit_replace_and_approval_is_bound_to_review(self):
        self.entity('Old Home', tags=['maven-home'])
        project = self.entity('New Home')
        root = self.base / 'new-home'
        self.enable_folder(root, project)
        result = self.controller.begin('home', {})
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(root))['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('already MAVEN Home', result['error'])
        result = self.controller.begin('home', {})
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(root),
                                                     replace=True)['operation_id'])
        self.assertEqual(len(result['pending_interaction']['review']['replaces']), 1)
        result = self.approve(result)
        self.binding = {**self.binding, 'connection': {**self.binding['connection'], 'instance_id': str(uuid4())}}
        result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('candidate changed', result['error'])
        self.assertFalse((self.home / 'home.json').exists())
        self.assertEqual(self.writes, [])

    def test_replacement_review_lists_conflicts_beyond_first_search_page(self):
        homes = [self.entity(str(i), tags=['maven-home']) for i in range(21)]
        root = self.base / 'new-home'
        self.enable_folder(root, self.entity('Replacement'))
        result = self.controller.begin('home', {})
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(root),
                                                     replace=True)['operation_id'])
        self.assertEqual(result['pending_interaction']['review']['replaces'], sorted(homes))

    def test_disable_reviews_and_removes_only_home_selection(self):
        project = self.entity('Personal', tags=['maven-home'])
        root = self.base / 'personal'
        self.enable_folder(root, project)
        from tools.home import save_home
        save_home(self.home, root=root, project_id=project, instance_id=self.instance, label='Me',
                  consent=self.source)
        before = {path: path.read_bytes() for path in (root / '.maven/project.json', self.home / '.env')}
        result = self.controller.begin('home', {'action': 'disable'})
        self.assertEqual(result['pending_interaction']['review']['remove'], [str(self.home / 'home.json')])
        self.assertTrue((self.home / 'home.json').exists())
        result = self.controller.advance(self.approve(result)['operation_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertFalse((self.home / 'home.json').exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.entities[project]['tags'], ['maven-home'])
        again = self.controller.begin('home', {'action': 'disable'})
        self.assertEqual((again['state'], again['outcome']['status']), ('completed', 'unchanged'))

    def test_home_review_refuses_intervening_local_selection(self):
        from tools.home import save_home, load_home
        projects = [self.entity(name) for name in ('Old', 'Intervening', 'Replacement')]
        roots = [self.base / name for name in ('old', 'intervening', 'replacement')]
        for root, project in zip(roots, projects):
            self.enable_folder(root, project)
        save_home(self.home, root=roots[0], project_id=projects[0], instance_id=self.instance,
                  label='Old', consent=self.source)
        result = self.controller.begin('home', {})
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(roots[2]), replace=True)['operation_id'])
        result = self.approve(result)
        intervening = save_home(self.home, root=roots[1], project_id=projects[1], instance_id=self.instance,
                                label='Intervening', consent=self.source)
        result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(load_home(self.home), intervening)
        self.assertEqual(self.writes, [])

    def test_interrupted_disable_reconciles_original_removal_without_cloud(self):
        from tools.home import save_home, remove_home
        project = self.entity('Personal')
        root = self.base / 'personal'
        self.enable_folder(root, project)
        save_home(self.home, root=root, project_id=project, instance_id=self.instance,
                  label='Personal', consent=self.source)
        result = self.approve(self.controller.begin('home', {'action': 'disable'}))

        def interrupted(home):
            remove_home(home)
            raise KeyboardInterrupt()

        with patch('tools.home.remove_home', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.advance(result['operation_id'])
        with patch('tools.environment_migrations.prerequisite', side_effect=AssertionError('local-only')):
            result = AdminOperations(self.home).advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['outcome']['status'], 'removed')
        self.assertFalse((self.home / 'home.json').exists())
        self.assertEqual(self.writes, [])

    def test_interrupted_home_write_reuses_exact_selection(self):
        from tools.home import save_home, load_home
        project = self.entity('Personal')
        root = self.base / 'personal'
        self.enable_folder(root, project)
        result = self.controller.begin('home', {})
        result = self.controller.advance(self.answer(result, mode='enabled', root=str(root))['operation_id'])
        result = self.approve(result)

        def interrupted(*args, **kwargs):
            save_home(*args, **kwargs)
            raise KeyboardInterrupt()

        with patch('tools.home.save_home', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.advance(result['operation_id'])
        original = load_home(self.home)
        result = AdminOperations(self.home).advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(load_home(self.home), original)
        self.assertEqual(len(self.writes), 1)

    def test_home_parameters_are_validated(self):
        for parameters in ({'action': 'remove'}, {'mode': 'create'}, {'root': 7}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.controller.begin('home', parameters)


class CleanupAdministrationTests(unittest.TestCase):
    """Memory clean-up with mocked admin SQL functions and a real local runtime fixture."""

    def setUp(self):
        from tools.memory.boundary import save_private as save_runtime
        self.save_runtime = save_runtime
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / 'maven-home'
        self.home.mkdir(mode=0o700)
        self.instance = str(uuid4())
        self.source = {'origin': 'owner chat message 9', 'excerpt': 'Yes, go ahead with exactly that.'}
        values = {'MAVEN_AWS_PROFILE': 'owner', 'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:sts::123456789012:assumed-role/Owner/me'}
        self.session = object()
        admin_session = patch.object(AdminOperations, '_cleanup_admin',
                                     side_effect=lambda: (values, {'instance_id': self.instance}, self.session))
        admin_session.start()
        self.addCleanup(admin_session.stop)
        preflight = patch('tools.setup_common.permission_preflight', return_value={
            'status': 'Passed', 'checks': [], 'limitations': ['Fixture.'], 'remediation': ''})
        preflight.start()
        self.addCleanup(preflight.stop)
        self.records, self.erased = {}, []
        self.sql = {}
        for name, effect in (('cleanup_erased_ids', lambda config, session: list(self.erased)),
                             ('cleanup_candidates', None), ('cleanup_describe', self.describe),
                             ('forget_records', None), ('erase_records', None)):
            mocked = patch('tools.memory.admin.' + name, create=True, side_effect=effect)
            self.sql[name] = mocked.start()
            self.addCleanup(mocked.stop)
        self.root = self.base / 'project'
        self.root.mkdir()
        self.session_id = str(uuid4())
        save_runtime(self.home, 'runtime/sessions.json', {'schema': 1, 'reservations': {}, 'sessions': {
            self.session_id: {'root': str(self.root), 'project_id': str(uuid4()), 'state': 'active'}}})
        self.runtime = f'.maven/runtime/sessions/{self.session_id}'
        self.controller = AdminOperations(self.home)

    def record(self, kind='note', revision=2):
        identifier = str(uuid4())
        self.records[identifier] = {'record_id': identifier, 'found': True, 'kind': kind, 'scope': 'project',
                                    'project_id': str(uuid4()), 'title': 'Customer Acme notes',
                                    'revision': revision, 'visibility_state': 'visible', 'archived': False,
                                    'superseded': True, 'versions': revision, 'erased': False}
        return identifier

    def describe(self, config, session, ids):
        self.assertIs(session, self.session)
        return [dict(self.records[identifier]) if identifier in self.records
                else {'record_id': identifier, 'found': False} for identifier in ids]

    def approve(self, result):
        question = result['pending_interaction']
        self.assertEqual((question['kind'], question['key']), ('review', 'cleanup_review'))
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                       {'approved': True, 'review_digest': question['review_digest'],
                                        'source': self.source})

    def write_runtime(self, name, value):
        self.save_runtime(self.root, self.runtime + '/' + name, value)

    def read_runtime(self, name):
        return json.loads((self.root / self.runtime / name).read_text())

    @staticmethod
    def write_request(record_id, secret='Acme Corp private'):
        return {'kind': 'note', 'scope': 'project', 'record_id': record_id, 'expected_revision': 1,
                'reason': 'Note about ' + secret, 'source': {'origin': 'chat', 'excerpt': secret},
                'data': {'title': secret, 'body_markdown': secret}, 'tags': ['client:acme'],
                'request_key': str(uuid4())}

    def test_cleanup_review_groups_candidates_without_changing_memory_or_prior_receipts(self):
        old = str(uuid4())
        self.erased = [old]
        self.write_runtime('last-memory-write.json', {
            'route': 'write', 'request_key': 'key-1', 'record': {'record_id': old, 'revision': 2},
            'result': {'record_id': old, 'revision': 2, 'request_key': 'key-1', 'data': {'title': 'Acme'}}})
        candidates = [{'record_id': str(uuid4()), 'suggested': suggested, 'reasons': [reason], 'title': reason}
                      for suggested, reason in (('erase', 'superseded'), ('forget', 'duplicate'),
                                                ('archive', 'stale_task'), ('forget', 'archived'))]
        self.sql['cleanup_candidates'].side_effect = None
        self.sql['cleanup_candidates'].return_value = {'candidates': candidates, 'truncated': False}
        result = self.controller.begin('cleanup', {'action': 'review'})
        self.assertEqual(result['state'], 'completed', result.get('error'))
        outcome = result['outcome']
        self.assertTrue(outcome['read_only'])
        self.assertFalse(outcome['cloud_changed'])
        self.assertEqual(outcome['total'], 4)
        self.assertEqual({key: [item['reasons'][0] for item in value] for key, value in outcome['groups'].items()},
                         {'erase': ['superseded'], 'forget': ['duplicate', 'archived'], 'archive': ['stale_task']})
        self.assertIn('separate reviewed', outcome['next'])
        self.assertNotIn('local_sweep', outcome)
        self.assertEqual(self.read_runtime('last-memory-write.json')['result']['data'], {'title': 'Acme'})
        for name in ('forget_records', 'erase_records', 'cleanup_describe', 'cleanup_erased_ids'):
            self.sql[name].assert_not_called()

    def test_forget_and_restore_apply_only_after_approval_with_the_review_digest(self):
        for action in ('forget', 'restore'):
            with self.subTest(action=action):
                first, second = self.record(), self.record()
                self.sql['forget_records'].reset_mock()
                self.sql['forget_records'].side_effect = None
                self.sql['forget_records'].return_value = {'receipt_id': str(uuid4()), 'changed': [first],
                                                           'unchanged': [second]}
                result = self.controller.begin('cleanup', {'action': action, 'record_ids': [first, second],
                                                           'reason': 'Obsolete duplicates'})
                question = result['pending_interaction']
                review = question['review']
                self.assertEqual((review['action'], review['record_ids'], review['versions_affected'], review['caveats']),
                                 (action, [first, second], 4, []))
                self.assertEqual([record['record_id'] for record in review['records']], [first, second])
                self.sql['forget_records'].assert_not_called()
                declined = self.controller.respond(result['operation_id'], question['interaction_id'],
                                                   question['revision'], {'approved': False,
                                                                          'review_digest': question['review_digest']})
                self.assertEqual(self.controller.advance(declined['operation_id'])['state'], 'paused')
                self.sql['forget_records'].assert_not_called()
                result = self.controller.advance(self.approve(declined)['operation_id'])
                self.assertEqual(result['state'], 'completed', result.get('error'))
                call = self.sql['forget_records'].call_args
                self.assertEqual(call.args[2], [first, second])
                self.assertEqual(call.kwargs['restore'], action == 'restore')
                self.assertEqual(call.kwargs['approval_digest'], question['review_digest'])
                self.assertEqual(call.kwargs['reason'], 'Obsolete duplicates')
                self.assertIn(result['operation_id'], call.kwargs['actor'])
                self.assertEqual((result['outcome']['changed'], result['outcome']['unchanged']), ([first], [second]))

    def test_form_selection_and_changed_records_invalidate_approval(self):
        target = self.record()
        result = self.controller.begin('cleanup', {'action': 'forget'})
        question = result['pending_interaction']
        self.assertEqual(question['key'], 'cleanup_selection')
        for bad in ('', 'not-a-uuid', '\n'.join(str(uuid4()) for _ in range(201)), target + '\n' + target):
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                        {'record_ids': bad, 'reason': 'Old'})
        result = self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                         {'record_ids': target + '\n', 'reason': 'Old'})
        result = self.approve(self.controller.advance(result['operation_id']))
        self.records[target]['revision'] += 1
        result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('Reviewed candidate changed', result['error'])
        self.sql['forget_records'].assert_not_called()

    def test_empty_oversized_and_invalid_selections_are_refused(self):
        valid = str(uuid4())
        for parameters in ({'action': 'forget', 'record_ids': []},
                           {'action': 'erase', 'record_ids': [str(uuid4()) for _ in range(201)]},
                           {'action': 'forget', 'record_ids': [valid, valid]},
                           {'action': 'forget', 'record_ids': [valid.upper()]},
                           {'action': 'forget', 'record_ids': 'all'},
                           {'action': 'forget', 'record_ids': [valid], 'reason': ''},
                           {'action': 'review', 'record_ids': [valid]},
                           {'action': 'delete', 'record_ids': [valid]}, {}):
            with self.subTest(parameters=str(parameters)[:60]), self.assertRaises(ValueError):
                self.controller.begin('cleanup', parameters)
        self.sql['cleanup_erased_ids'].assert_not_called()
        lifecycle, missing = self.record(kind='session'), str(uuid4())
        for ids, message in (([lifecycle], 'not cleaned up'), ([missing], 'not found')):
            result = self.controller.begin('cleanup', {'action': 'forget', 'record_ids': ids, 'reason': 'x'})
            self.assertEqual(result['state'], 'blocked')
            self.assertIn(message, result['error'])
        self.sql['forget_records'].assert_not_called()

    def test_lost_cleanup_response_replays_exact_original_request_without_redescribe(self):
        target = self.record()
        result = self.controller.begin('cleanup', {
            'action': 'erase', 'record_ids': [target], 'reason': 'Obsolete record'})
        original_digest = result['pending_interaction']['review_digest']
        result = self.approve(result)
        receipt = {'receipt_id': str(uuid4()), 'records': [{'record_id': target, 'versions': 2}]}

        def lost(*args, **kwargs):
            self.records[target].update(erased=True, revision=3)
            raise MemoryClientError('operation_outcome_unknown')

        self.sql['erase_records'].side_effect = lost
        result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'uncertain')
        original_call = self.sql['erase_records'].call_args
        self.assertEqual(original_call.kwargs['operation_id'], result['operation_id'])
        self.assertEqual(original_call.kwargs['expected_revisions'], {target: 2})
        self.assertEqual(original_call.kwargs['approval_digest'], original_digest)
        self.sql['cleanup_describe'].side_effect = AssertionError('Replay must not re-describe erased data')
        self.sql['erase_records'].side_effect = None
        self.sql['erase_records'].return_value = receipt
        result = AdminOperations(self.home).advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(self.sql['erase_records'].call_args, original_call)
        self.assertEqual(result['outcome']['receipt_id'], receipt['receipt_id'])

    def test_erase_scrubs_resolved_administration_titles_and_keeps_approval_digest(self):
        target = self.record()
        title = self.records[target]['title']
        self.source = {**self.source, 'excerpt': title}
        self.sql['cleanup_candidates'].return_value = {'candidates': [
            {**self.records[target], 'suggested': 'erase'}], 'truncated': False}
        prior = self.controller.begin('cleanup', {'action': 'review'})
        result = self.controller.begin('cleanup', {
            'action': 'erase', 'record_ids': [target], 'reason': 'Obsolete record'})
        digest = result['pending_interaction']['review_digest']
        self.sql['erase_records'].return_value = {'receipt_id': str(uuid4()), 'records': []}
        result = self.controller.advance(self.approve(result)['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(result['approvals'][-1]['review_digest'], digest)
        for identifier in (prior['operation_id'], result['operation_id']):
            self.assertNotIn(title, self.controller._path(identifier).read_text())
        self.assertIn(target, self.controller._path(result['operation_id']).read_text())

    def test_unresolved_administration_request_blocks_erasure_without_redaction(self):
        target = self.record()
        pending = self.controller.begin('cleanup', {
            'action': 'forget', 'record_ids': [target], 'reason': 'Duplicate'})
        before = self.controller._path(pending['operation_id']).read_bytes()
        result = self.controller.begin('cleanup', {
            'action': 'erase', 'record_ids': [target], 'reason': 'Obsolete record'})
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('unresolved selected work', result['error'])
        self.assertEqual(self.controller._path(pending['operation_id']).read_bytes(), before)
        self.sql['erase_records'].assert_not_called()

    def test_erase_lists_caveats_and_sweeps_only_erased_records_content(self):
        from tools.administration import ERASE_CAVEATS
        target, other = self.record(), self.record()
        kept = self.write_request(other, 'Keep me')
        swept = self.write_request(target)
        self.write_runtime('maintenance.json', {'schema': 1, 'assignments': {'op-1': {
            'operation_id': 'op-1', 'state': 'completed', 'effects': [
                {'state': 'saved', 'request': swept, 'record': {'record_id': target, 'revision': 2}},
                {'state': 'saved', 'request': kept, 'record': {'record_id': other, 'revision': 2}}]}}})
        self.write_runtime('last-memory-write.json', {
            'route': 'write', 'request_key': swept['request_key'], 'record': {'record_id': target, 'revision': 2},
            'result': {'record_id': target, 'revision': 2, 'request_key': swept['request_key'],
                       'replayed': False, 'data': {'title': 'Acme Corp private'}}})
        from tools.memory.boundary import digest
        failed = {'route': 'write', 'body': self.write_request(target), 'session_id': self.session_id}
        failed['digest'] = digest(failed)
        self.write_runtime('failed-memory-write.json', {'request': failed, 'status': 'revision_conflict'})
        manifest = {'manifest': {'session_id': self.session_id, 'memory_refs': [{'record_id': target, 'revision': 2}],
                                 'summary': 'Checkpoint'}, 'status': 'checkpointed'}
        self.write_runtime('checkpoints/' + str(uuid4()) + '.json', manifest)
        result = self.controller.begin('cleanup', {'action': 'erase', 'record_ids': [target], 'reason': 'Client data'})
        review = result['pending_interaction']['review']
        self.assertEqual(review['caveats'], list(ERASE_CAVEATS))
        text = ' '.join(review['caveats'])
        self.assertIn('30 days', text)
        self.assertIn('Read-only inspection never changes a cache.', text)
        self.assertIn('cannot be undone', text)
        self.sql['erase_records'].assert_not_called()
        receipt = str(uuid4())
        tombstone = {'erased': True, 'erased_at': '2026-11-11T00:00:00Z', 'receipt_id': receipt}
        self.sql['erase_records'].side_effect = None
        self.sql['erase_records'].return_value = {'receipt_id': receipt, 'erased_at': tombstone['erased_at'],
                                                  'tombstone': tombstone,
                                                  'records': [{'record_id': target, 'versions': 2}]}
        digest = result['pending_interaction']['review_digest']
        result = self.controller.advance(self.approve(result)['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(self.sql['erase_records'].call_args.kwargs['approval_digest'], digest)
        touched = sorted(result['outcome']['local_sweep']['files_touched'])
        self.assertEqual(touched, sorted(str(self.root / self.runtime / name) for name in
                                         ('failed-memory-write.json', 'last-memory-write.json', 'maintenance.json')))
        effects = self.read_runtime('maintenance.json')['assignments']['op-1']['effects']
        self.assertEqual(effects[0]['request']['data'], tombstone)
        self.assertEqual(effects[0]['request']['tags'], ['erased'])
        self.assertEqual((effects[0]['request']['request_key'], effects[0]['record']),
                         (swept['request_key'], {'record_id': target, 'revision': 2}))
        self.assertEqual(effects[1]['request'], kept)
        self.assertEqual(self.read_runtime('last-memory-write.json')['result'],
                         {'record_id': target, 'revision': 2, 'request_key': swept['request_key'], 'replayed': False})
        body = self.read_runtime('failed-memory-write.json')['request']['body']
        self.assertEqual((body['data'], body['request_key']), (tombstone, failed['body']['request_key']))
        everything = ''.join(path.read_text() for path in (self.root / self.runtime).rglob('*.json'))
        self.assertEqual(everything.count('Acme Corp private'), 0)
        self.assertIn('Keep me', everything)
        self.assertEqual(json.loads(next((self.root / self.runtime / 'checkpoints').iterdir()).read_text()), manifest)

    def test_legacy_failed_request_keeps_outer_digest_format_when_scrubbed(self):
        from tools.administration import sweep_erased, ERASED_LOCAL
        from tools.memory.boundary import digest
        target = self.record()
        request = {'route': 'write', 'body': self.write_request(target), 'session_id': self.session_id}
        self.write_runtime('failed-memory-write.json', {
            'request': request, 'status': 'revision_conflict', 'pending_digest': digest(request)})
        sweep_erased([self.root], [target], ERASED_LOCAL)
        saved = self.read_runtime('failed-memory-write.json')
        self.assertNotIn('digest', saved['request'])
        self.assertEqual(saved['pending_digest'], digest(saved['request']))
        self.assertEqual(saved['request']['body']['request_key'], request['body']['request_key'])
        self.assertEqual(saved['request']['body']['data'], ERASED_LOCAL)

    def test_pending_journal_for_erased_record_refuses_erase_and_sweep(self):
        from tools.administration import sweep_erased
        target = self.record()
        pending = {'route': 'write', 'body': self.write_request(target), 'session_id': self.session_id}
        self.write_runtime('pending-memory.json', pending)
        last = {'route': 'write', 'request_key': 'k', 'record': {'record_id': target, 'revision': 2},
                'result': {'record_id': target, 'revision': 2, 'data': 'Acme'}}
        self.write_runtime('last-memory-write.json', last)
        result = self.controller.begin('cleanup', {'action': 'erase', 'record_ids': [target], 'reason': 'x'})
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('pending-memory.json', result['error'])
        self.sql['erase_records'].assert_not_called()
        with self.assertRaises(SetupPending):
            sweep_erased([str(self.root)], [target], {'erased': True})
        self.assertEqual(self.read_runtime('pending-memory.json'), pending)
        self.assertEqual(self.read_runtime('last-memory-write.json'), last)
        # Inspection can list candidates but never sweeps or clears unresolved evidence.
        self.erased = [target]
        inspected = self.controller.begin('cleanup', {'action': 'review'})
        self.assertEqual(inspected['state'], 'completed')
        self.assertTrue(inspected['outcome']['read_only'])
        self.assertEqual(self.read_runtime('pending-memory.json'), pending)
        self.assertEqual(self.read_runtime('last-memory-write.json'), last)
        self.sql['erase_records'].assert_not_called()
        (self.root / self.runtime / 'pending-memory.json').unlink()
        self.write_runtime('maintenance.json', {'schema': 1, 'assignments': {'op-2': {
            'operation_id': 'op-2', 'state': 'uncertain',
            'effects': [{'state': 'uncertain', 'request': self.write_request(target)}]}}})
        with self.assertRaises(SetupPending) as raised:
            sweep_erased([str(self.root)], [target], {'erased': True})
        self.assertIn('unresolved effect', str(raised.exception))
        self.assertEqual(self.read_runtime('last-memory-write.json'), last)




if __name__ == '__main__':
    unittest.main()
