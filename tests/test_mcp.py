"""Local SDK2 behavior and real stdio transport, with an injected cloud fixture only."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tools.coordination import SessionBusy, private_lock
from tools.mcp import server as mcp_server
from tools.mcp.server import MavenApplication, create_server, main
from tools.memory.client import MemoryClientError
from tools.memory.boundary import BoundaryError, read_private, save_private
from tools.memory.lifecycle import SessionService
from test_workflow import MemoryCloud


SOURCE = {'origin': 'Owner conversation', 'excerpt': 'Preserve the verified finding.'}
NOTE = {'kind': 'note', 'data': {'title': 'Finding', 'body_markdown': 'Verified locally.', 'category': 'work'}}


class MCPCloud(MemoryCloud):
    """Answers the protocol-4 routes (search ranking fields, project_context, revisions, generations)."""
    pages = []

    def invoke(self, route, body):
        if route == '/v1/pages/index':
            return {'status': 'ready', 'authority_generation': 1,
                    'data': {'pages': list(self.pages), 'authority_generation': 1, 'next_after_slug': None}}
        if route == '/v1/memory/project_context':
            return {'status': 'ready', 'authority_generation': 1, 'data': {
                'latest_handover': None, 'active_peers': [], 'tasks': [], 'blockers': [],
                'owner_preferences': []}}
        if route == '/v1/memory/revisions':
            return {'status': 'ready', 'authority_generation': 1, 'data': {'revisions': {
                record_id: self.records[record_id]['revision']
                for record_id in body['record_ids'] if record_id in self.records}}}
        if route == '/v1/memory/search':
            listing = {key: value for key, value in body.items() if key not in ('include_archived', 'offset')}
            result = super().invoke(route, listing)
            records = [record for record in result['data']['records'] if body.get('include_archived')
                       or 'archived' not in record.get('tags', []) and record['data'].get('state') != 'archived']
            if body.get('query'):
                terms = body['query'].lower().split()
                records = [dict(record, rank=1, snippet=None) for record in records
                           if all(term in json.dumps(record['data']).lower() for term in terms)]
                return {'status': 'ready', 'authority_generation': 1,
                        'data': {'records': records, 'next_offset': None}}
            return {'status': 'ready', 'authority_generation': 1, 'data': {
                'records': [dict(record, snippet=None) for record in records],
                'next_after_id': result['data']['next_after_id']}}
        result = super().invoke(route, body)
        return {**result, 'authority_generation': 1} if isinstance(result, dict) else result


class SharedMCPCloud(MCPCloud):
    """Private fixture backend shared by independent stdio worker processes."""
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def invoke(self, route, body):
        with private_lock(self.directory, 'cloud.lock'):
            if (self.directory / 'cloud.json').exists():
                stored = read_private(self.directory, 'cloud.json')
                self.records = stored['records']
                self.requests = stored['requests']
                self.history = {(item['record_id'], item['revision']): item['record']
                                for item in stored['history']}
            try:
                return super().invoke(route, body)
            finally:
                save_private(self.directory, 'cloud.json', {
                    'records': self.records, 'requests': self.requests,
                    'history': [{'record_id': record_id, 'revision': revision, 'record': record}
                                for (record_id, revision), record in self.history.items()]})


def serve_fixture(arguments):
    parser = argparse.ArgumentParser(description='Test-only real stdio MCP with private fixture storage.')
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--env', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--fixture-store', type=Path)
    args = parser.parse_args(arguments)
    if args.root is not None:
        root = args.root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with private_lock(root, '.maven/fixture-project.lock'):
            if not (root / '.maven/project.json').exists():
                save_private(root, '.maven/project.json', {
                    'schema': 1, 'root': str(root), 'project_id': str(uuid.uuid4())})
    cloud = SharedMCPCloud(args.fixture_store or args.home / 'fixture-cloud')
    selected = ['--home', str(args.home)]
    if args.env is not None:
        selected.extend(['--env', str(args.env)])
    main(selected, application_factory=lambda **kwargs: fixture_application(cloud, **kwargs))


def fixture_application(cloud, **kwargs):
    def service(root, session_id, **selection):
        return SessionService(root, session_id, client=cloud, **selection)
    return MavenApplication(service_factory=service, **kwargs)


class MCPBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'ordinary-directory'
        self.root.mkdir()
        self.home = self.root.parent / 'instance'
        self.project = str(uuid.uuid4())
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': self.project})
        self.cloud = MCPCloud()
        self.app = self.application()
        self.server = create_server(self.app)

    def application(self, **kwargs):
        app = fixture_application(self.cloud, home=self.home, **kwargs)
        self.addCleanup(app.close)
        return app

    async def call(self, name, arguments=None, *, server=None):
        result = await (server or self.server).call_tool(name, arguments or {})
        self.assertIsInstance(result.structured_content, dict)
        return result, result.structured_content

    async def initialize(self):
        result, value = await self.call('maven_initialize', {'root': str(self.root), 'harness': 'mcp'})
        self.assertFalse(result.is_error, value)
        self.assertEqual(value['status'], 'ready')
        return value['binding']

    async def test_project_enablement_rejections_name_the_problem(self):
        _, begun = await self.call('maven_project', {'parameters': {'root': str(self.root)}})
        question = begun['pending_interaction']
        identity = {'action': 'respond', 'kind': 'project', 'operation_id': begun['operation_id'],
                    'interaction_id': question['interaction_id'], 'revision': question['revision']}
        _, rejected = await self.call('maven_admin', {**identity, 'response': {'root': str(self.root)}})
        self.assertEqual(rejected['error'], 'invalid_request')
        self.assertIn('mode, name, project_id, root', rejected['message'])
        _, extra = await self.call('maven_project', {'parameters': {'root': str(self.root), 'action': 'respond'}})
        self.assertEqual(extra['error'], 'invalid_request')
        self.assertIn('Unsupported administration kind or parameters', extra['message'])

    async def test_failed_initialization_never_becomes_ready_by_context_only(self):
        self.cloud.lose_next = True
        first, value = await self.call('maven_initialize', {'root': str(self.root)})
        self.assertTrue(first.is_error)
        self.assertEqual(value['status'], 'uncertain')
        identifier = value['binding']['session_id']
        second, value = await self.call('maven_initialize', {'root': str(self.root)})
        self.assertTrue(second.is_error)
        self.assertNotEqual(value['status'], 'ready')
        self.assertEqual(self.app.binding['session_id'], identifier)
        self.assertFalse(self.app.initialized)
        blocked, _ = await self.call('maven_save', {'change': NOTE, 'source': SOURCE})
        self.assertTrue(blocked.is_error)
        recovered, _ = await self.call('maven_recover', {'workers_stopped': True})
        self.assertFalse(recovered.is_error)
        binding = await self.initialize()
        self.assertEqual(binding['session_id'], identifier)
        sessions = [record for record in self.cloud.records.values() if record['kind'] == 'session']
        self.assertEqual(len(sessions), 1)

    async def test_reconnect_checks_persisted_environment_and_harness(self):
        binding = await self.initialize()
        restored = self.application()
        result = restored.initialize(self.root, session_id=binding['session_id'], resume=True)
        self.assertEqual(result['binding'], binding)
        wrong_environment = self.application(env=self.home / 'other.env')
        with self.assertRaises(BoundaryError):
            wrong_environment.initialize(self.root, session_id=binding['session_id'], resume=True)
        with self.assertRaises(BoundaryError):
            self.application().initialize(self.root, session_id=binding['session_id'], resume=True, harness='other')
        with self.assertRaises(BoundaryError):
            self.application().initialize(self.root, session_id=binding['session_id'])
        self.assertEqual(len([r for r in self.cloud.records.values() if r['kind'] == 'session']), 1)

    async def test_worker_cannot_replace_session_or_escape_assignment(self):
        binding = await self.initialize()
        _, queued = await self.call('maven_maintenance', {'action': 'enqueue', 'intent': 'Preserve finding', 'source': SOURCE})
        operation = queued['assignment']['operation_id']
        worker = self.application()
        worker.initialize(self.root, session_id=binding['session_id'], resume=True, assignment_id=operation)
        server = create_server(worker)
        for name, arguments in (
            ('maven_save', {'change': NOTE, 'source': SOURCE}),
            ('maven_maintenance', {'action': 'status', 'operation_id': str(uuid.uuid4())}),
            ('maven_search', {'scope': 'all'}),
            ('maven_initialize', {'root': str(self.root)}),
            ('maven_pause', {}),
        ):
            result, _ = await self.call(name, arguments, server=server)
            self.assertTrue(result.is_error, name)
        claim, claimed = await self.call('maven_maintenance', {
            'action': 'claim', 'operation_id': operation, 'worker': 'bounded-worker'}, server=server)
        self.assertFalse(claim.is_error, claimed)
        self.assertEqual(claimed['assignment']['operation_id'], operation)
        unknown = self.application()
        with self.assertRaises(BoundaryError):
            unknown.initialize(self.root, session_id=str(uuid.uuid4()), resume=True, assignment_id=operation)
        with self.assertRaises(BoundaryError):
            unknown.initialize(self.root)
        self.assertEqual(len([r for r in self.cloud.records.values() if r['kind'] == 'session']), 1)

    async def test_close_peer_requires_bound_primary_session_and_passes_arguments(self):
        peer = str(uuid.uuid4())
        record = {'record_id': peer, 'revision': 3, 'kind': 'session', 'classification': 'Sensitive',
                  'data_scope': 'owner', 'project_id': self.project, 'tags': ['work-session'],
                  'data': {'state': 'active', 'summary': json.dumps({'harness': 'omp', 'machine': 'old-laptop'})}}
        self.cloud.records[peer] = record
        self.cloud.history[(peer, 3)] = json.loads(json.dumps(record))
        arguments = {'session_id': peer, 'revision': 3, 'source': SOURCE, 'reason': 'Owner confirmed it stopped'}
        unbound, _ = await self.call('maven_close_peer', arguments)
        self.assertTrue(unbound.is_error)
        binding = await self.initialize()
        _, queued = await self.call('maven_maintenance', {'action': 'enqueue', 'intent': 'Preserve finding', 'source': SOURCE})
        worker = self.application()
        worker.initialize(self.root, session_id=binding['session_id'], resume=True,
                          assignment_id=queued['assignment']['operation_id'])
        refused, _ = await self.call('maven_close_peer', arguments, server=create_server(worker))
        self.assertTrue(refused.is_error)
        self.assertEqual(self.cloud.records[peer], record)
        with patch.object(SessionService, 'close_peer', autospec=True,
                          return_value={'status': 'peer_closed'}) as close:
            result, value = await self.call('maven_close_peer', arguments)
        self.assertFalse(result.is_error, value)
        close.assert_called_once_with(self.app.service, peer, expected_revision=3, source=SOURCE,
                                      reason='Owner confirmed it stopped')
        result, value = await self.call('maven_close_peer', arguments)
        self.assertFalse(result.is_error, value)
        self.assertEqual(value['closed_session'], {'record_id': peer, 'revision': 4})
        self.assertEqual(self.cloud.records[peer]['data']['state'], 'abandoned')
        self.assertEqual(self.cloud.records[binding['session_id']]['data']['state'], 'active')

    async def test_pending_and_paused_are_not_saved_and_errors_hide_credentials(self):
        await self.initialize()
        await self.call('maven_pause')
        _, paused = await self.call('maven_maintenance', {'action': 'enqueue', 'intent': 'Paused finding', 'source': SOURCE})
        self.assertEqual(paused['status'], 'paused')
        self.assertFalse(paused['accepted'])
        self.assertFalse(paused['cloud_saved'])
        _, queued = await self.call('maven_maintenance', {
            'action': 'enqueue', 'intent': 'Explicit pending finding', 'source': SOURCE, 'automatic': False})
        _, checkpoint = await self.call('maven_checkpoint', {'summary': 'Pending boundary'})
        self.assertEqual(checkpoint['status'], 'pending')
        self.assertFalse(checkpoint['cloud_saved'])
        self.assertIn(queued['assignment']['operation_id'], checkpoint['pending_assignments'])
        secret = 'credential-value-that-must-not-escape'
        invalid, _ = await self.call('maven_search', {'limit': secret})
        self.assertTrue(invalid.is_error)
        self.assertNotIn(secret, invalid.model_dump_json())
        with patch.object(self.app.service, 'context', side_effect=BoundaryError(secret)):
            failed, _ = await self.call('maven_context')
        self.assertTrue(failed.is_error)
        self.assertNotIn(secret, failed.model_dump_json())

    async def test_errors_distinguish_availability_from_rejections(self):
        await self.initialize()
        rejected = MemoryClientError('invalid_request')
        rejected.detail = 'note requires data.body_markdown'
        with patch.object(self.app.service, 'search', side_effect=rejected):
            result, value = await self.call('maven_search', {'query': 'x'})
        self.assertTrue(result.is_error)
        self.assertEqual((value['status'], value['error']), ('error', 'invalid_request'))
        self.assertEqual(value['detail'], 'note requires data.body_markdown')
        self.assertNotIn('unavailable', value['message'])
        with patch.object(self.app.service, 'search', side_effect=MemoryClientError('warming')):
            _, value = await self.call('maven_search', {'query': 'x'})
        self.assertEqual(value['status'], 'blocked')
        self.assertIn('memory unavailable', value['message'])
        with patch.object(self.app.service, 'search', side_effect=MemoryClientError('selected_login_unavailable_or_expired')):
            _, value = await self.call('maven_search', {'query': 'x'})
        self.assertEqual(value['status'], 'blocked')
        with patch.object(self.app.service, 'search', side_effect=SessionBusy('secret lock path')):
            _, value = await self.call('maven_search', {'query': 'x'})
        self.assertEqual((value['status'], value['error']), ('error', 'session_busy'))
        self.assertNotIn('secret lock path', json.dumps(value))

    async def test_tool_descriptions_and_schemas_describe_the_record_model(self):
        from tools.memory.validation import MEMORY_DATA_FIELDS
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        for name in ('maven_save', 'maven_maintenance'):
            description = tools[name].description
            for kind, fields in MEMORY_DATA_FIELDS.items():
                self.assertIn(f'{kind}: required', description)
                for field in fields.get('required', []):
                    self.assertIn(field, description)
            self.assertIn('50 changes', description)
            self.assertIn('100 checkpoint refs', description)
            self.assertIn('subject_id', description)
        search = tools['maven_search']
        self.assertEqual(search.input_schema['properties']['limit']['maximum'], 20)
        for parameter in ('include_archived', 'offset', 'unbound'):
            self.assertIn(parameter, search.input_schema['properties'])
        self.assertIn('snippet', search.description)
        self.assertIn('detail', tools['maven_maintenance'].input_schema['properties'])
        self.assertIn('nothing_to_record', tools['maven_resume'].input_schema['properties'])
        self.assertIn('detail', tools['maven_admin'].input_schema['properties'])
        self.assertTrue(tools['maven_instructions'].annotations.read_only_hint)

    async def test_unbound_universal_consultation_never_binds_or_captures(self):
        universal, project = str(uuid.uuid4()), str(uuid.uuid4())
        for identifier, owner in ((universal, None), (project, self.project)):
            self.cloud.records[identifier] = {
                'record_id': identifier, 'kind': 'decision', 'revision': 1, 'project_id': owner,
                'data': {'title': 'Never name clients publicly', 'state': 'accepted'}, 'tags': [],
                'classification': 'Sensitive', 'data_scope': 'owner'}
        self.cloud.pages = [{'slug': 'core', 'page_kind': 'directive', 'title': 'Core', 'revision': 2,
                             'requires': [], 'data_scope': 'owner', 'scope': {}},
                            {'slug': 'local', 'page_kind': 'skill', 'revision': 1, 'data_scope': 'owner',
                             'scope': {'project': self.project}}]
        with patch.object(self.app, 'unbound_client', return_value=self.cloud):
            result, found = await self.call('maven_search', {'query': 'clients', 'unbound': True})
            self.assertFalse(result.is_error, found)
            self.assertTrue(found['unbound'])
            self.assertEqual([item['record_id'] for item in found['records']], [universal])
            _, implicit = await self.call('maven_search', {'scope': 'universal'})
            self.assertTrue(implicit['unbound'])
            _, record = await self.call('maven_read', {'record_id': universal, 'unbound': True})
            self.assertTrue(record['unbound'])
            denied, _ = await self.call('maven_read', {'record_id': project, 'unbound': True})
            self.assertTrue(denied.is_error)
            escaped, _ = await self.call('maven_search', {'scope': 'all', 'unbound': True})
            self.assertTrue(escaped.is_error)
            _, listed = await self.call('maven_instructions')
        self.assertEqual([(page['slug'], page['loaded']) for page in listed['pages']], [('core', False)])
        self.assertIsNone(self.app.service)
        self.assertIsNone(self.app.binding)
        self.assertFalse(any(route == '/v1/memory/write' for route, _ in self.cloud.calls))
        bound_only, _ = await self.call('maven_search', {'scope': 'context'})
        self.assertTrue(bound_only.is_error)

    async def test_status_resumes_only_exact_native_conversation_not_newest_peer(self):
        _, first = await self.call('maven_initialize', {
            'root': str(self.root), 'harness': 'omp', 'harness_session': 'conversation-a'})
        peer = self.application()
        peer.initialize(self.root, harness='omp', harness_session='conversation-b')
        restarted = create_server(self.application())
        _, status = await self.call('maven_status', {
            'cwd': str(self.root / '.maven'), 'harness': 'omp', 'harness_session': 'conversation-a'},
            server=restarted)
        self.assertEqual(status['status'], 'not_initialized')
        self.assertEqual(status['rebind_hint']['binding'], first['binding'])
        _, fresh = await self.call('maven_status', {
            'cwd': str(self.root), 'harness': 'omp', 'harness_session': 'conversation-c'}, server=restarted)
        self.assertNotIn('binding', fresh['rebind_hint'])
        self.assertEqual({item['session_id'] for item in fresh['rebind_hint']['local_sessions']},
                         {first['binding']['session_id'], peer.binding['session_id']})

    def configure_home(self, label='nexus'):
        from tools.home import save_home
        self.home.mkdir(mode=0o700, exist_ok=True)
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID="instance-one"\n')
        (self.home / '.env').chmod(0o600)
        root = (Path(self.temporary.name) / 'nexus').resolve()
        root.mkdir()
        project = str(uuid.uuid4())
        save_private(root, '.maven/project.json', {'schema': 1, 'root': str(root), 'project_id': project})
        save_home(self.home, root=root, project_id=project, instance_id='instance-one', label=label, consent=SOURCE)
        return root, project

    def scratch(self):
        folder = (Path(self.temporary.name) / 'scratch').resolve()
        folder.mkdir(exist_ok=True)
        return folder

    async def test_status_prefers_rebind_hint_then_start_hint_and_always_reports_active(self):
        scratch = self.scratch()
        _, outside = await self.call('maven_status', {'cwd': str(scratch)})
        self.assertNotIn('rebind_hint', outside)
        self.assertEqual((outside['start_hint']['folder'], outside['start_hint']['options']),
                         (str(scratch), ['project', 'read_only']))
        self.assertEqual(outside['next_action'], outside['start_hint']['next_action'])
        self.assertEqual(outside['active']['mode'], 'off')
        _, inside = await self.call('maven_status', {'cwd': str(self.root / '.maven')})
        self.assertEqual(inside['rebind_hint']['root'], str(self.root.resolve()))
        self.assertNotIn('start_hint', inside)
        self.assertEqual(inside['active']['mode'], 'off')
        _, idle = await self.call('maven_status', {'cwd': str(self.root)})
        self.assertEqual(idle['rebind_hint']['status'], 'initialize')
        self.assertEqual(idle['active']['mode'], 'off')
        await self.initialize()
        _, bound = await self.call('maven_status', {'cwd': str(scratch)})
        self.assertFalse({'rebind_hint', 'start_hint'} & set(bound))
        self.assertEqual((bound['active']['mode'], bound['active']['label'], bound['active']['project_id']),
                         ('project', self.root.name, self.project))

    async def test_home_initialize_binds_home_from_unrelated_cwd_and_never_rebinds(self):
        from tools.home import last_choice, read_active
        scratch = self.scratch()
        missing, value = await self.call('maven_initialize', {'home': True, 'cwd': str(scratch)})
        self.assertTrue(missing.is_error)
        self.assertIn('not configured', value['error'])
        root, project = self.configure_home()
        (self.home / 'home.json').chmod(0o644)
        invalid, value = await self.call('maven_initialize', {'home': True, 'cwd': str(scratch)})
        self.assertTrue(invalid.is_error)
        self.assertIn('invalid', value['error'])
        (self.home / 'home.json').chmod(0o600)
        mismatch, _ = await self.call('maven_initialize', {'home': True, 'root': str(self.root)})
        self.assertTrue(mismatch.is_error)
        self.assertIsNone(self.app.binding)
        self.assertIsNone(last_choice(self.home, scratch))
        result, ready = await self.call('maven_initialize', {
            'home': True, 'cwd': str(scratch), 'harness': 'omp', 'harness_session': 'harness-1'})
        self.assertFalse(result.is_error, ready)
        self.assertEqual((ready['binding']['root'], ready['binding']['project_id']), (str(root), project))
        self.assertEqual((ready['active']['mode'], ready['active']['label']), ('home', 'nexus'))
        self.assertIn('Home (nexus)', ready['message'])
        self.assertEqual(last_choice(self.home, scratch), 'home')
        self.assertEqual(read_active(self.home, harness_session='harness-1')['mode'], 'home')
        self.assertEqual(read_active(self.home, folder=scratch)['project_id'], project)
        _, moved = await self.call('maven_status', {'cwd': str(self.root)})
        self.assertEqual(moved['binding']['root'], str(root))
        self.assertEqual(moved['active']['mode'], 'home')
        self.assertFalse({'rebind_hint', 'start_hint'} & set(moved))
        other, _ = await self.call('maven_initialize', {'root': str(self.root), 'cwd': str(self.root)})
        self.assertTrue(other.is_error)
        self.assertEqual(self.app.binding['root'], str(root))

    async def test_start_records_read_only_without_any_cloud_call(self):
        from tools.home import last_choice
        scratch = self.scratch()
        constructed, factory = [], self.app.service_factory
        self.app.service_factory = lambda *args, **kwargs: constructed.append(args)
        with patch.object(self.app, 'unbound_client', side_effect=AssertionError('no client')) as client:
            result, value = await self.call('maven_start', {'choice': 'read_only', 'cwd': str(scratch)})
            self.assertFalse(result.is_error, value)
            self.assertEqual((value['status'], value['active']['mode']), ('recorded', 'read_only'))
            _, status = await self.call('maven_status', {'cwd': str(scratch)})
        client.assert_not_called()
        self.assertEqual((constructed, self.cloud.calls), ([], []))
        self.assertIsNone(self.app.binding)
        self.assertEqual(last_choice(self.home, scratch), 'read_only')
        self.assertEqual(status['active']['mode'], 'read_only')
        self.assertEqual(status['start_hint']['last_choice'], 'read_only')
        _, value = await self.call('maven_start', {'choice': 'off', 'cwd': str(scratch)})
        self.assertEqual(value['active']['mode'], 'off')
        self.app.service_factory = factory
        await self.initialize()
        refused, _ = await self.call('maven_start', {'choice': 'read_only', 'cwd': str(scratch)})
        self.assertTrue(refused.is_error)
        self.assertEqual(last_choice(self.home, scratch), 'off')

    async def test_unbound_include_home_consults_only_universal_and_home(self):
        identifiers = {}
        with patch.object(self.app, 'unbound_client', return_value=self.cloud):
            unconfigured, _ = await self.call('maven_search', {'query': 'clients', 'unbound': True, 'include_home': True})
            self.assertTrue(unconfigured.is_error)
            _, home_project = self.configure_home()
            for name, owner in (('universal', None), ('home', home_project), ('project', self.project),
                                ('other', str(uuid.uuid4()))):
                identifier = identifiers[name] = str(uuid.uuid4())
                self.cloud.records[identifier] = {
                    'record_id': identifier, 'kind': 'decision', 'revision': 1, 'project_id': owner,
                    'data': {'title': f'Never name clients ({name})', 'state': 'accepted'}, 'tags': [],
                    'classification': 'Sensitive', 'data_scope': 'owner'}
            _, plain = await self.call('maven_search', {'query': 'clients', 'unbound': True})
            self.assertEqual([item['record_id'] for item in plain['records']], [identifiers['universal']])
            result, found = await self.call('maven_search', {'query': 'clients', 'unbound': True, 'include_home': True})
            self.assertFalse(result.is_error, found)
            self.assertEqual({item['record_id'] for item in found['records']},
                             {identifiers['universal'], identifiers['home']})
            self.assertTrue(found['unbound'])
            _, page = await self.call('maven_search', {'query': 'clients', 'unbound': True, 'include_home': True,
                                                       'limit': 1})
            self.assertEqual(len(page['records']), 1)
            _, listed = await self.call('maven_search', {'unbound': True, 'include_home': True,
                                                         'project_id': home_project})
            self.assertEqual(len(listed['records']), 2)
            _, record = await self.call('maven_read', {'record_id': identifiers['home'], 'unbound': True,
                                                       'include_home': True})
            self.assertEqual((record['project_id'], record['unbound']), (home_project, True))
            _, record = await self.call('maven_read', {'record_id': identifiers['universal'], 'unbound': True,
                                                       'include_home': True})
            self.assertIsNone(record['project_id'])
            for name, arguments in (
                ('maven_read', {'record_id': identifiers['home'], 'unbound': True}),
                ('maven_read', {'record_id': identifiers['other'], 'unbound': True, 'include_home': True}),
                ('maven_read', {'record_id': identifiers['project'], 'unbound': True, 'include_home': True}),
                ('maven_search', {'unbound': True, 'include_home': True, 'project_id': self.project}),
                ('maven_search', {'unbound': True, 'include_home': True, 'scope': 'all'}),
                ('maven_search', {'include_home': True}),
            ):
                denied, _ = await self.call(name, arguments)
                self.assertTrue(denied.is_error, arguments)
        self.assertIsNone(self.app.binding)
        self.assertFalse(any(route == '/v1/memory/write' for route, _ in self.cloud.calls))

    async def test_home_tool_schemas(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertIn('cwd', tools['maven_status'].input_schema['properties'])
        self.assertTrue({'home', 'cwd'} <= set(tools['maven_initialize'].input_schema['properties']))
        self.assertNotIn('root', tools['maven_initialize'].input_schema.get('required', []))
        for name in ('maven_search', 'maven_read'):
            self.assertIn('include_home', tools[name].input_schema['properties'])
        self.assertIn('home', json.dumps(tools['maven_admin'].input_schema['properties']['kind']))
        self.assertEqual(tools['maven_start'].input_schema['properties']['choice']['enum'], ['read_only', 'off'])

    async def test_expired_sign_in_returns_mavens_exact_login_command(self):
        self.home.mkdir(mode=0o700)
        cache = self.home / 'runtime' / 'aws-login-cache'
        (self.home / '.env').write_text(f'MAVEN_AWS_PROFILE="daily"\nMAVEN_AWS_LOGIN_CACHE="{cache}"\n'
                                        'MAVEN_AWS_REGION="us-east-1"\n')
        (self.home / '.env').chmod(0o600)
        expected = f'AWS_LOGIN_CACHE_DIRECTORY={cache} aws login --profile daily --region us-east-1'
        with patch.object(self.app, 'unbound_client',
                          side_effect=MemoryClientError('selected_login_unavailable_or_expired')):
            _, blocked = await self.call('maven_search', {'query': 'firmware', 'unbound': True})
        self.assertEqual((blocked['status'], blocked['error']), ('blocked', 'selected_login_unavailable_or_expired'))
        self.assertEqual(blocked['sign_in']['command'], expected)
        self.assertIn(expected, blocked['message'])
        _, status = await self.call('maven_status')
        self.assertEqual(status['sign_in']['command'], expected)

    async def test_detail_flags_pass_through_and_admin_responses_are_summarized(self):
        await self.initialize()
        operation = str(uuid.uuid4())
        with patch.object(self.app.maintenance, 'status', return_value={'state': 'saved'}) as status:
            await self.call('maven_maintenance', {'action': 'status', 'operation_id': operation, 'detail': True})
        status.assert_called_once_with(operation, detail=True)
        with patch.object(self.app.maintenance, 'resume', return_value={'status': 'resumed'}) as resume:
            await self.call('maven_resume', {'nothing_to_record': True})
        self.assertTrue(resume.call_args.kwargs['nothing_to_record'])
        full = {'status': 'pending', 'choice_checkpoints': [1], 'responses': {}, 'navigation': {},
                'retired_interactions': [], 'pending_interaction': None}

        class Admin:
            def status(self, operation_id):
                return dict(full)

        self.app.admin_factory = lambda home: Admin()
        _, summary = await self.call('maven_admin', {'action': 'status', 'operation_id': operation})
        self.assertEqual(summary, {'status': 'pending', 'pending_interaction': None})
        _, detailed = await self.call('maven_admin', {'action': 'status', 'operation_id': operation, 'detail': True})
        self.assertEqual(detailed, full)

    async def test_admin_accepts_cleanup_kind(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertIn('cleanup', json.dumps(tools['maven_admin'].input_schema['properties']['kind']))
        begun = []

        class Admin:
            def begin(self, kind, parameters):
                begun.append((kind, parameters))
                return {'operation_id': 'op', 'state': 'completed', 'outcome': {'action': 'review'}}

        self.app.admin_factory = lambda home: Admin()
        error, result = await self.call('maven_admin', {'action': 'begin', 'kind': 'cleanup',
                                                        'parameters': {'action': 'review'}})
        self.assertFalse(error.is_error)
        self.assertEqual(begun, [('cleanup', {'action': 'review'})])
        self.assertEqual(result['outcome'], {'action': 'review'})

    async def test_keep_warm_is_opt_in(self):
        with patch.dict('os.environ', {'MAVEN_KEEP_WARM': ''}):
            await self.initialize()
        self.assertIsNone(self.app._warm_thread)
        app = self.application()
        pinged = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = self.cloud.invoke

        def invoke(route, body):
            if route == '/v1/pages/index' and body == {'limit': 1}:
                loop.call_soon_threadsafe(pinged.set)
            return original(route, body)

        with patch.dict('os.environ', {'MAVEN_KEEP_WARM': '1'}), \
                patch.object(mcp_server, 'KEEP_WARM_SECONDS', 0.05), \
                patch.object(self.cloud, 'invoke', side_effect=invoke):
            app.initialize(self.root, session_id=self.app.binding['session_id'], resume=True)
            await asyncio.wait_for(pinged.wait(), 5)
        app.close()
        self.assertTrue(app._warm_stop.is_set())

    async def test_git_configuration_approval_is_exact_and_has_real_source(self):
        await self.initialize()
        subprocess.run(['git', 'init', '-b', 'main', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin', 'https://example.invalid/owner/repo.git'],
                       check=True, capture_output=True)
        configuration = {'remote': 'origin', 'accepted_url': 'https://example.invalid/owner/repo.git', 'branch': 'main'}
        _, review = await self.call('maven_git', {'action': 'configure', 'configuration': configuration})
        self.assertEqual(review['status'], 'approval_required')
        approval = {'approved': True, 'candidate': review['candidate'], 'source': SOURCE}
        rejected, _ = await self.call('maven_git', {'action': 'configure', 'approval': dict(approval, candidate='stale')})
        self.assertTrue(rejected.is_error)
        self.assertNotIn('git_enabled', read_private(self.root, '.maven/project.json'))
        accepted, value = await self.call('maven_git', {'action': 'configure', 'approval': approval})
        self.assertFalse(accepted.is_error, value)
        self.assertEqual(value['project']['project_id'], self.project)
        self.assertEqual(value['project']['accepted_url'], configuration['accepted_url'])
        receipt = read_private(self.root, f'.maven/runtime/sessions/{self.app.service.session_id}/git-config-review.json')
        self.assertEqual(receipt['approval']['source'], SOURCE)

    async def test_shared_fixture_restores_records_across_independent_clients(self):
        directory = self.home / 'shared-fixture'
        primary = fixture_application(SharedMCPCloud(directory), home=self.home)
        restored = fixture_application(SharedMCPCloud(directory), home=self.home)
        self.addCleanup(primary.close)
        self.addCleanup(restored.close)
        binding = primary.initialize(self.root)['binding']
        record = primary.service.save('note', NOTE['data'], source=SOURCE)
        restored.initialize(self.root, session_id=binding['session_id'], resume=True)
        reread = restored.service.read_record(record['record_id'])
        self.assertEqual(reread['revision'], record['revision'])
        self.assertEqual(reread['data'], NOTE['data'])

    async def test_stdio_smoke(self):
        await stdio_smoke(self.root, self.home)

    async def test_task_claim_and_delegated_completion_share_checkpoint_cutoff(self):
        binding = await self.initialize()
        task = self.app.service.save('task', {'title': 'Check', 'description': '', 'state': 'open'}, source=SOURCE)
        operation = str(uuid.uuid4())
        request = {'record_id': task['record_id'], 'action': 'claim', 'expected_revision': 1,
                   'source': SOURCE, 'lease_seconds': 60, 'operation_id': operation}
        claimed, lease = await self.call('maven_claim', request)
        self.assertFalse(claimed.is_error, lease)
        replay, repeated = await self.call('maven_claim', request)
        self.assertFalse(replay.is_error, repeated)
        self.assertEqual(repeated['record'], lease['record'])
        _, queued = await self.call('maven_maintenance', {
            'action': 'enqueue', 'intent': 'Complete verified task', 'source': SOURCE})
        delegated = queued['assignment']['operation_id']
        worker = self.application()
        worker.initialize(self.root, session_id=binding['session_id'], resume=True, assignment_id=delegated)
        server = create_server(worker)
        _, assignment = await self.call('maven_maintenance', {
            'action': 'claim', 'operation_id': delegated, 'worker': 'task-worker'}, server=server)
        token = assignment['assignment']['worker_token']
        _, checkpoint = await self.call('maven_checkpoint', {'summary': 'Task cutoff'})
        self.assertEqual(checkpoint['status'], 'pending')
        applied, value = await self.call('maven_maintenance', {
            'action': 'apply', 'operation_id': delegated, 'worker_token': token,
            'changes': [{'kind': 'task', 'task_action': 'complete', 'record_id': task['record_id'],
                         'expected_revision': lease['record']['revision'], 'scope': 'project',
                         'fence': lease['claim']['fence']}]}, server=server)
        self.assertFalse(applied.is_error, value)
        completed, value = await self.call('maven_maintenance', {
            'action': 'complete', 'operation_id': delegated, 'worker_token': token,
            'summary': 'Task completed with actual lease fence.'}, server=server)
        self.assertFalse(completed.is_error, value)
        finished, value = await self.call('maven_checkpoint', {
            'advance': True, 'checkpoint_id': checkpoint['checkpoint']['checkpoint_id']})
        self.assertFalse(finished.is_error, value)
        self.assertEqual(value['status'], 'checkpointed')
        self.assertEqual(self.cloud.records[task['record_id']]['data']['state'], 'completed')
        self.assertIn(operation, value['checkpoint']['assignments'])
        self.assertIn(delegated, value['checkpoint']['assignments'])

    async def test_interrupted_close_finishes_after_reconnect_without_reopening(self):
        binding = await self.initialize()
        _, checkpoint = await self.call('maven_checkpoint', {'summary': 'Original closing boundary'})
        identifier = checkpoint['checkpoint']['checkpoint_id']
        with patch.object(self.app.service.registry, 'finish_memory', side_effect=OSError('interrupted cleanup')):
            interrupted, _ = await self.call('maven_finish', {'checkpoint_id': identifier})
        self.assertTrue(interrupted.is_error)
        closed = self.cloud.records[binding['session_id']]
        self.assertEqual(closed['data']['state'], 'closed')
        revision = closed['revision']
        restored = self.application()
        server = create_server(restored)
        initialized, _ = await self.call('maven_initialize', {
            'root': str(self.root), 'session_id': binding['session_id'], 'resume': True}, server=server)
        self.assertTrue(initialized.is_error)
        wrong, _ = await self.call('maven_finish', {'checkpoint_id': str(uuid.uuid4())}, server=server)
        self.assertTrue(wrong.is_error)
        saved, _ = await self.call('maven_save', {'change': NOTE, 'source': SOURCE}, server=server)
        self.assertTrue(saved.is_error)
        finished, value = await self.call('maven_finish', {'checkpoint_id': identifier}, server=server)
        self.assertFalse(finished.is_error, value)
        self.assertEqual(value['status'], 'closed')
        self.assertEqual(self.cloud.records[binding['session_id']]['revision'], revision)
        self.assertEqual(read_private(self.root,
            f".maven/runtime/sessions/{binding['session_id']}/finish.json")['status'], 'closed')

    async def test_unready_connection_cannot_start_a_new_closure(self):
        binding = await self.initialize()
        _, checkpoint = await self.call('maven_checkpoint', {'summary': 'Not yet closing'})
        restored = self.application()
        server = create_server(restored)
        with patch.object(self.cloud, 'load_context', side_effect=BoundaryError('authority unavailable')):
            result, _ = await self.call('maven_initialize', {
                'root': str(self.root), 'session_id': binding['session_id'], 'resume': True}, server=server)
        self.assertTrue(result.is_error)
        finished, _ = await self.call('maven_finish', {
            'checkpoint_id': checkpoint['checkpoint']['checkpoint_id']}, server=server)
        self.assertTrue(finished.is_error)
        self.assertEqual(self.cloud.records[binding['session_id']]['data']['state'], 'active')
        self.assertNotIn('closure', read_private(self.root,
            f".maven/runtime/sessions/{binding['session_id']}/maintenance.json"))

    async def test_git_pause_acknowledges_only_exact_quiescent_peer_reservation(self):
        binding = await self.initialize()
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': self.project, 'git_enabled': True,
            'remote': 'origin', 'accepted_url': 'https://example.invalid/owner/repo.git',
            'branch': 'main', 'checks': []})
        self.app.env.write_text('')
        self.app.env.chmod(0o600)
        registry = self.app.service.registry
        registry.use_git(self.root, binding['session_id'])
        peer = str(uuid.uuid4())
        registry.register(self.root, self.project, peer, {})
        registry.activate(self.root, peer)
        reservation = registry.reserve(self.root, peer)['reservation_id']
        await self.call('maven_pause')
        self.assertEqual(registry.inspect(self.root, binding['session_id'])['session']['state'], 'active')
        restored = self.application()
        server = create_server(restored)
        initialized, _ = await self.call('maven_initialize', {
            'root': str(self.root), 'session_id': binding['session_id'], 'resume': True}, server=server)
        self.assertTrue(initialized.is_error)
        for arguments in (
            {'action': 'pause', 'writers_quiescent': True},
            {'action': 'pause', 'reservation_id': reservation},
            {'action': 'pause', 'reservation_id': str(uuid.uuid4()), 'writers_quiescent': True},
        ):
            rejected, _ = await self.call('maven_git', arguments, server=server)
            self.assertTrue(rejected.is_error)
            self.assertEqual(registry.inspect(self.root, binding['session_id'])['session']['state'], 'active')
        paused, value = await self.call('maven_git', {
            'action': 'pause', 'reservation_id': reservation, 'writers_quiescent': True}, server=server)
        self.assertFalse(paused.is_error, value)
        self.assertEqual(registry.quiescent(self.root, peer, reservation)['reservation_id'], reservation)

    async def test_git_worker_cannot_consume_protocol_stdin(self):
        # Isolate stdin manipulation in a real helper process, not the test runner.
        code = """
import asyncio, json, subprocess, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[3])
from test_mcp import MCPCloud, fixture_application
from tools.mcp.server import create_server
original_run = subprocess.run
worker = "import json,sys; print(json.dumps({'status':'ready','stdin_eof':sys.stdin.read(1)==''}))"
def launch(command, **options):
    return original_run([sys.executable, '-c', worker], **options)
async def exercise():
    app = fixture_application(MCPCloud(), home=Path(sys.argv[2]))
    try:
        app.initialize(Path(sys.argv[1]))
        with patch('tools.mcp.server.subprocess.run', side_effect=launch):
            result = await create_server(app).call_tool('maven_git', {'action': 'status'})
        print(json.dumps({'worker': result.structured_content, 'next_request': sys.stdin.readline()}))
    finally:
        app.close()
asyncio.run(exercise())
"""
        request = '{"jsonrpc":"2.0","id":42,"method":"tools/list"}\n'
        result = subprocess.run(
            [sys.executable, '-c', code, str(self.root), str(self.home), str(Path(__file__).resolve().parent)],
            input=request, text=True, capture_output=True, timeout=20, check=True,
            cwd=str(Path(__file__).resolve().parents[1]))
        value = json.loads(result.stdout)
        self.assertTrue(value['worker']['stdin_eof'])
        self.assertEqual(value['next_request'], request)

    async def test_git_recovery_uses_real_parser_and_original_writer_identity(self):
        from tools.git_workflow import parser
        binding = await self.initialize()
        token = str(uuid.uuid4())
        captured = []

        def run(command, **kwargs):
            arguments = parser().parse_args(command[3:])
            captured.append(arguments)
            return subprocess.CompletedProcess(command, 4, json.dumps({'status': 'ready'}), 'sensitive stderr')

        with patch('tools.mcp.server.subprocess.run', side_effect=run):
            result, value = await self.call('maven_git', {
                'action': 'recover', 'writers_quiescent': True, 'writer_tokens': [token]})
        self.assertTrue(result.is_error)
        self.assertEqual(value['status'], 'error')
        self.assertEqual(captured[0].session, binding['session_id'])
        self.assertEqual(captured[0].writer_token, [token])
        self.assertNotIn('sensitive stderr', result.model_dump_json())



async def stdio_smoke(root, home):
    """Exercise the real main/SDK framing, controllers and cloud fixture over pipes."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), '--stdio-fixture', '--home', str(home)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
    async with asyncio.timeout(30):
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                initialized = await client.initialize()
                assert initialized.server_info.name == 'MAVEN'
                listed = await client.list_tools()
                save_tool = next(tool for tool in listed.tools if tool.name == 'maven_save')
                assert 'change' in save_tool.input_schema['properties']

                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.is_error, result
                    return result.structured_content

                bound = await call('maven_initialize', root=str(root))
                assert bound['status'] == 'ready'
                assert bound['memory']['context']['instructions']
                saved = await call('maven_save', change=NOTE, source=SOURCE)
                assert saved['status'] == 'saved' and saved['cloud_saved']
                await call('maven_pause')
                paused = await call('maven_maintenance', action='enqueue', intent='Do not save paused discussion', source=SOURCE)
                assert paused['status'] == 'paused' and not paused['accepted'] and not paused['cloud_saved']
                checkpoint = await call('maven_checkpoint', summary='Verified stdio fixture boundary')
                assert checkpoint['status'] == 'checkpointed' and checkpoint['cloud_saved']
                closed = await call('maven_finish', checkpoint_id=checkpoint['checkpoint']['checkpoint_id'])
                assert closed['status'] == 'closed'


if __name__ == '__main__':
    if '--stdio-fixture' in sys.argv or '--serve-fixture' in sys.argv:
        serve_fixture([argument for argument in sys.argv[1:]
                       if argument not in ('--stdio-fixture', '--serve-fixture')])
    else:
        unittest.main()
