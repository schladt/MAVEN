"""Offline rename orchestration; no AWS access or cloud infrastructure mutations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.memory.client import MemoryClientError
from tools.project_rename import apply_rename, prepare_rename
from tools.setup_common import SetupPending, save_json


class ProjectRenameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.root = self.base / 'project'
        self.root.mkdir()
        (self.root / '.maven').mkdir(mode=0o700)
        self.project_id = str(uuid4())
        self.instance_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.local = {'schema': 2, 'instance_id': self.instance_id, 'recovery_epoch': str(uuid4()),
                      'project_id': self.project_id, 'root': str(self.root),
                      'git_enabled': False, 'remote': None, 'accepted_url': None,
                      'branch': None, 'checks': []}
        save_json(self.root / '.maven/project.json', self.local)
        save_json(self.home / 'home.json', {'root': str(self.root), 'project_id': self.project_id,
                                          'label': 'Personal Home'})
        self.original_files = {path: path.read_bytes() for path in
                               (self.root / '.maven/project.json', self.home / 'home.json')}
        self.binding = {'connection': {'instance_id': self.instance_id, 'recovery_epoch': self.local['recovery_epoch'],
                                       'region': 'us-east-1', 'endpoint': 'https://example.invalid'},
                        'environment': {'MAVEN_ACCOUNT_ID': '123456789012', 'MAVEN_AWS_REGION': 'us-east-1'}}
        self.record = {'record_id': self.project_id, 'revision': 4, 'kind': 'entity',
                       'classification': 'Sensitive', 'data_scope': 'owner', 'project_id': None,
                       'data': {'entity_type': 'project', 'name': 'MAVEN', 'aliases': ['Original name']},
                       'tags': ['maven-home', 'important']}
        self.original = deepcopy(self.record)
        self.receipts = {}
        self.writes = []
        self.calls = []
        self.lose_response = False
        self.fail_before_commit = False
        self.hide_record = False
        self.client = Mock()
        self.client.invoke.side_effect = self.invoke
        self.enter_patch('tools.project_rename.verify_connection')
        self.enter_patch('tools.project_rename.configured_client', return_value=self.client)
        self.source = {'origin': 'owner-conversation', 'excerpt': 'Rename MAVEN to maven-code, preserving its UUID and history.'}

    def enter_patch(self, target, **kwargs):
        context = patch(target, **kwargs)
        self.addCleanup(context.stop)
        return context.start()

    def invoke(self, route, body):
        self.calls.append((route, deepcopy(body)))
        if route == '/v1/memory/read':
            if self.hide_record:
                raise MemoryClientError('record_unavailable')
            return {'status': 'ready', 'data': deepcopy(self.record)}
        if route == '/v1/operations/lookup':
            if body['request_key'] not in self.receipts:
                raise MemoryClientError('operation_unavailable')
            return {'status': 'ready', 'data': deepcopy(self.receipts[body['request_key']])}
        if route == '/v1/memory/write':
            self.writes.append(deepcopy(body))
            if self.fail_before_commit:
                self.fail_before_commit = False
                raise MemoryClientError('outcome_unknown')
            if body['expected_revision'] != self.record['revision']:
                raise MemoryClientError('revision_conflict')
            self.record = {key: deepcopy(body[key]) for key in
                           ('record_id', 'kind', 'classification', 'data_scope', 'data', 'tags')}
            self.record.update(revision=body['expected_revision'] + 1, project_id=None)
            receipt = {key: body[key] for key in ('record_id', 'request_key')}
            receipt['revision'] = self.record['revision']
            self.receipts[body['request_key']] = receipt
            if self.lose_response:
                self.lose_response = False
                raise MemoryClientError('outcome_unknown')
            return {'status': 'ready', 'data': deepcopy(receipt)}
        raise AssertionError(route)

    def candidate(self):
        return prepare_rename(self.home, str(self.root), self.project_id, 'maven-code',
                              self.operation_id, self.binding)

    def apply(self, candidate, progress=None, persist=None):
        return apply_rename(self.home, candidate, self.binding, self.source,
                            progress if progress is not None else {}, persist or Mock())

    def test_preserves_uuid_aliases_tags_folder_home_and_binding(self):
        candidate = self.candidate()
        result = self.apply(candidate)
        self.assertEqual(result['status'], 'renamed')
        self.assertEqual(result['project_id'], self.project_id)
        self.assertEqual(self.record, {**self.original, 'revision': 5,
                                      'data': {**self.original['data'], 'name': 'maven-code'}})
        self.assertEqual(self.writes[0]['source'], self.source)
        self.assertNotIn('authority_generation', self.writes[0])
        self.assertEqual(self.writes[0]['expected_revision'], 4)
        for path, contents in self.original_files.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(candidate['old_name'], 'MAVEN')
        self.assertEqual(candidate['new_name'], 'maven-code')
        self.assertEqual(candidate['instance_id'], self.instance_id)
        self.assertEqual(candidate['account'], '123456789012')
        self.assertTrue(candidate['affects_all_machines'])

    def test_stale_project_revision_refused_before_submission(self):
        candidate = self.candidate()
        self.record['revision'] += 1
        with self.assertRaisesRegex(SetupPending, 'Project changed after review'):
            self.apply(candidate)
        self.assertEqual(self.writes, [])

    def test_changed_epoch_refused_before_submission(self):
        candidate = self.candidate()
        self.binding['connection']['recovery_epoch'] = str(uuid4())
        with self.assertRaisesRegex(SetupPending, 'binding changed'):
            self.apply(candidate)
        self.assertEqual(self.writes, [])

    def test_missing_project_is_not_created(self):
        self.hide_record = True
        with self.assertRaises(MemoryClientError):
            self.candidate()
        self.assertEqual(self.writes, [])

    def test_different_local_project_is_refused(self):
        with self.assertRaisesRegex(SetupPending, 'existing project UUID'):
            prepare_rename(self.home, str(self.root), str(uuid4()), 'maven-code',
                           self.operation_id, self.binding)
        self.assertEqual(self.calls, [])

    def test_uncertain_commit_reconciles_original_receipt_without_second_write(self):
        candidate, progress, saved = self.candidate(), {}, []
        self.lose_response = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress, lambda: saved.append(deepcopy(progress)))
        self.assertEqual(saved[0]['request'], self.writes[0])
        self.assertTrue(saved[0]['submitted'])
        # A restart keeps durable evidence, not the original Python objects.
        progress = deepcopy(saved[-1])
        self.calls.clear()
        result = self.apply(candidate, progress)
        self.assertEqual(result['status'], 'renamed')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.calls[0][0], '/v1/operations/lookup')
        self.assertTrue(progress['verified'])

    def test_uncertain_absent_receipt_retries_identical_request(self):
        candidate, progress = self.candidate(), {}
        self.fail_before_commit = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        result = self.apply(candidate, deepcopy(progress))
        self.assertEqual(result['status'], 'renamed')
        self.assertEqual(self.writes[0], self.writes[1])

    def test_mismatched_lookup_content_never_claims_completion(self):
        candidate, progress = self.candidate(), {}
        self.lose_response = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        self.record['data']['aliases'] = ['Not the reviewed aliases']
        with self.assertRaisesRegex(SetupPending, 'read-back differs'):
            self.apply(candidate, progress)
        self.assertNotIn('verified', progress)
        self.assertEqual(len(self.writes), 1)

    def test_resume_cannot_replace_original_provenance(self):
        candidate, progress = self.candidate(), {}
        self.fail_before_commit = True
        with self.assertRaises(MemoryClientError):
            self.apply(candidate, progress)
        self.source = {**self.source, 'excerpt': 'Different approval'}
        with self.assertRaisesRegex(SetupPending, 'Original rename request changed'):
            self.apply(candidate, progress)
        self.assertEqual(len(self.writes), 1)

    def test_admin_form_exact_review_stale_approval_and_restart_recovery(self):
        self.enter_patch('tools.environment_migrations.prerequisite', return_value=None)
        self.enter_patch('tools.administration.AdminOperations._project_binding', return_value=self.binding)
        self.enter_patch('tools.administration.AdminOperations._preflight')
        operations = AdminOperations(self.home)
        state = operations.begin('project', {'root': str(self.root)})
        identifier = state['operation_id']
        form = state['pending_interaction']
        mode = next(field for field in form['fields'] if field['name'] == 'mode')
        self.assertIn('rename', [choice[1] for choice in mode['choices']])
        operations.respond(identifier, form['interaction_id'], form['revision'], {
            'root': str(self.root), 'mode': 'rename', 'project_id': self.project_id, 'name': 'maven-code'})
        state = operations.advance(identifier)
        review = state['pending_interaction']
        self.assertEqual(review['review']['rename']['old_name'], 'MAVEN')
        self.assertIn('all machines', review['description'])
        self.assertIn(self.instance_id, review['description'])
        response = {'approved': True, 'review_digest': review['review_digest'], 'source': self.source}
        with self.assertRaises(AdminInputError):
            operations.respond(identifier, review['interaction_id'], review['revision'] - 1, response)
        with self.assertRaises(AdminInputError):
            operations.respond(identifier, review['interaction_id'], review['revision'],
                               {**response, 'review_digest': 'stale'})
        operations.respond(identifier, review['interaction_id'], review['revision'], response)
        self.lose_response = True
        state = operations.advance(identifier)
        self.assertEqual(state['state'], 'uncertain')
        operations = AdminOperations(self.home)
        state = operations.advance(identifier)
        self.assertEqual(state['state'], 'completed')
        self.assertEqual(state['outcome']['name'], 'maven-code')
        self.assertEqual(len(self.writes), 1)


if __name__ == '__main__':
    unittest.main()
