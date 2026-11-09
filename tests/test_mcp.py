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

from tools.coordination import private_lock
from tools.mcp.server import MavenApplication, create_server, main
from tools.memory.boundary import BoundaryError, read_private, save_private
from tools.memory.lifecycle import SessionService
from test_workflow import MemoryCloud


SOURCE = {'origin': 'Owner conversation', 'excerpt': 'Preserve the verified finding.'}
NOTE = {'kind': 'note', 'data': {'title': 'Finding', 'body_markdown': 'Verified locally.', 'category': 'work'}}


class MCPCloud(MemoryCloud):
    def invoke(self, route, body):
        if route == '/v1/pages/index':
            return {'status': 'ready', 'data': {'pages': [], 'authority_generation': 1, 'next_after_slug': None}}
        return super().invoke(route, body)


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
