"""Offline lifecycle/coordination regressions; no AWS or SQL emulation claims."""
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.coordination import CoordinationError, ensure_available, private_lock, project_binding_lock
from tools.memory import admin, store, validation
from tools.memory.boundary import save_private as save_pointer
from tools.uninstall import lifecycle
from tools.uninstall.common import UninstallError, file_digest, save_private


CONFIG = {'instance_id': '10000000-0000-4000-8000-000000000001',
          'recovery_epoch': '10000000-0000-4000-8000-000000000002',
          'journal_bucket': 'fixture-journal'}
EMPTY_WORK = {'records': [], 'receipts': []}


class RegistryBarrierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir(mode=0o700)
        self.root = Path(self.temporary.name) / 'project'
        self.root.mkdir(mode=0o700)
        self.other = Path(self.temporary.name) / 'other'
        self.other.mkdir(mode=0o700)

    def marker(self, *, whole_home=False):
        with private_lock(self.home, 'runtime/registry.lock'):
            save_pointer(self.home, 'runtime/uninstall.json', {
                'schema': 1, 'whole_home': whole_home, 'project_roots': [str(self.root.resolve())],
                'actions': {'a' * 64: {'root': None if whole_home else str(self.root.resolve())}}})

    def test_selected_root_blocks_access_even_after_restart_but_not_other_roots(self):
        self.marker()
        with project_binding_lock(self.home, self.root), self.assertRaises(CoordinationError):
            ensure_available(self.home, self.root)
        with project_binding_lock(self.home, self.other):
            ensure_available(self.home, self.other)
        self.assertFalse((self.home / 'runtime/sessions.json').exists())

    def test_removal_waits_for_real_access_then_blocks_all_roots(self):
        with project_binding_lock(self.home, self.root):
            with self.assertRaises(CoordinationError), private_lock(self.home, 'runtime/registry.lock', blocking=False):
                self.fail('Removal crossed a held project-access gate')
        self.marker(whole_home=True)
        with self.assertRaises(CoordinationError):
            ensure_available(self.home, self.other)

    def test_malformed_marker_fails_closed_without_blocking_inspection(self):
        save_pointer(self.home, 'runtime/uninstall.json', {'schema': 1, 'whole_home': False,
                                                         'project_roots': ['relative']})
        with self.assertRaises(CoordinationError):
            ensure_available(self.home, self.root)


class LifecycleIdentityTests(unittest.TestCase):
    def test_retirement_control_requires_maintenance_and_exact_token(self):
        ordinary = store.control_document(CONFIG, 'ready')
        self.assertEqual(store.parse_control(json.dumps(ordinary), CONFIG), ordinary)
        frozen = {**ordinary, 'state': 'maintenance', 'uninstall': 'a' * 64}
        self.assertEqual(store.parse_control(json.dumps(frozen), CONFIG, allow_maintenance=True), frozen)
        for bad in ({**frozen, 'state': 'ready'}, {**frozen, 'uninstall': 'A' * 64},
                    {**frozen, 'uninstall': None}):
            with self.assertRaises(validation.MemoryError):
                store.parse_control(json.dumps(bad), CONFIG, allow_maintenance=True)
        with self.assertRaises(validation.MemoryError):
            store.parse_control(json.dumps(frozen), CONFIG)

    def test_project_migration_is_explicit_and_existing_barrier_schema_is_supported(self):
        prefix = {name: checksum for name, checksum, _ in admin.migrations()[:8]}
        db = Mock()
        db.scalar.return_value = json.dumps(prefix)
        self.assertEqual(lifecycle._schema(db, 'transaction'), prefix)
        with self.assertRaises(UninstallError):
            lifecycle._schema(db, 'transaction', project=True)
        db.execute.assert_not_called()
        prefix['008_service_state_lock.sql'] = '0' * 64
        db.scalar.return_value = json.dumps(prefix)
        with self.assertRaises(UninstallError):
            lifecycle._schema(db, 'transaction')

    def test_retirement_marker_refuses_bootstrap_and_migrate_without_writes(self):
        config = {**CONFIG, 'region': 'us-east-1', 'cluster_arn': 'fixture',
                  'app_secret_arn': 'app', 'admin_secret_arn': 'admin', 'database': 'maven',
                  'admin_role_arn': 'arn:aws:iam::123456789012:role/admin'}
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {
            'Arn': 'arn:aws:sts::123456789012:assumed-role/admin/fixture'}
        frozen = {**store.control_document(config, 'maintenance'), 'uninstall': 'a' * 64}
        with patch.object(admin, '_config'), patch.object(admin, '_check_logging'), \
                patch.object(admin, 'sdk_config'), patch.object(admin, '_control', return_value=(frozen, 'v1')), \
                patch.object(admin, '_install') as install:
            for operation in (admin.bootstrap, admin.migrate):
                with self.assertRaises(validation.MemoryError) as error:
                    operation(config, session)
                self.assertEqual(error.exception.status, 'bootstrap_refused')
            install.assert_not_called()
            session.client.return_value.execute_statement.assert_not_called()

    def test_detached_binding_is_digest_checked_and_unambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = {'schema': 1, 'root': str(root), 'project_id': str(uuid4()),
                       'git_enabled': False, 'checks': []}
            original = root / '.maven/project.json'
            save_private(original, binding)
            retained = original.with_name('detached-project-' + file_digest(original) + '.json')
            original.rename(retained)
            self.assertEqual(lifecycle._binding(root)['project_id'], binding['project_id'])
            save_private(retained, {**binding, 'project_id': str(uuid4())})
            with self.assertRaises(UninstallError):
                lifecycle._binding(root)

    def test_archived_project_denial_is_bounded_and_deterministic(self):
        error = Exception('private provider diagnostic')
        error.response = {'Error': {'Code': 'DatabaseErrorException',
                                   'Message': 'ERROR: project_archived; SQLState: 55000'}}
        result = store.safe_provider_error(error)
        self.assertEqual((result.status, result.http_status), ('project_archived', 409))


class RecordingDatabase:
    def __init__(self):
        self.client = Mock()
        self.transactions = 0
        self.mutations = []

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield 'transaction'

    def execute(self, sql, values=None, transaction=None):
        if values is not None and 'archived' in values:
            self.mutations.append(deepcopy(values))


class ProjectTransitionTests(unittest.TestCase):
    def test_replay_accepts_only_exact_generation_and_conflicts_never_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = {'schema': 1, 'root': str(root), 'project_id': str(uuid4()),
                       'git_enabled': False, 'checks': []}
            save_private(root / '.maven/project.json', binding)
            bound = lifecycle._binding(root)
            for kind, archived in (('project-archive', True), ('project-reactivate', False)):
                previous = {'archived': not archived, 'generation': 2, 'action_key': 'd' * 64}
                selected = {'project_id': binding['project_id'], 'revision': 1,
                            'content_hash': 'b' * 64, 'item_hash': 'c' * 64, 'lifecycle': previous}
                details = {'root': str(root), 'project_id': binding['project_id'], 'config': CONFIG,
                           'binding_sha256': lifecycle.digest(bound), 'project': selected,
                           'migrations': {lifecycle.PROJECT_MIGRATION: 'e' * 64},
                           'work': EMPTY_WORK, 'admin_identity': {'arn': 'selected-admin'}}
                action = {'kind': kind, 'details': details}
                applied = {'archived': archived, 'generation': 3,
                           'action_key': lifecycle.digest({'kind': kind, 'details': details})}
                cases = [
                    ({**selected, 'lifecycle': applied}, True),
                    ({**selected, 'lifecycle': {**applied, 'generation': 4}}, False),
                    ({**selected, 'lifecycle': {**applied, 'archived': not archived}}, False),
                    ({**selected, 'content_hash': 'f' * 64}, False),
                    ({**selected, 'content_hash': 'f' * 64, 'lifecycle': applied}, False),
                    ({**selected, 'lifecycle': {**applied, 'action_key': '0' * 64}}, False),
                ]
                for observed, accepted in cases:
                    with self.subTest(kind=kind, observed=observed):
                        before = deepcopy(observed)
                        db, progress, saves = RecordingDatabase(), {}, []
                        s3 = Mock()
                        s3.get_object.return_value = {
                            'Body': BytesIO(json.dumps(store.control_document(CONFIG, 'ready')).encode()),
                            'ETag': 'ready'}
                        save = lambda: saves.append(deepcopy(progress))
                        with patch.object(lifecycle, '_state', return_value={'state': 'ready'}), \
                                patch.object(lifecycle, '_schema'), \
                                patch.object(lifecycle, '_work', return_value=EMPTY_WORK), \
                                patch.object(lifecycle, '_project', return_value=observed):
                            if accepted:
                                result = lifecycle._project_execute(action, progress, save, db, s3)
                                self.assertEqual(result['status'], 'archived' if archived else 'reactivated')
                            else:
                                with self.assertRaises(UninstallError):
                                    lifecycle._project_execute(action, progress, save, db, s3)
                                self.assertNotIn('result', progress)
                        self.assertEqual(db.mutations, [])
                        self.assertEqual(observed, before)


class ControlObject:
    """A conditional byte store for orchestration tests, not a provider simulator."""
    def __init__(self, document, saves):
        self.document = deepcopy(document)
        self.etag = 'initial'
        self.saves = saves
        self.writes = 0
        self.lose_response = False
        self.closed = 0

    def close(self):
        self.closed += 1

    def get_object(self, **kwargs):
        return {'Body': BytesIO(json.dumps(self.document).encode()), 'ETag': self.etag}

    def put_object(self, **kwargs):
        if not self.saves or 'intent' not in self.saves[-1]:
            raise AssertionError('effect occurred before durable intent')
        if kwargs['IfMatch'] != self.etag:
            raise AssertionError('stale conditional control write')
        self.document = json.loads(kwargs['Body'])
        self.etag = 'frozen'
        self.writes += 1
        if self.lose_response:
            self.lose_response = False
            raise OSError('response lost after committed control write')


class CloudFreezeTests(unittest.TestCase):
    def run_freeze(self, planned, observed=None, *, lose_response=False):
        progress, saves = {}, []
        control = store.control_document(CONFIG, 'ready')
        details = {'home': '/selected-home', 'config': CONFIG, 'instance_id': CONFIG['instance_id'],
                   'recovery_epoch': CONFIG['recovery_epoch'], 'control': control,
                   'control_etag': 'initial', 'barrier_token': 'a' * 64,
                   'migrations': {}, 'work': deepcopy(planned)}
        action = {'handler': 'lifecycle', 'kind': 'cloud-freeze', 'details': details}
        s3 = ControlObject(control, saves)
        s3.lose_response = lose_response
        db = RecordingDatabase()
        save = lambda: saves.append(deepcopy(progress))
        patches = (patch.object(lifecycle, '_connection', return_value=(db, s3, details)),
                   patch.object(lifecycle, '_state', return_value={'state': 'maintenance'}),
                   patch.object(lifecycle, '_schema'),
                   patch.object(lifecycle, '_work', return_value=deepcopy(planned if observed is None else observed)))
        return action, progress, saves, s3, db, save, patches

    def test_lost_control_response_reconciles_without_rewriting_or_losing_evidence(self):
        work = EMPTY_WORK
        action, progress, saves, s3, db, save, patches = self.run_freeze(work, lose_response=True)
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(UninstallError):
                lifecycle.execute(action, progress, save)
            self.assertEqual(db.transactions, 0)
            result = lifecycle.execute(action, progress, save)
        self.assertEqual(s3.writes, 1)
        self.assertEqual(action['details']['work'], work)
        self.assertFalse(result['session_records_modified'])
        self.assertFalse(result['unsaved_work_recovered'])
        self.assertTrue(result['unknown_receipts_preserved'])
        self.assertNotIn('outcome_unresolved', progress)
        self.assertEqual(s3.closed, 2)

    def test_changed_work_keeps_barrier_and_refuses_purge(self):
        observed = {'records': [{'id': str(uuid4()), 'state': 'active'}], 'receipts': []}
        action, progress, saves, s3, db, save, patches = self.run_freeze(EMPTY_WORK, observed)
        with patches[0], patches[1], patches[2], patches[3], self.assertRaises(UninstallError):
            lifecycle.execute(action, progress, save)
        self.assertEqual(s3.document['state'], 'maintenance')
        self.assertTrue(progress['database_barrier'])
        self.assertTrue(progress['inventory_changed'])
        self.assertNotIn('result', progress)


if __name__ == '__main__':
    unittest.main()
