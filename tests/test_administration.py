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

    def respond(self, result, values=None):
        question = result['pending_interaction']
        if question['kind'] == 'review':
            values = {'approved': True, 'review_digest': question['review_digest'],
                      'source': self.source, 'confirmations': question['confirmations']}
        elif question['key'] == 'selection':
            names = {field['name'] for field in question['fields']}
            values = {key: value for key, value in values.items() if key in names}
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

    def starter_review(self):
        result = self.controller.begin('setup', {'mode': 'new'})
        state = self.controller._load(result['operation_id'])
        state.update(step='starter_prepare_review', pending_interaction=None, state='pending',
                     starter_binding=dict(Wizard(self.home).values))
        self.controller._save(state)
        return self.controller.advance(result['operation_id'])

    def starter_client(self, pages):
        from tools.memory.validation import digest
        by_id = {page['proposal_id']: page for page in pages}
        active, uploaded = [], []

        def invoke(route, body):
            if route == '/v1/proposals/create':
                uploaded.append(body['proposal_id'])
                return {'status': 'ready'}
            if route == '/v1/operations/lookup':
                return {'status': 'ready'}
            draft = by_id[body['proposal_id']]
            if route == '/v1/proposals/challenge':
                return {'status': 'ready', 'data': {
                    'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'],
                    'expected_revision': 0, 'candidate_hash': digest(draft['candidate']),
                    'challenge_id': str(uuid4()),
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}
            if route == '/v1/proposals/resolve':
                self.assertEqual(body['candidate_hash'], digest(draft['candidate']))
                active.append(body)
                return {'status': 'active', 'data': {'page_id': draft['page_id'], 'revision': 1}}
            self.fail('Unexpected route: ' + route)

        client = Mock(invoke=Mock(side_effect=invoke))
        client.read_full.side_effect = lambda target: {'data': {
            'content_hash': digest(by_id[target['proposal_id']]['candidate'])}}
        return client, uploaded, active

    def test_one_collection_approval_installs_nine_pages_and_survives_resume(self):
        result = self.starter_review()
        question = result['pending_interaction']
        pages = question['review']['pages']
        self.assertEqual(len(pages), 9)
        self.assertEqual(question['key'], 'starter_collection')
        client, uploaded, active = self.starter_client(pages)
        with patch('tools.memory.environment.configured_client', return_value=client):
            paused = self.controller.respond(result['operation_id'], question['interaction_id'],
                question['revision'], {'approved': False, 'review_digest': question['review_digest']})
            client.invoke.assert_not_called()
            result = self.respond(paused)
            for _ in range(60):
                if result['step'] == 'verify':
                    break
                self.assertIsNone(result['pending_interaction'])
                self.controller = AdminOperations(self.home)
                result = self.controller.advance(result['operation_id'])
                self.assertNotIn(result['state'], ('blocked', 'uncertain'), result.get('error'))
        self.assertEqual(result['step'], 'verify')
        self.assertEqual(uploaded, [page['proposal_id'] for page in pages])
        self.assertEqual([item['proposal_id'] for item in active], uploaded)
        self.assertTrue(all(item['approval_source'] == self.source for item in active))
        self.assertEqual(len(result['approvals']), 1)
        self.assertEqual(result['starter_progress']['completed'], 9)

    def test_collection_approval_rejects_changes_to_a_later_page_before_any_submission(self):
        from tools.memory.validation import digest
        result = self.respond(self.starter_review())
        wizard = self.controller._wizard(self.controller._load(result['operation_id']))
        directory = wizard.directory / 'starter'
        manifest = read_private(directory / 'manifest.json')
        page = manifest['pages'][-1]
        path = directory / page['file']
        changed = read_private(path)
        changed['candidate']['body_markdown'] += '\\nChanged after review.'
        page['candidate_hash'] = digest(changed['candidate'])
        save_json(path, changed)
        save_json(directory / 'manifest.json', manifest)
        with patch('tools.memory.environment.configured_client') as client:
            result = self.controller.advance(result['operation_id'])
        client.assert_not_called()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('Reviewed candidate changed', result['error'])

    def test_old_individual_review_becomes_one_remaining_collection_review_without_replaying_pages(self):
        result = self.starter_review()
        pages = result['pending_interaction']['review']['pages']
        state = self.controller._load(result['operation_id'])
        state.update(step='starter_review', starter_index=5)
        self.controller._review(state, 'starter_submit', 'Old individual review', pages[5], 'starter_submit')
        self.controller._save(state)
        old = state['pending_interaction']
        before = dict(pages[5])
        with self.assertRaises(ValueError):
            self.controller.respond(result['operation_id'], old['interaction_id'], old['revision'],
                {'approved': True, 'review_digest': old['review_digest'], 'source': self.source})
        self.assertIn('advance', self.controller.status(result['operation_id'])['next_actions'])
        result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['pending_interaction']['key'], 'starter_collection')
        self.assertEqual(result['pending_interaction']['review']['pages'][5], before)
        result = self.respond(result)
        client, uploaded, active = self.starter_client(pages)
        with patch('tools.memory.environment.configured_client', return_value=client):
            for _ in range(30):
                if result['step'] == 'verify':
                    break
                self.assertIsNone(result['pending_interaction'])
                result = self.controller.advance(result['operation_id'])
                self.assertNotIn(result['state'], ('blocked', 'uncertain'), result.get('error'))
        self.assertEqual(result['step'], 'verify')
        self.assertEqual(uploaded, [page['proposal_id'] for page in pages[5:]])
        self.assertEqual(len(active), 4)
        self.assertEqual(len(result['approvals']), 1)


    def test_existing_setup_reaches_verified_readiness_without_provisioning(self):
        wizard = Wizard(self.home)
        instance = str(uuid4())
        wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': instance, 'MAVEN_AWS_REGION': 'us-east-1'})
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        config = {'instance_id': instance, 'schema_version': 2}
        with patch('tools.administration._connection_config', return_value=config), \
                patch('tools.setup_common.inspect_login_identity', return_value=identity), \
                patch.object(Wizard, 'verify_authority', return_value=17), \
                patch('tools.memory.bootstrap.prepare') as prepare, \
                patch('tools.memory.bootstrap.apply') as apply, \
                patch('tools.memory.bootstrap.initialize') as initialize:
            result = self.controller.begin('setup', {})
            answers = {'selection': {'mode': 'existing', 'connection': '', 'region': 'us-east-1'},
                       'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
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
            self.assertFalse(result['outcome']['project_configured'])
            self.assertNotIn('optional_project', result['answers'])
            self.assertNotIn('deploy_identity', result['answers'])
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
        document = {'review': {'change_set': {'Changes': [{'ResourceChange': change} for change in stack_changes]}},
                    'review_sha256': 'f' * 64}
        patches = [
            patch('tools.administration._connection_config', return_value={'instance_id': instance, 'schema_version': 2}),
            patch('tools.setup_common.inspect_login_identity', return_value=identity),
            patch.object(Wizard, 'verify_authority', return_value=21),
            patch('tools.update.local_report', return_value={'current': True, 'checkout': str(self.base)}),
            patch('tools.update.running_software_stale', return_value=False),
            patch('tools.update.schema_status', return_value={'state': 'pending', 'applied': 11,
                                                              'pending': ['012_agentic_memory.sql']}),
            patch('tools.update.build_service_package', side_effect=lambda stack, path, instance: path.mkdir()),
            patch('tools.memory.bootstrap._cloud', return_value=cloud),
            patch('tools.memory.bootstrap._package', return_value=(
                {'deployment_inputs': {'InstanceId': instance}}, json.dumps({'deployed': 'new'}), [])),
            patch('tools.memory.bootstrap.plan', side_effect=lambda values, package, path, change_type: save_json(path, document)),
            patch('tools.memory.environment.configured_client'),
            patch('tools.update.authority_changes', return_value=[]),
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

    def test_update_migrates_schema_then_updates_service_with_reviews(self):
        mocks = self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction',
                                      'ResourceType': 'AWS::Lambda::Function', 'Replacement': 'False'}])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
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

    def test_update_refuses_service_change_that_replaces_data_resources(self):
        self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'DatabaseCluster',
                              'ResourceType': 'AWS::RDS::DBCluster', 'Replacement': 'True'}])
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')}}
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}), \
                patch('tools.memory.bootstrap.apply') as apply:
            result = self.run_update(answers)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('DatabaseCluster (AWS::RDS::DBCluster)', result['error'])
        apply.assert_not_called()
        self.assertFalse(any('changes' in approval['review'] for approval in result['approvals']))

    def test_update_offers_instruction_changes_as_one_collection_review(self):
        from tools.memory.onboarding import catalog
        candidate = catalog()[0][0]
        mocks = self.update_fixture([{'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction',
                                      'ResourceType': 'AWS::Lambda::Function', 'Replacement': 'False'}])
        mocks['authority_changes'].return_value = [{
            'slug': candidate['slug'], 'title': candidate['title'], 'action': 'update', 'page_id': str(uuid4()),
            'revision': 3, 'candidate': candidate, 'diff': '-old\n+new\n'}]
        answers = {'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                   'deploy_identity': {'profile': 'maintainer', 'cache': str(self.home / 'runtime/deploy-login')},
                   'authority_update': {'update': True}}
        with patch('tools.memory.bootstrap.initialize', return_value={'status': 'ready'}), \
                patch('tools.memory.bootstrap.apply', return_value={'status': 'connected'}):
            offered = self.run_update(answers, stop_at='authority_update')
            self.assertIn('-old\n+new', offered['pending_interaction']['details'])
            result = self.respond(offered, answers['authority_update'])
            result = self.advance_to_question(result)
        review = result['pending_interaction']
        self.assertEqual(review['key'], 'starter_collection')
        self.assertEqual(review['title'], 'Update MAVEN instructions — one approval')
        page = review['review']['pages'][0]
        self.assertEqual((page['page_id'], page['expected_revision']),
                         (mocks['authority_changes'].return_value[0]['page_id'], 3))

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
        with patch('tools.memory.bootstrap.apply') as cloud:
            review = review_restart(self.home, identifier)
            result = restart(self.home, identifier, confirm_review=_digest(review), source=self.source)
            successor = result['operation_id']
            self.assertNotEqual(successor, identifier)
            self.assertEqual(Wizard(self.home).values, {})
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

    def test_restart_rejects_stale_review_and_preserves_active_sessions(self):
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
        with self.assertRaises(ValueError):
            review_restart(self.home, identifier)
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

    def test_fresh_setup_preserves_exact_requests_and_chat_activation_provenance(self):
        instance = str(uuid4())
        identity = {'Arn': 'arn:aws:iam::123456789012:user/owner', 'Account': '123456789012'}
        config = {'instance_id': instance, 'schema_version': 2}
        draft = {'request_key': str(uuid4()), 'proposal_id': str(uuid4()), 'page_id': str(uuid4()),
                 'expected_revision': 0, 'classification': 'Sensitive', 'data_scope': 'owner',
                 'candidate': {'slug': 'maven', 'page_kind': 'directive', 'title': 'MAVEN', 'requires': [],
                               'body_markdown': 'Use explicit owner approval.', 'scope': {}, 'authority_state': 'active'},
                 'source': self.source, 'change_reason': 'Reviewed starter proposal'}
        from tools.memory.validation import digest
        candidate_hash = digest(draft['candidate'])
        request_calls = []
        lost = [False]

        def prepare_package(values, destination):
            destination.mkdir(mode=0o700)
            return {'status': 'prepared'}

        def apply_package(values, package, review, env, **kwargs):
            Wizard(self.home).settings({'MAVEN_MEMORY_INSTANCE_ID': instance})
            return {'status': 'connected', 'instance_id': instance}

        def plan(values, package, destination, **kwargs):
            save_json(destination, {'review_sha256': 'a' * 64})
            return {'status': 'planned'}

        def prepare_starter(destination, **kwargs):
            destination.mkdir(mode=0o700)
            save_json(destination / 'maven.json', draft)
            save_json(destination / 'manifest.json', {'pages': [{'slug': 'maven', 'file': 'maven.json',
                'candidate_hash': candidate_hash, **{key: draft[key] for key in ('proposal_id', 'page_id', 'request_key')}}]})

        def invoke(route, body):
            if route == '/v1/proposals/challenge':
                return {'status': 'ready', 'data': {'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'],
                    'expected_revision': 0, 'candidate_hash': candidate_hash, 'challenge_id': str(uuid4()),
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}
            if route == '/v1/proposals/resolve':
                request_calls.append(dict(body))
                if not lost[0]:
                    lost[0] = True
                    raise MemoryClientError('operation_outcome_unknown')
                return {'status': 'active', 'data': {'page_id': draft['page_id'], 'revision': 1}}
            return {'status': 'ready'}

        client = Mock(invoke=Mock(side_effect=invoke))
        client.read_full.return_value = {'data': {'content_hash': candidate_hash}}
        with patch('tools.setup_common.inspect_login_identity', return_value=identity), \
                patch('tools.administration._connection_config', return_value=config), \
                patch('tools.memory.bootstrap.prepare', side_effect=prepare_package), \
                patch('tools.memory.bootstrap._package', return_value=({'deployment_inputs': {'InstanceId': instance}, 'artifact_sha256': {}}, '{}', [])), \
                patch('tools.memory.bootstrap.plan', side_effect=plan), \
                patch('tools.memory.bootstrap.apply', side_effect=apply_package), \
                patch('tools.memory.bootstrap.initialize', return_value={'status': 'initialized'}), \
                patch('tools.memory.onboarding.prepare', side_effect=prepare_starter), \
                patch('tools.memory.environment.configured_client', return_value=client), \
                patch.object(Wizard, 'verify_authority', return_value=18):
            result = self.controller.begin('setup', {'mode': 'new'})
            answers = {'selection': {'mode': 'new', 'connection': '', 'region': 'us-east-1'},
                       'identity': {'profile': 'owner', 'cache': str(self.home / 'runtime/login')},
                       'deploy_identity': {'profile': 'deployment', 'cache': str(self.home / 'runtime/deploy-login')},
                       'provision': {'MAVEN_STACK_NAME': 'test', 'MAVEN_INSTANCE_NAME': 'test',
                           'MAVEN_NOTIFICATION_EMAIL': 'owner@example.com', 'MAVEN_AVAILABILITY_ZONES': 'us-east-1a,us-east-1b', 'MAVEN_ENGINE_VERSION': '18.6'}}
            for _ in range(60):
                result = self.advance_to_question(result)
                if result['state'] == 'completed':
                    break
                if result['state'] == 'uncertain':
                    before = (self.controller.directory / (result['operation_id'] + '.json')).read_bytes()
                    AdminOperations(self.home).status(result['operation_id'])
                    self.assertEqual((self.controller.directory / (result['operation_id'] + '.json')).read_bytes(), before)
                    self.controller = AdminOperations(self.home)
                    result = self.controller.advance(result['operation_id'])
                    continue
                self.assertNotEqual(result['state'], 'blocked', result)
                result = self.respond(result, answers.get(result['pending_interaction']['key']))
            self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(request_calls), 2)
        self.assertEqual(request_calls[0], request_calls[1])
        self.assertEqual(request_calls[0]['approval_source'], self.source)
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
            self.assertIn('harnesses-stopped', result['handoff']['required_external_attestations'])
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

    def test_every_starter_phase_refuses_changed_instance_or_identity_before_cloud_dispatch(self):
        result = self.controller.begin('setup', {})
        wizard = Wizard(self.home)
        original = {'MAVEN_MEMORY_INSTANCE_ID': str(uuid4()), 'MAVEN_AWS_PROFILE': 'original-owner'}
        wizard.settings(original)
        binding = dict(wizard.values)
        for field, changed in (('MAVEN_MEMORY_INSTANCE_ID', str(uuid4())), ('MAVEN_AWS_PROFILE', 'other-owner')):
            for phase in ('starter_prepare_review', 'starter_prepare', 'starter_submit',
                          'starter_challenge', 'starter_activate', 'starter_renew'):
                with self.subTest(field=field, phase=phase):
                    wizard = Wizard(self.home)
                    wizard.settings({**original, field: changed})
                    state = self.controller._load(result['operation_id'])
                    state.update(step=phase, state='pending', pending_interaction=None, starter_binding=binding)
                    self.controller._save(state)
                    with patch('tools.memory.environment.configured_client') as client:
                        observed = AdminOperations(self.home).advance(result['operation_id'])
                        self.assertEqual(observed['state'], 'blocked')
                        self.assertIn('original initialization', observed['error'])
                        client.assert_not_called()

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
            self.assertEqual(result['pending_interaction']['key'], 'identity')
            self.assertEqual(Wizard(self.home).values['MAVEN_STACK_NAME'], second['StackId'])
            self.assertNotIn('MAVEN_MEMORY_INSTANCE_ID', Wizard(self.home).values)
            result = self.respond(result, {'profile': 'daily', 'cache': str(self.home / 'runtime/daily')})
            result = self.advance_to_question(self.respond(self.advance_to_question(result)))
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
            self.assertEqual(recovered['pending_interaction']['key'], 'deploy_identity')
            self.assertEqual(recovered['identity_return_step'], 'discover')
            self.assertEqual(recovered['operation_id'], identifier)
            self.assertEqual(recovered['approvals'], state['approvals'])
            self.assertEqual(recovered['receipts'], state['receipts'])
            aws.assert_not_called()
            self.preflight.assert_not_called()

    def test_unknown_account_offers_typed_external_login_without_starting_it(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        result = self.respond(result, {'mode': 'existing', 'connection': 'saved-stack', 'region': 'us-east-1'})
        result = self.advance_to_question(result)
        result = self.advance_to_question(self.respond(result))
        question = result['pending_interaction']
        self.assertIn('Unknown', question['description'])
        self.assertTrue(all(field['help'] for field in question['fields']))
        result = self.respond(result, {'profile': 'daily-owner', 'cache': str(self.home / 'runtime/daily-login')})
        with patch('tools.setup_common.inspect_login_identity', side_effect=RuntimeError('expired')), \
                patch('subprocess.run') as launch:
            result = self.controller.advance(result['operation_id'])
        launch.assert_not_called()
        self.assertEqual(result['handoff']['type'], 'aws_login')
        self.assertEqual(result['handoff']['command'],
                         ['aws', 'login', '--profile', 'daily-owner', '--region', 'us-east-1'])
        self.assertIn('Unknown', result['handoff']['account'])
        self.assertEqual(result['handoff']['environment'],
                         {'AWS_LOGIN_CACHE_DIRECTORY': str(self.home / 'runtime/daily-login')})
        self.assertFalse(result['presentation']['verified'])

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

    def test_old_optional_project_checkpoint_finishes_without_project_mutation(self):
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
        with patch.object(Wizard, 'verify_authority', return_value=19), \
                patch('tools.project_setup.setup_project') as project:
            result = self.controller.advance(result['operation_id'])
        project.assert_not_called()
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['outcome']['instance_ready'])
        self.assertFalse(result['outcome']['project_configured'])
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
        with patch('tools.setup_common.inspect_login_identity', side_effect=RuntimeError('expired')), \
                patch('tools.uninstall.cli.build_plan') as plan, \
                patch('subprocess.run') as launch:
            result = self.controller.begin('uninstall', {'cloud': True})
        plan.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(result['handoff']['type'], 'aws_login')
        self.assertIn('Maintenance', result['handoff']['purpose'])
        self.assertEqual(result['handoff']['account'], '123456789012')

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
                raise RuntimeError('expired')
            return {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/daily'}
        with patch.object(AdminOperations, '_setup', side_effect=MemoryClientError('selected_login_unavailable_or_expired')), \
                patch('tools.setup_common.inspect_login_identity', side_effect=identity):
            result = self.controller.advance(result['operation_id'])
        self.assertEqual((result['state'], result['step']), ('uncertain', 'update_service_apply'))
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
        selected.assert_called_once_with(config, admin=True, approval=False, environment=values)
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

    def test_missing_credentials_require_signin_without_treating_them_as_static_keys(self):
        from botocore.exceptions import NoCredentialsError
        session = Mock()
        session.get_credentials.return_value = None
        values = {'MAVEN_AWS_PROFILE': 'daily', 'MAVEN_AWS_LOGIN_CACHE': '/private/cache',
                  'MAVEN_AWS_REGION': 'us-east-1'}
        with patch('boto3.Session', return_value=session):
            with self.assertRaises(NoCredentialsError):
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
        def factory():
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
            self.assertEqual(body, {'kind': 'entity', 'tag': 'maven-home', 'scope': 'universal'})
            return {'status': 'ready', 'data': {'records': [
                record for record in self.entities.values() if 'maven-home' in record['tags']]}}
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
            'schema': 1, 'project_id': project_id, 'root': str(root), 'git_enabled': False,
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
        created = str(uuid4())
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

    def test_review_is_read_only_groups_candidates_and_sweeps_earlier_erasures(self):
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
        self.assertEqual(outcome['total'], 4)
        self.assertEqual({key: [item['reasons'][0] for item in value] for key, value in outcome['groups'].items()},
                         {'erase': ['superseded'], 'forget': ['duplicate', 'archived'], 'archive': ['stale_task']})
        self.assertIn('maven_save', outcome['next'])
        self.assertEqual(outcome['local_sweep']['files_touched'], [str(self.root / self.runtime / 'last-memory-write.json')])
        self.assertEqual(self.read_runtime('last-memory-write.json')['result'],
                         {'record_id': old, 'revision': 2, 'request_key': 'key-1'})
        for name in ('forget_records', 'erase_records', 'cleanup_describe'):
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
        failed = {'route': 'write', 'body': self.write_request(target), 'session_id': self.session_id, 'digest': 'x'}
        self.write_runtime('failed-memory-write.json', {'request': failed, 'status': 'revision_conflict'})
        manifest = {'manifest': {'session_id': self.session_id, 'memory_refs': [{'record_id': target, 'revision': 2}],
                                 'summary': 'Checkpoint'}, 'status': 'checkpointed'}
        self.write_runtime('checkpoints/' + str(uuid4()) + '.json', manifest)
        result = self.controller.begin('cleanup', {'action': 'erase', 'record_ids': [target], 'reason': 'Client data'})
        review = result['pending_interaction']['review']
        self.assertEqual(review['caveats'], list(ERASE_CAVEATS))
        text = ' '.join(review['caveats'])
        self.assertIn('30 days', text)
        self.assertIn('Other machines keep any locally cached copy until they next run a MAVEN clean-up', text)
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
        # An earlier erasure with an unresolved journal blocks every clean-up, including the review.
        self.erased = [target]
        blocked = self.controller.begin('cleanup', {'action': 'review'})
        self.assertEqual(blocked['state'], 'blocked')
        self.sql['cleanup_candidates'].assert_not_called()
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
