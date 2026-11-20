"""Offline cloud-only project creation and durable owner review coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.memory.client import MemoryClientError
from tools.project_create import apply_create, prepare_create
from tools.setup_common import SetupPending, save_json


class ProjectCreateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.root = self.base / 'project'
        (self.root / '.maven/runtime').mkdir(parents=True, mode=0o700)
        self.old_id, self.instance_id, self.operation_id = (str(uuid4()) for _ in range(3))
        save_json(self.root / '.maven/project.json', {'schema': 1, 'project_id': self.old_id,
                  'root': str(self.root), 'git_enabled': False, 'remote': None,
                  'accepted_url': None, 'branch': None, 'checks': []})
        save_json(self.root / '.maven/runtime/evidence.json', {'unfinished': True})
        save_json(self.home / 'home.json', {'root': str(self.root), 'project_id': self.old_id})
        self.original = {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        self.binding = {'connection': {'instance_id': self.instance_id, 'recovery_epoch': str(uuid4()),
                                       'region': 'us-east-1', 'endpoint': 'https://example.invalid'},
                        'environment': {'MAVEN_ACCOUNT_ID': '123456789012', 'MAVEN_AWS_REGION': 'us-east-1'}}
        self.records, self.receipts, self.writes, self.calls = {}, {}, [], []
        self.lose_response = self.fail_before_commit = False
        self.client = Mock()
        self.client.invoke.side_effect = self.invoke
        self.connection = self.enter_patch('tools.project_create.verify_connection')
        self.enter_patch('tools.project_create.configured_client', return_value=self.client)
        self.source = {'origin': 'owner-conversation', 'excerpt': 'Create maven-code in this instance; leave local evidence untouched.'}

    def enter_patch(self, target, **kwargs):
        context = patch(target, **kwargs)
        self.addCleanup(context.stop)
        return context.start()

    def invoke(self, route, body):
        self.calls.append((route, deepcopy(body)))
        if route == '/v1/operations/lookup':
            if body['request_key'] not in self.receipts:
                raise MemoryClientError('operation_unavailable')
            return {'data': deepcopy(self.receipts[body['request_key']])}
        if route == '/v1/memory/read':
            return {'data': deepcopy(self.records[body['record_id']])}
        if route == '/v1/memory/write':
            self.writes.append(deepcopy(body))
            if self.fail_before_commit:
                self.fail_before_commit = False
                raise MemoryClientError('outcome_unknown')
            if body['record_id'] in self.records or body['expected_revision'] != 0:
                raise MemoryClientError('revision_conflict')
            record = {key: deepcopy(body[key]) for key in
                      ('record_id', 'kind', 'classification', 'data_scope', 'data', 'tags')}
            record.update(revision=1, project_id=None)
            self.records[body['record_id']] = record
            receipt = {key: body[key] for key in ('record_id', 'request_key')}
            receipt['revision'] = 1
            self.receipts[body['request_key']] = receipt
            if self.lose_response:
                self.lose_response = False
                raise MemoryClientError('outcome_unknown')
            return {'data': deepcopy(receipt)}
        raise AssertionError(route)

    def candidate(self):
        return prepare_create(self.home, str(self.root), 'maven-code', self.operation_id, self.binding)

    def apply(self, candidate, progress=None, persist=None):
        return apply_create(self.home, candidate, self.binding, self.source,
                            progress if progress is not None else {}, persist or Mock())

    def assert_local_unchanged(self):
        for path, content in self.original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(set(self.root.rglob('*')), {
            self.root / '.maven', self.root / '.maven/project.json',
            self.root / '.maven/runtime', self.root / '.maven/runtime/evidence.json'})

    def test_existing_bound_root_creates_only_cloud_entity(self):
        candidate = self.candidate()
        self.assertEqual(candidate, self.candidate())
        result = self.apply(candidate)
        self.assertEqual(result['status'], 'created')
        self.assertEqual(result['name'], 'maven-code')
        self.assertEqual(result['instance_id'], self.instance_id)
        self.assertFalse(result['local_binding_changed'])
        self.assertNotEqual(result['project_id'], self.old_id)
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.writes[0]['expected_revision'], 0)
        self.assertEqual(self.writes[0]['source'], self.source)
        self.connection.assert_called_with(self.home / '.env', values=self.binding['environment'])
        self.assert_local_unchanged()

    def test_changed_instance_refuses_submission(self):
        candidate = self.candidate()
        self.binding['connection']['instance_id'] = str(uuid4())
        with self.assertRaisesRegex(SetupPending, 'binding changed'):
            self.apply(candidate)
        self.assertEqual(self.writes, [])

    def test_existing_different_record_is_not_overwritten(self):
        candidate = self.candidate()
        self.records[candidate['project_id']] = {'data': {'name': 'different'}}
        with self.assertRaises(MemoryClientError):
            self.apply(candidate)
        self.assertEqual(self.records[candidate['project_id']]['data']['name'], 'different')

    def test_lost_response_reconciles_original_receipt_after_restart(self):
        candidate, progress, saved = self.candidate(), {}, []
        self.lose_response = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress, lambda: saved.append(deepcopy(progress)))
        self.assertTrue(saved[0]['submitted'])
        self.assertEqual(saved[0]['request'], self.writes[0])
        progress = deepcopy(saved[-1])
        self.calls.clear()
        result = self.apply(deepcopy(candidate), progress)
        self.assertEqual(self.calls[0][0], '/v1/operations/lookup')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.records), 1)
        self.assertEqual(progress['receipt'], self.receipts[result['request_key']])
        self.assertTrue(progress['verified'])
        self.assert_local_unchanged()

    def test_absent_receipt_retries_identical_original_request(self):
        candidate, progress = self.candidate(), {}
        self.fail_before_commit = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        self.apply(candidate, deepcopy(progress))
        self.assertEqual(self.writes[0], self.writes[1])

    def test_changed_provenance_refuses_resume(self):
        candidate, progress = self.candidate(), {}
        self.fail_before_commit = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        self.source['excerpt'] = 'Different approval'
        with self.assertRaisesRegex(SetupPending, 'Original creation request changed'):
            self.apply(candidate, progress)
        self.assertEqual(len(self.writes), 1)

    def test_differing_read_back_refuses_completion(self):
        candidate, progress = self.candidate(), {}
        self.lose_response = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        self.records[candidate['project_id']]['data']['name'] = 'different'
        with self.assertRaisesRegex(SetupPending, 'read-back differs'):
            self.apply(candidate, progress)
        self.assertNotIn('verified', progress)

    def test_admin_exact_review_and_restart_consumer_outcome(self):
        self.enter_patch('tools.environment_migrations.prerequisite', return_value=None)
        self.enter_patch('tools.administration.AdminOperations._project_binding', return_value=self.binding)
        self.enter_patch('tools.administration.AdminOperations._preflight')
        operations = AdminOperations(self.home)
        state = operations.begin('project', {'root': str(self.root)})
        identifier, form = state['operation_id'], state['pending_interaction']
        answer = {'root': str(self.root), 'mode': 'create_cloud', 'project_id': '', 'name': 'maven-code'}
        with self.assertRaises(AdminInputError):
            operations.respond(identifier, form['interaction_id'], form['revision'],
                               {**answer, 'project_id': self.old_id})
        operations.respond(identifier, form['interaction_id'], form['revision'], answer)
        state = operations.advance(identifier)
        review = state['pending_interaction']
        candidate = review['review']['create_cloud']
        for value in (candidate['project_id'], candidate['request']['request_key'], self.instance_id,
                      str(self.root), '123456789012', 'us-east-1', 'separately reviewed switch'):
            self.assertIn(value, review['description'])
        response = {'approved': True, 'review_digest': review['review_digest'], 'source': self.source}
        with self.assertRaises(AdminInputError):
            operations.respond(identifier, review['interaction_id'], review['revision'] - 1, response)
        with self.assertRaises(AdminInputError):
            operations.respond(identifier, review['interaction_id'], review['revision'],
                               {**response, 'review_digest': 'stale'})
        operations.respond(identifier, review['interaction_id'], review['revision'], response)
        self.lose_response = True
        self.assertEqual(operations.advance(identifier)['state'], 'uncertain')
        state = AdminOperations(self.home).advance(identifier)
        self.assertEqual(state['state'], 'completed')
        self.assertEqual(state['outcome']['status'], 'created')
        self.assertEqual(state['outcome']['project_id'], candidate['project_id'])
        self.assertFalse(state['outcome']['local_binding_changed'])
        self.assertEqual(len(self.writes), 1)
        self.assert_local_unchanged()


if __name__ == '__main__':
    unittest.main()
