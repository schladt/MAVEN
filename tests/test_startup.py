"""Offline lifecycle behavior: exact conversation identity, recovery and shutdown evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_mcp import MCPCloud, fixture_application, skill_page
from tools.coordination import private_lock
from tools.home import save_home
from tools.memory.boundary import BoundaryError, digest, read_private, save_private
from tools.memory.client import MemoryClientError
from tools.memory.lifecycle import SessionService
from tools.memory.maintenance import MaintenanceError
from tools.startup import conversation_binding, inspect_shutdown, inspect_startup, record_shutdown
from tools import startup as startup_module


class Crash(BaseException):
    """Abrupt interruption after a durable replacement boundary."""

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
        self.home.mkdir(mode=0o700)
        self.project_id = str(uuid.uuid4())
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': self.project_id})
        self.cloud = StartupCloud()
        self.cloud.records[self.project_id] = {
            'record_id': self.project_id, 'revision': 1, 'kind': 'entity', 'data_scope': 'owner',
            'project_id': None, 'tags': [], 'data': {'entity_type': 'project', 'name': 'MAVEN'}}

    def application(self):
        app = fixture_application(self.cloud, home=self.home)
        app._unbound_client = self.cloud
        self.addCleanup(app.close)
        return app

    def startup(self, app, conversation='conversation-a', action='continue', **kwargs):
        return app.startup(str(self.root), harness='omp', harness_session=conversation, action=action, **kwargs)

    def inspect_exit(self, conversation='conversation-a'):
        return inspect_shutdown(self.home, self.root, harness='omp', harness_session=conversation)

    def failed_initialization(self, conversation=None, *, before_registration=False):
        app = self.application()
        target, method = (SessionService, '_values') if before_registration else (self.cloud, 'invoke')
        with patch.object(target, method, side_effect=MemoryClientError('selected_login_expired')):
            with self.assertRaisesRegex(MemoryClientError, 'selected_login_expired'):
                app.initialize(self.root, harness='omp', harness_session=conversation)
        self.assertFalse(app.initialized)
        return app

    def test_expired_login_failure_can_start_new_native_session_without_touching_peers(self):
        app = self.failed_initialization()
        original, service = dict(app.binding), app.service
        prefix = f'.maven/runtime/sessions/{original["session_id"]}'
        persisted = read_private(self.root, prefix + '/mcp-binding.json')
        lifecycle = read_private(self.root, prefix + '/lifecycle.json')
        peer = self.startup(self.application(), 'peer-conversation')['binding']
        peer_record = deepcopy(self.cloud.records[peer['session_id']])
        result = self.startup(app, 'real-native-conversation', action='new')
        replacement = result['binding']
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(replacement['harness_session'], 'real-native-conversation')
        self.assertEqual(replacement['project_id'], original['project_id'])
        self.assertNotEqual(replacement['session_id'], original['session_id'])
        self.assertEqual(result['replaced_failed_binding'], original)
        self.assertEqual(self.cloud.records[peer['session_id']], peer_record)
        self.assertNotIn(original['session_id'], self.cloud.records)
        self.assertEqual(read_private(self.root, prefix + '/lifecycle.json'), lifecycle)
        self.assertEqual(read_private(self.root, prefix + '/mcp-binding.json'),
                         {**persisted, 'superseded_by': replacement['session_id']})
        evidence = read_private(self.root, prefix + '/failed-start-recovery.json')
        self.assertEqual(evidence['persisted_binding'], persisted)
        self.assertEqual(evidence['registry_entry']['state'], 'starting')
        registry = read_private(self.home, 'runtime/sessions.json')
        self.assertNotIn(original['session_id'], registry['sessions'])
        self.assertIn(peer['session_id'], registry['sessions'])
        self.assertEqual(conversation_binding(self.home, harness='omp', harness_session='real-native-conversation'),
                         replacement)
        with self.assertRaisesRegex(BoundaryError, 'superseded'):
            service.initialize()
        with self.assertRaisesRegex(MaintenanceError, 'superseded'):
            self.application().initialize(self.root, harness='omp', session_id=original['session_id'], resume=True)

    def test_failed_native_reservation_is_replaced_only_by_its_proven_successor(self):
        app = self.failed_initialization('conversation-a')
        original = dict(app.binding)
        original_native = read_private(self.home, 'runtime/conversations/' + digest(['omp', 'conversation-a']) + '.json')
        result = self.startup(app, action='new')
        evidence = read_private(self.root, f'.maven/runtime/sessions/{original["session_id"]}/failed-start-recovery.json')
        self.assertEqual(evidence['conversation_record'], original_native)
        self.assertEqual(evidence['replacement_binding'], result['binding'])
        self.assertEqual(self.inspect_exit()['binding'], result['binding'])
        self.assertNotIn(original['session_id'], self.cloud.records)

    def test_failed_inspect_accepts_real_native_identity_but_does_not_detach(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        records = deepcopy(self.cloud.records)
        result = self.startup(app, 'real-native-conversation', action='inspect')
        self.assertEqual(result['status'], 'recovery')
        self.assertEqual(result['binding'], original)
        self.assertTrue(result['failed_initialization']['local_replacement_allowed'])
        self.assertFalse(result['failed_initialization']['cloud_absence_verified'])
        self.assertTrue(result['project_check']['cloud_verified'])
        self.assertNotIn('available_skills', result)
        self.assertEqual(self.cloud.records, records)
        self.assertEqual(app.binding, original)
        self.assertIsNone(conversation_binding(self.home, harness='omp', harness_session='real-native-conversation'))
        with self.assertRaises(MaintenanceError):
            self.startup(app, 'real-native-conversation', action='continue')

    def test_unavailable_project_blocks_failed_replacement_and_explains_local_association(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        del self.cloud.records[self.project_id]
        for action in ('inspect', 'new'):
            with self.subTest(action=action):
                result = self.startup(app, 'real-native-conversation', action=action)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['project_check']['error'], 'record_unavailable')
                self.assertTrue(result['project_check']['local_association'])
                self.assertFalse(result['project_check']['cloud_verified'])
                self.assertFalse(result['active']['cloud_project_verified'])
                self.assertEqual(app.binding, original)
        self.assertFalse((self.root / '.maven/runtime/sessions' / original['session_id'] / 'failed-start-recovery.json').exists())
        self.assertEqual(self.cloud.records, {})

    def test_startup_uses_verified_renamed_project_without_changing_folder(self):
        self.cloud.records[self.project_id]['data']['name'] = 'maven-code'
        result = self.startup(self.application())
        self.assertEqual(result['active']['label'], 'maven-code')
        self.assertEqual(result['active']['label_source'], 'cloud_project')
        self.assertTrue(result['active']['cloud_project_verified'])
        self.assertEqual(result['binding']['root'], str(self.root))
        self.assertEqual(result['startup_review']['project_check']['name'], 'maven-code')
        self.assertEqual(read_private(self.root, '.maven/project.json')['project_id'], self.project_id)

    def test_initialized_or_previously_successful_binding_is_never_replaced(self):
        app = self.application()
        original = self.startup(app)['binding']
        records = deepcopy(self.cloud.records)
        for initialized in (True, False):
            app.initialized = initialized
            with self.subTest(initialized=initialized), self.assertRaises(MaintenanceError):
                self.startup(app, 'another-conversation', action='new')
        self.assertEqual(app.binding, original)
        self.assertEqual(self.cloud.records, records)

    def test_failed_replacement_refuses_retained_work_and_receipts(self):
        for name in ('pending-memory.json', 'session.json', 'last-memory-write.json',
                     'finish.json', 'approval.json'):
            with self.subTest(name=name):
                app = self.failed_initialization()
                original = dict(app.binding)
                path = f'.maven/runtime/sessions/{original["session_id"]}/{name}'
                retained = {'status': 'pending', 'body': {'request_key': str(uuid.uuid4())}}
                save_private(self.root, path, retained)
                with self.assertRaises(BoundaryError):
                    self.startup(app, action='new')
                self.assertEqual(app.binding, original)
                self.assertEqual(read_private(self.root, path), retained)
                self.assertNotIn(original['session_id'], self.cloud.records)

    def test_failed_replacement_refuses_any_assignment_checkpoint_or_closure(self):
        for field, value in (
                ('assignments', {str(uuid.uuid4()): {'state': 'claimed', 'worker_token': 'actual-worker'}}),
                ('checkpoints', {str(uuid.uuid4()): {'state': 'prepared'}}),
                ('closure', {'checkpoint_id': str(uuid.uuid4())})):
            with self.subTest(field=field):
                app = self.failed_initialization()
                original = dict(app.binding)
                state = app.maintenance._load()
                state[field] = value
                save_private(self.root, app.maintenance.path, state)
                with self.assertRaises(BoundaryError):
                    self.startup(app, action='new')
                self.assertEqual(app.binding, original)
                self.assertEqual(read_private(self.root, app.maintenance.path), state)

    def test_failed_replacement_refuses_active_registration_writers_and_reservations(self):
        for change in ('active', 'writers', 'reservation'):
            with self.subTest(change=change):
                app = self.failed_initialization()
                original = dict(app.binding)
                registry = read_private(self.home, 'runtime/sessions.json')
                if change == 'reservation':
                    registry['reservations'][str(self.root)] = {
                        'session_id': original['session_id'], 'reservation_id': str(uuid.uuid4())}
                elif change == 'writers':
                    registry['sessions'][original['session_id']]['writers'] = [str(uuid.uuid4())]
                else:
                    registry['sessions'][original['session_id']]['state'] = 'active'
                save_private(self.home, 'runtime/sessions.json', registry)
                with self.assertRaises(BoundaryError):
                    self.startup(app, action='new')
                self.assertEqual(read_private(self.home, 'runtime/sessions.json'), registry)
                self.assertEqual(app.binding, original)

    def test_failed_replacement_requires_same_project_instance_and_unclaimed_native_identity(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        other = self.base / 'other-project'
        other.mkdir()
        save_private(other, '.maven/project.json', {
            'schema': 1, 'root': str(other), 'project_id': str(uuid.uuid4())})
        with self.assertRaises(MaintenanceError):
            app.startup(other, harness='omp', harness_session='conversation-a', action='new')
        with self.assertRaises(MaintenanceError):
            app.startup(self.root, harness='codex', harness_session='conversation-a', action='new')
        peer = self.startup(self.application(), 'conversation-a')['binding']
        with self.assertRaises(MaintenanceError):
            self.startup(app, action='new')
        self.assertEqual(self.inspect_exit()['binding'], peer)
        app.env = self.base / 'different.env'
        with self.assertRaises(MaintenanceError):
            self.startup(app, 'new-native', action='new')
        self.assertEqual(app.binding, original)

    def test_failed_replacement_refuses_existing_cloud_session_even_when_search_hides_it(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        self.cloud.records[original['session_id']] = {
            'record_id': original['session_id'], 'revision': 1, 'kind': 'session', 'data_scope': 'owner',
            'project_id': self.project_id, 'tags': ['archived'], 'data': {'state': 'closed'}}
        records = deepcopy(self.cloud.records)
        with self.assertRaisesRegex(BoundaryError, 'cloud session exists'):
            self.startup(app, action='new')
        self.assertEqual(app.binding, original)
        self.assertEqual(self.cloud.records, records)

    def test_failed_replacement_does_not_treat_outage_or_invalid_read_as_absence(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        invoke = self.cloud.invoke
        for outcome in ('selected_login_expired', 'transport_unavailable', 'reference_unavailable', 'invalid'):
            def uncertain(route, body):
                if route == '/v1/memory/read' and body['record_id'] == original['session_id']:
                    if outcome == 'invalid':
                        return {'status': 'blocked'}
                    raise MemoryClientError(outcome)
                return invoke(route, body)
            with self.subTest(outcome=outcome), patch.object(self.cloud, 'invoke', side_effect=uncertain):
                with self.assertRaises(BoundaryError if outcome == 'invalid' else MemoryClientError):
                    self.startup(app, action='new')
            self.assertEqual(app.binding, original)
        self.assertFalse((self.root / '.maven/runtime/sessions' / original['session_id'] / 'failed-start-recovery.json').exists())

    def test_unbound_process_cannot_retire_discovered_failed_native_binding(self):
        failed = self.failed_initialization('conversation-a')
        original = dict(failed.binding)
        restarted = self.application()
        with self.assertRaises(MaintenanceError):
            self.startup(restarted, action='new')
        self.assertIsNone(restarted.binding)
        self.assertEqual(self.inspect_exit()['binding'], original)

    def test_failed_replacement_holds_registry_gate_against_other_session_use(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        invoke = self.cloud.invoke
        def gated_read(route, body):
            if route == '/v1/memory/read' and body['record_id'] == original['session_id']:
                # A second independent lock acquisition must fail, not be treated as a dead peer.
                with self.assertRaises(BoundaryError):
                    with private_lock(self.home, 'runtime/registry.lock', blocking=False):
                        pass
            return invoke(route, body)
        with patch.object(self.cloud, 'invoke', side_effect=gated_read):
            result = self.startup(app, action='new')
        self.assertEqual(result['status'], 'ready')

    def test_failed_before_lifecycle_registration_can_be_replaced_and_old_service_is_fenced(self):
        app = self.application()
        with patch.object(SessionService, '_values', side_effect=MemoryClientError('selected_login_expired')):
            with self.assertRaises(MemoryClientError):
                app.initialize(self.root, harness='omp')
        original, old_service = dict(app.binding), app.service
        self.assertFalse((self.root / '.maven/runtime/sessions' / original['session_id'] / 'lifecycle.json').exists())
        result = self.startup(app, action='new')
        self.assertEqual(result['status'], 'ready')
        with self.assertRaisesRegex(BoundaryError, 'superseded'):
            old_service.initialize()

    def test_definitively_rejected_initial_write_is_preserved_not_replayed(self):
        app = self.application()
        invoke = self.cloud.invoke
        def reject_start(route, body):
            if route == '/v1/memory/write' and body.get('kind') == 'session':
                raise MemoryClientError('reference_unavailable')
            return invoke(route, body)
        with patch.object(self.cloud, 'invoke', side_effect=reject_start):
            with self.assertRaisesRegex(MemoryClientError, 'reference_unavailable'):
                app.initialize(self.root, harness='omp')
        original = dict(app.binding)
        path = f'.maven/runtime/sessions/{original["session_id"]}/failed-memory-write.json'
        rejected = read_private(self.root, path)
        calls = len(self.cloud.calls)
        result = self.startup(app, action='new')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(read_private(self.root, path), rejected)
        self.assertFalse(any(body.get('request_key') == rejected['request']['body']['request_key']
                             for _, body in self.cloud.calls[calls:]))

    def test_exact_session_absence_is_required_even_when_peer_listing_is_incomplete(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        page = {'sessions': [], 'next_after_id': str(uuid.uuid4()), 'complete': False}
        with patch('tools.startup.project_sessions', return_value=page):
            result = self.startup(app, action='new')
        self.assertEqual(result['startup_review']['cloud_sessions'], page)
        self.assertIn(('/v1/memory/read', {'record_id': original['session_id'], 'include_archived': True}), self.cloud.calls)
        self.assertEqual(result['startup_review']['cloud_session_absence_verified'], original['session_id'])

    def test_changed_project_and_initialized_persisted_binding_block_failed_replacement(self):
        app = self.failed_initialization()
        original = dict(app.binding)
        path = f'.maven/runtime/sessions/{original["session_id"]}/mcp-binding.json'
        save_private(self.root, path, {**read_private(self.root, path), 'initialized': True})
        with self.assertRaisesRegex(BoundaryError, 'never-initialized'):
            self.startup(app, action='new')
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': str(uuid.uuid4())})
        with self.assertRaises(MaintenanceError):
            self.startup(app, action='new')
        self.assertEqual(app.binding, original)

    def test_unenabled_startup_is_sessionless_and_does_not_contact_cloud(self):
        self.home.rmdir()  # Exercise a genuinely unconfigured installation.
        scratch = self.base / 'scratch'
        scratch.mkdir()
        app = self.application()
        result = app.startup(str(scratch), harness='omp', harness_session='conversation-a', action='continue')
        self.assertEqual(result['status'], 'choose')
        self.assertEqual(self.cloud.calls, [])
        self.assertIsNone(app.binding)
        self.assertFalse((scratch / '.maven').exists())
        self.assertFalse(self.home.exists())

    def test_native_hook_remains_local_even_with_enabled_skill_catalog(self):
        self.cloud.pages = [skill_page('calendar')]
        with patch('tools.memory.environment.configured_client', side_effect=AssertionError('hook read cloud')):
            result = inspect_startup(self.home, self.root, harness='omp', harness_session='conversation-a')
        self.assertEqual(result['status'], 'initialize')
        self.assertNotIn('available_skills', result)
        self.assertEqual(self.cloud.calls, [])

    def test_selected_project_startup_returns_only_applicable_summaries(self):
        self.cloud.pages = [skill_page('calendar'), skill_page('local', scope={'project': self.project_id}),
                            skill_page('other', scope={'project': str(uuid.uuid4())}),
                            {**skill_page('synthetic'), 'data_scope': 'synthetic'}]
        app = self.application()
        with patch.object(self.cloud, 'invoke', wraps=self.cloud.invoke) as invoke:
            inspected = self.startup(app, action='inspect')
        self.assertEqual(inspected['active']['mode'], 'off')
        self.assertNotIn('available_skills', inspected)
        self.assertFalse(any(call.args[0] == '/v1/pages/index' for call in invoke.call_args_list))
        self.assertIsNone(app.binding)
        initialized = self.startup(app)
        self.assertEqual([page['slug'] for page in initialized['available_skills']['pages']], ['calendar', 'local'])
        self.assertTrue(all('description' in page and 'body_markdown' not in page
                            for page in initialized['available_skills']['pages']))

    def test_inspection_reads_project_only_without_creating_a_session(self):
        app = self.application()
        result = self.startup(app, action='inspect')
        self.assertEqual(result['status'], 'initialize')
        self.assertTrue(result['live_state_checked'])
        self.assertIsNone(app.binding)
        self.assertFalse((self.root / '.maven/runtime').exists())
        self.assertTrue(self.cloud.calls)
        self.assertEqual(self.cloud.calls[0], ('/v1/memory/read', {'record_id': self.project_id}))
        self.assertTrue(all(route == '/v1/memory/search' and body['project_id'] == self.project_id
                            and body['scope'] == 'project' for route, body in self.cloud.calls[1:]))

    def test_home_startup_with_existing_cloud_session_reads_summary_then_current_record(self):
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID="instance-one"\n')
        (self.home / '.env').chmod(0o600)
        save_home(self.home, root=self.root, project_id=self.project_id, instance_id='instance-one',
                  label='Home', consent={'origin': 'Owner conversation', 'excerpt': 'Use this project as Home.'})
        self.cloud.pages = [skill_page('home-skill', scope={'project': self.project_id}),
                            skill_page('universal')]
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
        self.assertEqual([page['slug'] for page in result['available_skills']['pages']],
                         ['home-skill', 'universal'])
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

    def test_closed_conversation_waits_for_access_choice_before_skill_discovery(self):
        self.cloud.pages = [skill_page('calendar')]
        original = self.application()
        binding = self.startup(original)['binding']
        checkpoint = original.maintenance.checkpoint(summary='Work complete.', refs=[])
        original.maintenance.finish(checkpoint['checkpoint']['checkpoint_id'])
        records = deepcopy(self.cloud.records)
        restarted = self.application()
        with patch.object(self.cloud, 'invoke', wraps=self.cloud.invoke) as invoke:
            inspected = self.startup(restarted, action='inspect')
        self.assertEqual(inspected['status'], 'closed')
        self.assertEqual(inspected['active']['mode'], 'off')
        self.assertNotIn('available_skills', inspected)
        self.assertFalse(any(call.args[0] == '/v1/pages/index' for call in invoke.call_args_list))
        with self.assertRaises(MaintenanceError):
            restarted.context('calendar')
        restarted.start('off', self.root, harness='omp', harness_session='conversation-a')
        with patch.object(self.cloud, 'invoke', wraps=self.cloud.invoke) as invoke:
            self.assertNotIn('available_skills', self.startup(restarted, action='inspect'))
        invoke.assert_not_called()
        restarted.start('read_only', self.root, harness='omp', harness_session='conversation-a')
        readonly = self.startup(restarted, action='inspect')
        self.assertEqual(readonly['active']['mode'], 'read_only')
        self.assertEqual([page['slug'] for page in readonly['available_skills']['pages']], ['calendar'])
        self.assertIsNone(restarted.binding)
        self.assertEqual(self.cloud.records, records)
        fresh = self.startup(self.application(), action='new')
        self.assertNotEqual(fresh['binding']['session_id'], binding['session_id'])
        self.assertEqual([page['slug'] for page in fresh['available_skills']['pages']], ['calendar'])
        self.assertEqual(self.cloud.records[binding['session_id']], records[binding['session_id']])

    def test_shutdown_event_requires_choice_for_new_conversation_not_peer_takeover(self):
        first = self.application()
        self.cloud.pages = [skill_page('calendar')]
        original = self.startup(first)['binding']
        record_shutdown(self.home, self.root, harness='omp', harness_session='conversation-a')
        self.assertEqual(self.inspect_exit()['status'], 'open')
        fresh = self.application()
        with patch.object(self.cloud, 'invoke', wraps=self.cloud.invoke) as invoke:
            inspected = self.startup(fresh, 'conversation-b', action='inspect')
        self.assertEqual(inspected['status'], 'recovery')
        self.assertTrue(inspected['choices'])
        self.assertNotIn('available_skills', inspected)
        self.assertFalse(any(call.args[0] == '/v1/pages/index' for call in invoke.call_args_list))
        result = self.startup(fresh, 'conversation-b')
        self.assertEqual(result['status'], 'decision_required')
        self.assertNotIn('available_skills', result)
        self.assertIsNone(fresh.binding)
        old_record = deepcopy(self.cloud.records[original['session_id']])
        new = self.startup(fresh, 'conversation-b', action='new')
        self.assertNotEqual(new['binding']['session_id'], original['session_id'])
        self.assertEqual([page['slug'] for page in new['available_skills']['pages']], ['calendar'])
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
        self.assertEqual(sorted(path.name for path in (self.root / '.maven/runtime/sessions').iterdir()),
                         [binding['session_id']])
        registry = read_private(self.home, 'runtime/sessions.json')
        self.assertEqual(list(registry['sessions']), [binding['session_id']])
        self.assertEqual(conversation_binding(self.home, harness='omp', harness_session='conversation-a'), binding)
        checkpoint = first.maintenance.checkpoint(summary='Legitimate session finished')
        first.maintenance.finish(checkpoint['checkpoint']['checkpoint_id'])
        from tools.project_switch import apply_switch, prepare_switch
        destination = str(uuid.uuid4())
        self.cloud.records[destination] = {
            'record_id': destination, 'revision': 1, 'kind': 'entity', 'data_scope': 'owner',
            'project_id': None, 'tags': [], 'data': {'entity_type': 'project', 'name': 'Destination'}}
        selection = {'connection': {'instance_id': str(uuid.uuid4())}, 'environment': {}}
        with patch('tools.memory.environment.configured_client', return_value=self.cloud):
            candidate = prepare_switch(self.home, self.root, destination, str(uuid.uuid4()), selection)
            self.assertEqual(apply_switch(self.home, candidate, selection,
                                         {'origin': 'test', 'excerpt': 'Switch after clean closure'})['status'], 'ready')

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

    def test_explicit_project_selection_replaces_prior_sessionless_mode_for_discovery(self):
        self.cloud.pages = [skill_page('calendar')]
        for mode in ('read_only', 'off'):
            with self.subTest(mode=mode):
                app = self.application()
                conversation = 'conversation-' + mode
                app.start(mode, self.root, harness='omp', harness_session=conversation)
                app.initialize(self.root, harness='omp', harness_session=conversation)
                result = self.startup(app, conversation, action='inspect')
                self.assertEqual(result['active']['mode'], 'project')
                self.assertEqual(result['available_skills']['status'], 'ready')
                self.assertEqual(result['available_skills']['pages'][0]['slug'], 'calendar')

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

    def test_failed_start_replacement_resumes_each_durable_boundary_with_exact_ids(self):
        for native in (None, 'reserved'):
            for boundary in ('intent', 'fence', 'registry', 'successor', 'native', 'unregistered'):
                with self.subTest(native=native, boundary=boundary):
                    conversation = (native + '-' + boundary) if native else None
                    app = self.failed_initialization(conversation, before_registration=boundary == 'unregistered')
                    original = dict(app.binding)
                    requested = conversation or 'new-' + boundary
                    save = startup_module.save_private

                    def interrupt(root, path, value):
                        save(root, path, value)
                        selected = (
                            boundary == 'intent' and path.endswith('/failed-start-recovery.json')
                            or boundary == 'fence' and 'superseded_by' in value
                            or boundary == 'registry' and path == 'runtime/sessions.json'
                            or boundary in ('successor', 'unregistered') and path.endswith('/mcp-binding.json')
                            and value.get('session_id') != original['session_id']
                            or boundary == 'native' and path.startswith('runtime/conversations/'))
                        if selected:
                            raise Crash()

                    with patch.object(startup_module, 'save_private', side_effect=interrupt), self.assertRaises(Crash):
                        self.startup(app, requested, action='new')
                    receipt = read_private(self.root,
                        f'.maven/runtime/sessions/{original["session_id"]}/failed-start-recovery.json')
                    replacement = receipt['replacement_binding']
                    restored = self.application()
                    exact = {} if native and boundary != 'native' else {'failed_start_session_id': original['session_id']}
                    result = restored.startup(str(self.root), harness='omp', harness_session=requested,
                                              action='new', **exact)
                    self.assertEqual(result['status'], 'ready')
                    self.assertEqual(result['binding'], replacement)
                    self.assertNotIn(original['session_id'], self.cloud.records)
                    self.assertIn(replacement['session_id'], self.cloud.records)
                    self.assertNotIn(original['session_id'], read_private(self.home, 'runtime/sessions.json')['sessions'])
                    self.assertEqual(conversation_binding(self.home, harness='omp', harness_session=requested), replacement)
                    self.assertEqual(read_private(self.root,
                        f'.maven/runtime/sessions/{original["session_id"]}/failed-start-recovery.json'), receipt)

    def test_replacement_recovery_refuses_changed_identity_and_retained_work(self):
        app = self.failed_initialization('reserved')
        original = dict(app.binding)
        save = startup_module.save_private

        def interrupt(root, path, value):
            save(root, path, value)
            if path.endswith('/failed-start-recovery.json'):
                raise Crash()

        with patch.object(startup_module, 'save_private', side_effect=interrupt), self.assertRaises(Crash):
            self.startup(app, 'reserved', action='new')
        replacement = read_private(self.root,
            f'.maven/runtime/sessions/{original["session_id"]}/failed-start-recovery.json')['replacement_binding']
        for harness, conversation in (('codex', 'reserved'), ('omp', 'different')):
            with self.subTest(harness=harness, conversation=conversation), self.assertRaises(BoundaryError):
                self.application().startup(str(self.root), harness=harness, harness_session=conversation,
                                           action='new', failed_start_session_id=original['session_id'])
        other = self.base / 'other-project'
        other.mkdir()
        save_private(other, '.maven/project.json', {
            'schema': 1, 'root': str(other), 'project_id': str(uuid.uuid4())})
        with self.assertRaises(BoundaryError):
            self.application().startup(str(other), harness='omp', harness_session='reserved',
                                       action='new', failed_start_session_id=original['session_id'])
        project = read_private(self.root, '.maven/project.json')
        save_private(self.root, '.maven/project.json', {**project, 'project_id': str(uuid.uuid4())})
        with self.assertRaises(BoundaryError):
            self.application().startup(str(self.root), harness='omp', harness_session='reserved',
                                       action='new', failed_start_session_id=original['session_id'])
        save_private(self.root, '.maven/project.json', project)
        pending = f'.maven/runtime/sessions/{original["session_id"]}/pending-memory.json'
        save_private(self.root, pending, {'route': 'write', 'body': {'request_key': str(uuid.uuid4())}})
        with self.assertRaises(BoundaryError):
            self.application().startup(str(self.root), harness='omp', harness_session='reserved',
                                       action='new', failed_start_session_id=original['session_id'])
        self.assertNotIn(replacement['session_id'], self.cloud.records)
        self.assertTrue((self.root / pending).exists())

    def test_wrong_finish_receipt_cannot_authorize_automatic_exit(self):
        app = self.application()
        binding = self.startup(app)['binding']
        save_private(self.root, f'.maven/runtime/sessions/{binding["session_id"]}/finish.json', {
            'status': 'closed', 'root': str(self.root), 'project_id': self.project_id,
            'session_id': str(uuid.uuid4())})
        self.assertEqual(self.inspect_exit()['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
