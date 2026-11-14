"""Offline real admin orchestration with explicit cloud projections, not SQL/AWS verification."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.memory import admin, store
from tools.uninstall import lifecycle
from tools.uninstall.common import save_private


class ProjectCloudProjection:
    """Supply query results and a transactional lifecycle row at the Data API boundary."""
    def __init__(self, config, project_id):
        self.client = Mock()
        self.config = config
        self.project_id = project_id
        self.project = {'project_id': project_id, 'name': 'Cloud-only history', 'revision': 1,
                        'content_hash': 'b' * 64, 'item_hash': 'c' * 64}
        self.row = None
        self.work = {'records': [], 'receipts': []}
        self.installed = {name: checksum for name, checksum, _ in admin.migrations()}
        self.state = {**store.control_document(config, 'ready'),
                      'app_secret_arn': config['app_secret_arn'], 'app_secret_version': 'fixture-version'}
        self.mutations = []
        self.lose_commit_response = False
        self.fail_write = False
        self.accessible = True

    @contextmanager
    def transaction(self):
        before, count = deepcopy(self.row), len(self.mutations)
        try:
            yield 'transaction'
        except BaseException:
            self.row = before
            del self.mutations[count:]
            raise
        if self.lose_commit_response and len(self.mutations) > count:
            self.lose_commit_response = False
            raise OSError('private provider response lost after commit')

    def scalar(self, sql, values=None, transaction=None):
        if 'FROM maven.schema_migrations' in sql:
            return json.dumps(self.installed)
        if 'FROM maven.service_state' in sql:
            return json.dumps(self.state)
        if 'FROM maven.project_lifecycle' in sql:
            return json.dumps(self.row) if self.row is not None else None
        if 'FROM maven.records' in sql:
            if not self.accessible or values['project'] != self.project_id:
                return None
            return json.dumps(self.project)
        raise AssertionError('Unexpected projection query')

    def json_scalar(self, expression, values, transaction):
        if values != {'project': self.project_id}:
            raise AssertionError('Unexpected project work scope')
        return json.dumps(self.work)

    def execute(self, sql, values=None, transaction=None):
        if values is not None and 'archived' in values:
            if self.fail_write:
                raise OSError('private provider diagnostic')
            if values['project'] != self.project_id:
                raise AssertionError('Unexpected mutation target')
            self.row = {'archived': values['archived'],
                        'generation': (self.row or {}).get('generation', 0) + 1,
                        'action_key': values['key']}
            self.mutations.append(deepcopy(values))


class ProjectLifecycleAdministrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.environment = self.home / '.env'
        self.environment.write_text('MAVEN_MEMORY_REGION=us-east-1\n')
        self.environment.chmod(0o600)
        self.other_root = self.base / 'unrelated-project'
        save_private(self.other_root / '.maven/project.json', {
            'schema': 1, 'root': str(self.other_root), 'project_id': str(uuid4()),
            'git_enabled': False, 'checks': []})
        self.original_files = self.local_files()
        self.project_id = str(uuid4())
        self.config = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4()),
                       'region': 'us-east-1', 'journal_bucket': 'fixture-journal',
                       'app_secret_arn': 'fixture-app-secret',
                       'admin_role_arn': 'arn:aws:iam::123456789012:role/admin'}
        self.cloud = ProjectCloudProjection(self.config, self.project_id)
        self.identity = {'Arn': 'arn:aws:sts::123456789012:assumed-role/admin/fixture',
                         'Account': '123456789012', 'UserId': 'fixture-role:fixture-session'}
        session = Mock()
        session.client.return_value.get_caller_identity.side_effect = lambda: deepcopy(self.identity)
        for target, kwargs in (
                ('tools.memory.environment.memory_config', {'side_effect': lambda values: deepcopy(self.config)}),
                ('tools.memory.client.selected_session', {'return_value': session}),
                ('tools.memory.store.sdk_config', {'return_value': None}),
                ('tools.memory.store.DataAPI', {'return_value': self.cloud}),
                ('tools.memory.admin._check_logging', {}),
                ('tools.memory.store.fetch_control', {'side_effect': lambda *args, **kwargs:
                    (store.control_document(self.config, 'ready'), 'fixture-etag')}),
                ('tools.uninstall.lifecycle._binding', {'side_effect': AssertionError('No local binding may be read')})):
            patched = patch(target, **kwargs)
            patched.start()
            self.addCleanup(patched.stop)
        self.controller = AdminOperations(self.home)
        self.source = {'origin': 'owner fixture message',
                       'excerpt': 'Approve this exact project transition; all affected writers are quiescent.'}

    def local_files(self):
        return {str(path.relative_to(self.base)): (path.read_bytes(), path.stat().st_mode)
                for path in self.base.rglob('*') if path.is_file()
                and 'runtime/administration/' not in str(path.relative_to(self.base))}

    def begin(self, action='archive'):
        return self.controller.begin('project_lifecycle', {'action': action, 'project_id': self.project_id})

    def approve(self, result):
        question = result['pending_interaction']
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'], {
            'approved': True, 'review_digest': question['review_digest'], 'source': self.source,
            'confirmations': question['confirmations']})

    def apply(self, result):
        return AdminOperations(self.home).advance(result['operation_id'])

    def test_rootless_archive_and_reactivate_review_every_identity_without_local_changes(self):
        for action, current, outcome in (('archive', 'active', 'archived'),
                                         ('reactivate', 'archived', 'reactivated')):
            with self.subTest(action=action):
                result = self.begin(action)
                question = result['pending_interaction']
                self.assertEqual(result['state'], 'awaiting_decision', result.get('error'))
                review = question['review']
                self.assertEqual((review['account'], review['region'], review['instance_id']),
                                 ('123456789012', 'us-east-1', self.config['instance_id']))
                self.assertEqual((review['project_id'], review['project_name'], review['current_state']),
                                 (self.project_id, 'Cloud-only history', current))
                self.assertEqual(review['identity']['role_id'], 'fixture-role')
                self.assertTrue(review['history_retained'])
                self.assertTrue(review['affects_all_machines'])
                self.assertFalse(review['local_binding_changed'])
                self.assertFalse(review['local_detach_performed'])
                self.assertIn('across all machines', question['description'])
                self.assertEqual(question['confirmations'], {'writers_quiescent': self.project_id})
                self.assertNotIn('root', review['lifecycle_action']['details'])
                self.assertNotIn('binding_sha256', review['lifecycle_action']['details'])
                before = len(self.cloud.mutations)
                self.controller.advance(result['operation_id'])
                self.assertEqual(len(self.cloud.mutations), before)
                pending = self.approve(result)
                self.assertEqual(len(self.cloud.mutations), before)
                result = self.apply(pending)
                self.assertEqual(result['state'], 'completed', result.get('error'))
                self.assertEqual(result['outcome']['status'], outcome)
                self.assertFalse(result['outcome']['local_binding_changed'])
                self.assertFalse(result['outcome']['local_detach_performed'])
                self.assertTrue(result['outcome']['history_preserved'])
                self.assertEqual(len(self.cloud.mutations), before + 1)
                self.assertEqual(self.apply(result)['state'], 'completed')
                self.assertEqual(len(self.cloud.mutations), before + 1)
                self.assertEqual(self.local_files(), self.original_files)

    def test_exact_approval_digest_source_interaction_and_quiescence_are_required(self):
        result = self.begin()
        question = result['pending_interaction']
        valid = {'approved': True, 'review_digest': question['review_digest'],
                 'source': self.source, 'confirmations': question['confirmations']}
        cases = [dict(valid, review_digest='wrong'), dict(valid, source=None),
                 dict(valid, confirmations={}), dict(valid, approved='yes')]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(AdminInputError):
                self.controller.respond(result['operation_id'], question['interaction_id'],
                                        question['revision'], response)
        with self.assertRaises(AdminInputError):
            self.controller.respond(result['operation_id'], question['interaction_id'],
                                    question['revision'] + 1, valid)
        paused = self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
                                         {'approved': False, 'review_digest': question['review_digest']})
        self.assertEqual(paused['state'], 'paused')
        self.assertEqual(self.apply(paused)['state'], 'paused')
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.local_files(), self.original_files)

    def test_parameters_are_strict_and_rejected_before_journaling(self):
        for parameters in ({}, {'action': 'archive'}, {'project_id': self.project_id},
                           {'action': 'remove', 'project_id': self.project_id},
                           {'action': 'archive', 'project_id': self.project_id, 'root': str(self.other_root)},
                           {'action': 'archive', 'project_id': self.project_id.replace('-', '')},
                           {'action': 'archive', 'project_id': 42}):
            with self.subTest(parameters=parameters), self.assertRaises(AdminInputError):
                self.controller.begin('project_lifecycle', parameters)
        self.assertFalse(self.controller.directory.exists())
        self.assertEqual(self.cloud.mutations, [])

    def test_archive_requires_migration017_without_implicitly_migrating(self):
        self.cloud.installed.pop(lifecycle.ARCHIVE_VISIBILITY_MIGRATION)
        result = self.begin()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn(lifecycle.ARCHIVE_VISIBILITY_MIGRATION, result['error'])
        self.assertIsNone(result['pending_interaction'])
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.local_files(), self.original_files)

    def test_inaccessible_or_non_project_target_never_reaches_review(self):
        self.cloud.accessible = False
        result = self.begin()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('not an accessible owner project', result['error'])
        self.assertIsNone(result['pending_interaction'])
        self.assertEqual(self.cloud.mutations, [])

    def test_active_sessions_claims_and_uncertain_work_block_review_without_closure(self):
        for record in ({'kind': 'session', 'state': 'active'},
                       {'kind': 'operation', 'state': 'blocked'},
                       {'kind': 'checkpoint', 'state': 'prepared'},
                       {'kind': 'task', 'claim_session': str(uuid4()), 'claim_fence': 3}):
            with self.subTest(record=record):
                self.cloud.work = {'records': [record], 'receipts': []}
                before = deepcopy(self.cloud.work)
                result = self.begin()
                self.assertEqual(result['state'], 'blocked')
                self.assertIn('active sessions or unresolved work', result['error'])
                self.assertIsNone(result['pending_interaction'])
                self.assertEqual(self.cloud.work, before)
                self.assertEqual(self.cloud.mutations, [])

    def test_new_work_after_approval_blocks_without_mutating_work(self):
        result = self.approve(self.begin())
        self.cloud.work = {'records': [{'kind': 'operation', 'state': 'started'}],
                           'receipts': [{'request_key': 'original-request'}]}
        original = deepcopy(self.cloud.work)
        result = self.apply(result)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('acquired unfinished work', result['error'])
        self.assertEqual(self.cloud.work, original)
        self.assertEqual(self.cloud.mutations, [])

    def test_stale_project_name_revision_or_lifecycle_requires_new_review(self):
        for change in ('name', 'revision', 'lifecycle'):
            with self.subTest(change=change):
                original_project, original_row = deepcopy(self.cloud.project), deepcopy(self.cloud.row)
                result = self.approve(self.begin())
                if change == 'name':
                    self.cloud.project['name'] = 'Different project name'
                elif change == 'revision':
                    self.cloud.project['revision'] += 1
                else:
                    self.cloud.row = {'archived': True, 'generation': 1, 'action_key': 'e' * 64}
                result = self.apply(result)
                self.assertEqual(result['state'], 'blocked')
                self.assertIn('project changed since planning', result['error'])
                self.assertEqual(self.cloud.mutations, [])
                self.cloud.project, self.cloud.row = original_project, original_row

    def test_changed_connection_administrator_or_migration_inventory_cannot_apply_approval(self):
        for change in ('environment', 'instance', 'region', 'administrator', 'migrations'):
            with self.subTest(change=change):
                config, identity = deepcopy(self.config), deepcopy(self.identity)
                installed = deepcopy(self.cloud.installed)
                result = self.approve(self.begin())
                if change == 'environment':
                    self.environment.write_text('MAVEN_MEMORY_REGION=us-west-2\n')
                elif change == 'instance':
                    self.config['instance_id'] = str(uuid4())
                elif change == 'region':
                    self.config['region'] = 'us-west-2'
                elif change == 'administrator':
                    self.identity['UserId'] = 'different-role:fixture-session'
                else:
                    self.cloud.installed.pop(lifecycle.ARCHIVE_VISIBILITY_MIGRATION)
                result = self.apply(result)
                self.assertEqual(result['state'], 'blocked')
                self.assertEqual(self.cloud.mutations, [])
                self.config.clear()
                self.config.update(config)
                self.identity = identity
                self.cloud.installed = installed
                self.environment.write_text('MAVEN_MEMORY_REGION=us-east-1\n')

    def test_changed_persisted_candidate_invalidates_approval_before_submission(self):
        result = self.approve(self.begin())
        saved = self.controller._load(result['operation_id'])
        saved['lifecycle_review']['lifecycle_action']['target'] = str(uuid4())
        self.controller._save(saved)
        result = self.apply(result)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('Reviewed candidate changed', result['error'])
        self.assertEqual(self.cloud.mutations, [])

    def test_commit_response_loss_resumes_original_effect_and_operation_without_duplicate_write(self):
        result = self.approve(self.begin())
        original = deepcopy(result['lifecycle_review'])
        self.cloud.lose_commit_response = True
        result = self.apply(result)
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn('private provider', result['error'])
        self.assertEqual(len(self.cloud.mutations), 1)
        self.assertTrue(self.cloud.row['archived'])
        token = result['lifecycle_progress']['intent']
        self.assertEqual(token, self.cloud.row['action_key'])
        self.controller = AdminOperations(self.home)
        status = self.controller.status(result['operation_id'])
        self.assertEqual(status['lifecycle_review'], original)
        self.assertEqual(len(self.cloud.mutations), 1)
        result = self.apply(status)
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertTrue(result['outcome']['replayed'])
        self.assertEqual(result['outcome']['operation_id'], original['operation_id'])
        self.assertEqual(result['lifecycle_progress']['intent'], token)
        self.assertNotIn('outcome_unresolved', result['lifecycle_progress'])
        self.assertEqual(len(self.cloud.mutations), 1)
        self.assertEqual(self.local_files(), self.original_files)

    def test_failed_submission_resumes_same_token_without_claiming_success(self):
        result = self.approve(self.begin())
        self.cloud.fail_write = True
        result = self.apply(result)
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn('outcome', result)
        self.assertNotIn('private provider', result['error'])
        self.assertEqual(self.cloud.mutations, [])
        token = result['lifecycle_progress']['intent']
        self.cloud.fail_write = False
        result = self.apply(result)
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(self.cloud.row['action_key'], token)
        self.assertFalse(result['outcome']['replayed'])
        self.assertEqual(len(self.cloud.mutations), 1)

    def test_replay_rejects_wrong_generation_or_changed_project_identity(self):
        for change in ('generation', 'name', 'archived', 'action_key'):
            with self.subTest(change=change):
                self.cloud.row = None
                self.cloud.project['name'] = 'Cloud-only history'
                self.cloud.mutations.clear()
                result = self.approve(self.begin())
                self.cloud.lose_commit_response = True
                result = self.apply(result)
                if change == 'generation':
                    self.cloud.row['generation'] += 1
                elif change == 'name':
                    self.cloud.project['name'] = 'Changed after commit'
                elif change == 'archived':
                    self.cloud.row['archived'] = False
                else:
                    self.cloud.row['action_key'] = '0' * 64
                result = self.apply(result)
                self.assertNotEqual(result['state'], 'completed')
                self.assertNotIn('outcome', result)
                self.assertEqual(len(self.cloud.mutations), 1)

    def test_new_operation_has_distinct_effect_identity_even_for_same_target_and_state(self):
        first = self.begin()
        second = self.begin()
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        tokens = []
        for result in (first, second):
            action = result['lifecycle_review']['lifecycle_action']
            tokens.append(lifecycle.digest({'kind': action['kind'], 'details': action['details']}))
        self.assertNotEqual(*tokens)
        self.assertEqual(self.cloud.mutations, [])


if __name__ == '__main__':
    unittest.main()
