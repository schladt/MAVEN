"""Restartable administration behavior with temporary evidence and simulated AWS."""
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
from tools.setup import Wizard
from tools.setup_common import read_private, save_json
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

    def respond(self, result, values=None):
        question = result['pending_interaction']
        if question['kind'] == 'review':
            values = {'approved': True, 'review_digest': question['review_digest'],
                      'source': self.source, 'confirmations': question['confirmations']}
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

    def test_existing_setup_reaches_verified_readiness_without_provisioning(self):
        wizard = Wizard(self.home, None)
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
                       'upgrade': {'migrate': False}, 'optional_project': {'configure': False, 'root': ''}}
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
            prepare.assert_not_called()
            apply.assert_not_called()
            initialize.assert_not_called()

        original_path = self.controller._path(result['operation_id'])
        original_operation = original_path.read_bytes()
        wizard = Wizard(self.home, None)
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

    def test_setup_never_steals_unresolved_or_unverified_ownership(self):
        for phase in ('awaiting_decision', 'working', 'blocked', 'uncertain', 'cancelled', 'completed'):
            with self.subTest(phase=phase):
                home = self.base / phase
                controller = AdminOperations(home)
                original = controller.begin('setup', {})
                saved = controller._load(original['operation_id'])
                saved.update(state=phase, pending_interaction=None)
                controller._save(saved)
                wizard = Wizard(home, None)
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
                wizard = Wizard(home, None)
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
        wizard = Wizard(self.home, None)
        original_setup = wizard.state_path.read_bytes()
        with patch.object(Wizard, 'save', side_effect=OSError('interrupted owner write')):
            next_operation = self.controller.begin('setup', {})
        self.assertEqual(next_operation['state'], 'blocked')
        self.assertEqual(wizard.state_path.read_bytes(), original_setup)
        archive = self.controller.directory / ('setup-' + original['operation_id'] + '.json')
        self.assertEqual(archive.read_bytes(), original_setup)
        next_operation = AdminOperations(self.home).advance(next_operation['operation_id'])
        self.assertEqual(next_operation['state'], 'awaiting_decision')
        self.assertEqual(Wizard(self.home, None).state['administration_operation'], next_operation['operation_id'])
        self.assertEqual(archive.read_bytes(), original_setup)
        self.assertEqual(self.controller._path(original['operation_id']).read_bytes(), original_operation)

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
            Wizard(self.home, None).settings({'MAVEN_MEMORY_INSTANCE_ID': instance})
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
                           'MAVEN_NOTIFICATION_EMAIL': 'owner@example.com', 'MAVEN_AVAILABILITY_ZONES': 'us-east-1a,us-east-1b', 'MAVEN_ENGINE_VERSION': '18.6'},
                       'optional_project': {'configure': False, 'root': ''}}
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
        wizard = Wizard(self.home, None)
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
            {'excerpt': 'I approve this project in the terminal.'},
        ]
        ui.review.return_value = True
        with patch('tools.project_setup.setup_project', return_value={'status': 'ready'}) as backend:
            self.assertEqual(present(ui, self.home, operation_id=result['operation_id']), 0)
        actual = AdminOperations(self.home).status(result['operation_id'])
        self.assertEqual(actual['state'], 'completed')
        self.assertEqual(backend.call_args.args[0].source_origin, 'owner MAVEN administration TUI')
        self.assertEqual(actual['approvals'][0]['source']['excerpt'], 'I approve this project in the terminal.')

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
        wizard = Wizard(self.home, None)
        original = {'MAVEN_MEMORY_INSTANCE_ID': str(uuid4()), 'MAVEN_AWS_PROFILE': 'original-owner'}
        wizard.settings(original)
        binding = dict(wizard.values)
        for field, changed in (('MAVEN_MEMORY_INSTANCE_ID', str(uuid4())), ('MAVEN_AWS_PROFILE', 'other-owner')):
            for phase in ('starter_prepare_review', 'starter_prepare', 'starter_submit',
                          'starter_challenge', 'starter_activate', 'starter_renew'):
                with self.subTest(field=field, phase=phase):
                    wizard = Wizard(self.home, None)
                    wizard.settings({**original, field: changed})
                    state = self.controller._load(result['operation_id'])
                    state.update(step=phase, state='pending', pending_interaction=None, starter_binding=binding)
                    self.controller._save(state)
                    with patch('tools.memory.environment.configured_client') as client:
                        observed = AdminOperations(self.home).advance(result['operation_id'])
                        self.assertEqual(observed['state'], 'blocked')
                        self.assertIn('original initialization', observed['error'])
                        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
