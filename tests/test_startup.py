"""Offline lifecycle behavior: exact conversation identity, recovery and shutdown evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_mcp import MCPCloud, fixture_application
from tools.home import save_home
from tools.memory.boundary import BoundaryError, read_private, save_private
from tools.memory.maintenance import MaintenanceError
from tools.startup import inspect_shutdown, inspect_startup, record_shutdown

class StartupCloud(MCPCloud):
    """Production search headers and read timestamps, not full-record search fixtures."""

    def invoke(self, route, body):
        result = super().invoke(route, body)
        if route == '/v1/memory/search':
            result['data']['records'] = [
                {**{key: record[key] for key in ('record_id', 'revision', 'kind', 'project_id', 'data_scope', 'tags')},
                 'title': record['data'].get('title'), 'state': record['data'].get('state'), 'snippet': None}
                for record in result['data']['records']]
        elif route == '/v1/memory/read':
            result['data']['recorded_at'] = result['data'].pop('updated_at', None)
        return result


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.home = self.base / 'project', self.base / 'instance'
        self.root.mkdir()
        self.project_id = str(uuid.uuid4())
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': self.project_id})
        self.cloud = StartupCloud()

    def application(self):
        app = fixture_application(self.cloud, home=self.home)
        app._unbound_client = self.cloud
        self.addCleanup(app.close)
        return app

    def startup(self, app, conversation='conversation-a', action='continue', **kwargs):
        return app.startup(str(self.root), harness='omp', harness_session=conversation, action=action, **kwargs)

    def inspect_exit(self, conversation='conversation-a'):
        return inspect_shutdown(self.home, self.root, harness='omp', harness_session=conversation)

    def test_unenabled_startup_is_sessionless_and_does_not_contact_cloud(self):
        scratch = self.base / 'scratch'
        scratch.mkdir()
        app = self.application()
        result = app.startup(str(scratch), harness='omp', harness_session='conversation-a', action='continue')
        self.assertEqual(result['status'], 'choose')
        self.assertEqual(self.cloud.calls, [])
        self.assertIsNone(app.binding)
        self.assertFalse((scratch / '.maven').exists())
        self.assertFalse(self.home.exists())

    def test_inspection_reads_project_only_without_creating_a_session(self):
        app = self.application()
        result = self.startup(app, action='inspect')
        self.assertEqual(result['status'], 'initialize')
        self.assertTrue(result['live_state_checked'])
        self.assertIsNone(app.binding)
        self.assertFalse((self.root / '.maven/runtime').exists())
        self.assertTrue(self.cloud.calls)
        self.assertTrue(all(route == '/v1/memory/search' and body['project_id'] == self.project_id
                            and body['scope'] == 'project' for route, body in self.cloud.calls))

    def test_home_startup_with_existing_cloud_session_reads_summary_then_current_record(self):
        self.home.mkdir(mode=0o700)
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID="instance-one"\n')
        (self.home / '.env').chmod(0o600)
        save_home(self.home, root=self.root, project_id=self.project_id, instance_id='instance-one',
                  label='Home', consent={'origin': 'Owner conversation', 'excerpt': 'Use this project as Home.'})
        identifier = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        self.cloud.records[identifier] = {
            'record_id': identifier, 'revision': 3, 'kind': 'session', 'data_scope': 'owner',
            'project_id': self.project_id, 'tags': [], 'updated_at': timestamp,
            'data': {'state': 'active', 'summary': json.dumps({'machine': 'other-host', 'harness': 'codex'})}}
        peer = deepcopy(self.cloud.records[identifier])
        app = self.application()
        result = app.startup(str(self.base), harness='omp', harness_session='home-conversation',
                             home=True, action='continue')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['active']['mode'], 'home')
        self.assertEqual(result['binding']['root'], str(self.root))
        self.assertNotEqual(result['binding']['session_id'], identifier)
        inspected = result['startup_review']['cloud_sessions']['sessions'][0]
        self.assertEqual((inspected['revision'], inspected['machine'], inspected['last_record_update']),
                         (3, 'other-host', timestamp))
        self.assertEqual(self.cloud.records[identifier], peer)

    def test_session_read_scope_mismatch_blocks_startup_without_creating_session(self):
        existing = self.startup(self.application())['binding']
        app = self.application()
        invoke = self.cloud.invoke

        def wrong_scope(route, body):
            result = invoke(route, body)
            if route == '/v1/memory/read' and body['record_id'] == existing['session_id']:
                result['data']['project_id'] = str(uuid.uuid4())
            return result

        before = deepcopy(self.cloud.records)
        with patch.object(self.cloud, 'invoke', side_effect=wrong_scope), self.assertRaises(BoundaryError):
            self.startup(app, conversation='conversation-b')
        self.assertIsNone(app.binding)
        self.assertEqual(self.cloud.records, before)

    def test_exact_resume_preserves_pause_and_other_conversations_get_new_sessions(self):
        first = self.application()
        original = self.startup(first)['binding']
        first.maintenance.pause(anchor='native conversation boundary')
        restarted = self.application()
        resumed = self.startup(restarted)
        self.assertEqual(resumed['binding'], original)
        self.assertFalse(resumed['automatic_updates'])
        fresh = self.startup(self.application(), 'conversation-b')
        self.assertNotEqual(fresh['binding']['session_id'], original['session_id'])
        self.assertTrue(fresh['automatic_updates'])
        sessions = [record for record in self.cloud.records.values() if record['kind'] == 'session']
        self.assertEqual(len(sessions), 2)

    def test_finished_session_starts_new_without_reopening_old_record(self):
        app = self.application()
        original = self.startup(app)['binding']
        checkpoint = app.maintenance.checkpoint(summary='Work complete.', refs=[])
        app.maintenance.finish(checkpoint['checkpoint']['checkpoint_id'])
        self.assertEqual(self.inspect_exit()['status'], 'closed')
        record_shutdown(self.home, self.root, harness='omp', harness_session='conversation-a')
        later = inspect_startup(self.home, self.root, harness='omp', harness_session='conversation-b')
        self.assertEqual(later['issues'], [])
        old_record = deepcopy(self.cloud.records[original['session_id']])
        restarted = self.application()
        fresh = self.startup(restarted)
        self.assertNotEqual(fresh['binding']['session_id'], original['session_id'])
        self.assertEqual(self.cloud.records[original['session_id']], old_record)
        self.assertEqual(self.inspect_exit()['status'], 'open')

    def test_shutdown_event_requires_choice_for_new_conversation_not_peer_takeover(self):
        first = self.application()
        original = self.startup(first)['binding']
        record_shutdown(self.home, self.root, harness='omp', harness_session='conversation-a')
        self.assertEqual(self.inspect_exit()['status'], 'open')
        fresh = self.application()
        result = self.startup(fresh, 'conversation-b')
        self.assertEqual(result['status'], 'decision_required')
        self.assertIsNone(fresh.binding)
        old_record = deepcopy(self.cloud.records[original['session_id']])
        new = self.startup(fresh, 'conversation-b', action='new')
        self.assertNotEqual(new['binding']['session_id'], original['session_id'])
        self.assertEqual(self.cloud.records[original['session_id']], old_record)

    def test_actual_resume_clears_old_end_event_without_changing_pause(self):
        first = self.application()
        self.startup(first)
        first.maintenance.pause(anchor='pause')
        record_shutdown(self.home, self.root, harness='omp', harness_session='conversation-a')
        resumed = self.startup(self.application())
        self.assertFalse(resumed['automatic_updates'])
        fresh = inspect_startup(self.home, self.root, harness='omp', harness_session='conversation-b')
        self.assertEqual(fresh['status'], 'initialize')
        self.assertNotIn('last_end_event', fresh['local_sessions'][0])

    def test_uncertain_request_is_not_replayed_by_startup_or_diagnosis(self):
        first = self.application()
        binding = self.startup(first)['binding']
        request = {'route': 'write', 'body': {'request_key': str(uuid.uuid4()), 'record_id': str(uuid.uuid4())}}
        path = f'.maven/runtime/sessions/{binding["session_id"]}/pending-memory.json'
        save_private(self.root, path, request)
        before = deepcopy(self.cloud.records)
        restarted = self.application()
        result = self.startup(restarted)
        self.assertEqual(result['status'], 'recovery')
        self.assertIsNone(restarted.binding)
        diagnosis = restarted.diagnose(str(self.root), harness='omp', harness_session='conversation-a')
        self.assertEqual(diagnosis['issues'][0]['request_key'], request['body']['request_key'])
        self.assertEqual(read_private(self.root, path), request)
        self.assertEqual(self.cloud.records, before)
        self.assertEqual(self.inspect_exit()['status'], 'open')

    def test_other_harness_with_same_native_identifier_cannot_adopt_session(self):
        first = self.application()
        binding = self.startup(first)['binding']
        result = inspect_startup(self.home, self.root, harness='codex', harness_session='conversation-a')
        self.assertNotIn('binding', result)
        self.assertEqual(inspect_shutdown(self.home, self.root, harness='codex',
                                         harness_session='conversation-a')['status'], 'unbound')
        self.assertEqual(self.inspect_exit()['binding'], binding)

    def test_duplicate_binding_rejection_leaves_application_unbound_on_retry(self):
        first = self.application()
        binding = self.startup(first)['binding']
        duplicate = self.application()
        for _ in range(2):
            with self.assertRaises(BoundaryError):
                duplicate.initialize(self.root, harness='omp', harness_session='conversation-a')
            self.assertIsNone(duplicate.binding)
        self.assertEqual([record['record_id'] for record in self.cloud.records.values()
                          if record['kind'] == 'session'], [binding['session_id']])

    def test_stale_remote_session_warns_without_closing_or_stealing_it(self):
        identifier = str(uuid.uuid4())
        record = {'record_id': identifier, 'revision': 1, 'kind': 'session', 'data_scope': 'owner',
                  'classification': 'Sensitive', 'project_id': self.project_id,
                  'updated_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), 'tags': [],
                  'data': {'state': 'active', 'workflow': 'maven-workflow',
                           'summary': json.dumps({'machine': 'other-host', 'harness': 'codex'})}}
        self.cloud.records[identifier] = deepcopy(record)
        app = self.application()
        result = self.startup(app)
        self.assertEqual(result['status'], 'decision_required')
        self.assertEqual(result['peer_attention'][0]['machine'], 'other-host')
        self.assertIsNone(app.binding)
        self.startup(app, action='new')
        self.assertEqual(self.cloud.records[identifier], record)

    def test_read_only_choice_is_not_overridden_by_startup(self):
        app = self.application()
        app.start('read_only', self.root, harness='omp', harness_session='conversation-a')
        self.assertEqual(self.startup(app)['status'], 'read_only')
        self.assertEqual(self.cloud.calls, [])
        self.assertIsNone(app.binding)

    def test_changed_folder_binding_blocks_old_operations_before_new_runtime_is_created(self):
        app = self.application()
        binding = self.startup(app)['binding']
        archived = self.root / '.maven-old'
        (self.root / '.maven').rename(archived)
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': str(uuid.uuid4())})
        before = deepcopy(self.cloud.records)
        with self.assertRaises(BoundaryError):
            app.service.context()
        with self.assertRaises(MaintenanceError):
            app.maintenance.enqueue('Do not enter another project', source={
                'origin': 'test', 'excerpt': 'The old session must stay isolated.'})
        self.assertFalse((self.root / '.maven/runtime').exists())
        self.assertTrue((archived / 'runtime/sessions' / binding['session_id']).exists())
        self.assertEqual(self.cloud.records, before)
        self.assertEqual(self.inspect_exit()['status'], 'unknown')

    def test_wrong_finish_receipt_cannot_authorize_automatic_exit(self):
        app = self.application()
        binding = self.startup(app)['binding']
        save_private(self.root, f'.maven/runtime/sessions/{binding["session_id"]}/finish.json', {
            'status': 'closed', 'root': str(self.root), 'project_id': self.project_id,
            'session_id': str(uuid.uuid4())})
        self.assertEqual(self.inspect_exit()['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
