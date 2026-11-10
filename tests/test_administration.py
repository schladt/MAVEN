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


if __name__ == '__main__':
    unittest.main()
