"""Deterministic local-only retirement and imported-source regression cases."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.administration import AdminOperations
from tools.environment_migrations import RETIRED_KEY, ensure, load_import, preview
from tools.memory.environment import MemoryClientError, load_environment
from tools.setup_common import SetupPending, read_private


class EnvironmentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'home'
        self.controller = AdminOperations(self.home)
        self.controller._prepare()
        self.path = self.home / '.env'
        self.other = ('# Keep byte-for-byte including quoting and order\r\n'
                      'MAVEN_AWS_PROFILE="daily"\r\n'
                      'MAVEN_SECRETS_PARAMETER_PREFIX=/synthetic/values/\r\n'
                      'MAVEN_SECRETS_BUCKET=synthetic-bucket\r\n'
                      'MAVEN_SECRETS_FILE_KEY_PREFIX=/synthetic/keys/\r\n'
                      'MAVEN_SECRETS_OBJECT_PREFIX=objects/\r\n')
        self.legacy = (self.other + RETIRED_KEY + '="/synthetic/google/"\r\n').encode()

    def put(self, path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def approve(self, result, approved=True):
        question = result['pending_interaction']
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'],
            {'approved': approved, 'review_digest': question['review_digest'],
             'source': {'origin': 'offline synthetic owner', 'excerpt': 'Approve only this synthetic migration review.'}})

    def complete(self, result):
        answered = self.approve(result)
        return self.controller.advance(answered['operation_id'])

    def test_strict_parser_rejects_retired_key_and_preview_does_not_write(self):
        self.put(self.path, self.legacy)
        with self.assertRaises(MemoryClientError):
            load_environment(self.path)
        self.assertNotIn(RETIRED_KEY, preview(self.path))
        self.assertEqual(self.path.read_bytes(), self.legacy)

    def test_decline_keeps_every_byte_and_same_operation(self):
        self.put(self.path, self.legacy)
        pending = ensure(self.controller)
        declined = self.approve(pending, False)
        self.assertEqual(declined['state'], 'paused')
        self.assertEqual(self.path.read_bytes(), self.legacy)
        self.assertEqual(ensure(self.controller)['operation_id'], pending['operation_id'])
        self.assertEqual(declined['approvals'], [])

    def test_changed_source_before_apply_is_fenced(self):
        self.put(self.path, self.legacy)
        pending = ensure(self.controller)
        self.approve(pending)
        changed = self.legacy.replace(b'daily', b'other')
        self.put(self.path, changed)
        blocked = self.controller.advance(pending['operation_id'])
        self.assertEqual(blocked['state'], 'blocked')
        self.assertIn('changed after migration review', blocked['error'])
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(ensure(self.controller)['operation_id'], pending['operation_id'])

    def test_preserves_all_integrations_without_requiring_local_key(self):
        self.put(self.path, self.legacy)
        result = self.complete(ensure(self.controller))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.path.read_bytes(), self.other.encode())
        self.assertNotIn(RETIRED_KEY, load_environment(self.path))
        receipt_path = self.home / 'runtime/environment-migrations' / (result['operation_id'] + '.receipt.json')
        self.assertEqual(read_private(receipt_path)['preserved_locator'], '/synthetic/google/')
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(ensure(self.controller))
        self.assertEqual(self.controller.advance(result['operation_id'])['approvals'], result['approvals'])

    def test_fresh_home_needs_no_migration_or_receipt(self):
        self.assertIsNone(ensure(self.controller))
        self.put(self.path, self.other.encode())
        self.assertIsNone(ensure(self.controller))
        self.assertFalse((self.home / 'runtime/environment-migrations').exists())

    def test_import_is_reviewed_snapshot_never_source_rewrite(self):
        source = Path(self.temporary.name) / 'import.env'
        self.put(source, self.legacy)
        with self.assertRaises(SetupPending):
            load_import(self.home, source)
        result = self.complete(ensure(self.controller, source))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(source.read_bytes(), self.legacy)
        self.assertFalse(self.path.exists())
        self.assertEqual(load_import(self.home, source), preview(source))
        self.assertIsNone(ensure(self.controller, source))
        self.put(source, self.legacy.replace(b'daily', b'changed'))
        with self.assertRaises(SetupPending):
            load_import(self.home, source)
        replacement = ensure(self.controller, source)
        self.assertNotEqual(replacement['operation_id'], result['operation_id'])
        self.assertEqual(self.controller.status(result['operation_id'])['outcome'], result['outcome'])
        self.complete(replacement)
        self.assertEqual(load_import(self.home, source)['MAVEN_AWS_PROFILE'], 'changed')
        self.assertEqual(Path(result['outcome']['source_snapshot']).read_bytes(), self.legacy)
        self.assertEqual(source.read_bytes(), self.legacy.replace(b'daily', b'changed'))

    def test_repeated_import_recovers_new_original_record_before_pointer_update(self):
        from tools import environment_migrations as migrations
        source = Path(self.temporary.name) / 'import.env'
        self.put(source, self.legacy)
        old = self.complete(ensure(self.controller, source))
        self.put(source, self.legacy.replace(b'daily', b'next'))
        save = migrations.save_json
        def interrupt_pointer(path, value):
            if path == migrations._pointer(self.home, source):
                raise OSError('crash before pointer')
            return save(path, value)
        with patch.object(migrations, 'save_json', side_effect=interrupt_pointer):
            with self.assertRaises(OSError):
                ensure(self.controller, source)
        records = [read_private(path) for path in self.controller.directory.glob('*.json')]
        pending = [record for record in records if record['state'] != 'completed']
        self.assertEqual(len(pending), 1)
        resumed = ensure(self.controller, source)
        self.assertEqual(resumed['operation_id'], pending[0]['operation_id'])
        self.assertNotEqual(resumed['operation_id'], old['operation_id'])

    def test_changed_import_snapshot_is_fenced(self):
        source = Path(self.temporary.name) / 'import.env'
        self.put(source, self.legacy)
        result = self.complete(ensure(self.controller, source))
        self.put(Path(result['outcome']['destination']), self.other.replace('daily', 'other').encode())
        with self.assertRaises(SetupPending):
            load_import(self.home, source)

    def test_parent_interactions_and_approvals_keep_identity(self):
        with patch.object(AdminOperations, '_project') as dispatch:
            original = self.controller.begin('project', {'root': str(self.home)})
            state = self.controller._load(original['operation_id'])
            self.controller._review(state, 'synthetic', 'Original review', {'keep': 'exact'}, 'start')
            self.controller._save(state)
            before = self.controller.status(original['operation_id'])
            self.put(self.path, self.legacy)
            gated = self.controller.advance(original['operation_id'])
            self.assertEqual(gated['operation_id'], original['operation_id'])
            self.assertEqual(gated['pending_interaction'], before['pending_interaction'])
            self.assertEqual(gated['approvals'], before['approvals'])
            self.assertEqual(dispatch.call_count, 1)
            self.complete(gated['prerequisite'])
            resumed = self.controller.advance(original['operation_id'])
            self.assertEqual(resumed['pending_interaction'], before['pending_interaction'])
            self.assertEqual(resumed['approvals'], before['approvals'])

    def test_reconciles_crash_after_replacement_under_original_identity(self):
        self.put(self.path, self.legacy)
        pending = ensure(self.controller)
        self.approve(pending)
        state = self.controller._load(pending['operation_id'])
        from tools.environment_migrations import advance
        advance(self.controller, state)  # File and receipt durable; admin completion not saved.
        resumed = self.controller.advance(pending['operation_id'])
        self.assertEqual(resumed['state'], 'completed')
        self.assertEqual(resumed['operation_id'], pending['operation_id'])
        self.assertEqual(self.path.read_bytes(), self.other.encode())

    def test_recovers_original_record_before_pointer_was_saved(self):
        self.put(self.path, self.legacy)
        pending = self.controller.begin('environment_migration', {'path': str(self.path)})
        self.assertEqual(ensure(self.controller)['operation_id'], pending['operation_id'])

    def test_setup_gate_precedes_wizard_and_readiness_reuse(self):
        from tools.setup import _present
        ui = Mock()
        args = argparse.Namespace(migrate_environment=True)
        with patch('tools.environment_migrations.present', return_value=0) as migration, \
                patch('tools.setup.Wizard', side_effect=AssertionError('strict load too early')):
            self.assertEqual(_present(ui, args, self.home), 0)
        migration.assert_called_once_with(ui, self.home)

    def test_import_without_legacy_locator_needs_no_migration(self):
        source = Path(self.temporary.name) / 'import.env'
        self.put(source, self.other.encode())
        self.assertEqual(load_import(self.home, source), load_environment(source))
        self.assertIsNone(ensure(self.controller, source))

    def test_installer_migration_handoff_has_no_cloud_setup(self):
        from install import _handoff_command
        python = self.home / 'venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        command = _handoff_command({'home': self.home, 'migrate_environment': True})
        self.assertEqual(command[:3], [str(python), '-I', '-c'])
        self.assertEqual(command[-4:], ['tools.setup', '--home', str(self.home), '--migrate-environment'])

    def test_tui_presents_prerequisite_then_resumes_parent(self):
        from tools.tui.administration import present
        self.put(self.path, self.legacy)
        def finish(controller, state):
            state.update(state='completed', outcome={'synthetic': True})
        ui = Mock()
        ui.review.return_value = True
        with patch.object(AdminOperations, '_project', finish):
            result = present(ui, self.home, kind='project', parameters={'root': str(self.home)})
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_bytes(), self.other.encode())

    def test_import_migration_runs_before_parent_dispatch(self):
        source = Path(self.temporary.name) / 'import.env'
        self.put(source, self.legacy)
        with patch.object(AdminOperations, '_project') as dispatch:
            original = self.controller.begin('project', {'root': str(self.home)})
            state = self.controller._load(original['operation_id'])
            state['answers']['secrets_source'] = {'mode': 'import'}
            state['answers']['secrets_details'] = {'path': str(source)}
            self.controller._save(state)
            gated = self.controller.advance(original['operation_id'])
            self.assertEqual(dispatch.call_count, 1)
            self.complete(gated['prerequisite'])
            self.controller.advance(original['operation_id'])
            self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(source.read_bytes(), self.legacy)


if __name__ == '__main__':
    unittest.main()
