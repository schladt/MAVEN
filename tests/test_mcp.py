"""Exercise the installed-protocol surface with actual stdio subprocesses and no cloud config."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tools.memory.boundary import save_private


class OfflineStdioTests(unittest.IsolatedAsyncioTestCase):
    def test_switching_nested_project_never_selects_parent_during_rename(self):
        from tools.project import ProjectError
        from tools.project_switch import barrier_path
        from tools.reconcile import select_project
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home, nested = root / 'installation', root / 'nested'
            home.mkdir()
            nested.mkdir()
            save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root),
                         'project_id': str(uuid4()), 'recovery_epoch': str(uuid4()), 'instance_id': str(uuid4())})
            save_private(home, barrier_path(home, nested).relative_to(home), {'reviewed': True})
            with self.assertRaisesRegex(ProjectError, 'switch in progress'):
                select_project(nested, home)

    async def test_unexpected_tool_failure_returns_a_private_read_only_way_forward(self):
        from unittest.mock import patch
        from tools.mcp.server import create_server
        with patch('tools.mcp.server.consult', side_effect=RuntimeError('private argument')):
            result = await create_server().call_tool('maven_context', {'cwd': '/'})
        self.assertTrue(result.is_error)
        visible = json.loads(result.content[0].text)
        self.assertNotIn('private argument', result.content[0].text)
        self.assertIn('without making changes', visible['next_action'])
        self.assertEqual(visible, result.structured_content)

    async def test_missing_preserved_archive_offers_recovery_without_submitting_a_write(self):
        from unittest.mock import Mock
        from tools.mcp.server import MavenApplication, create_server
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, root = base / 'installation', base / 'enabled'
            for folder in (home, root):
                folder.mkdir(mode=0o700)
            identity = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4())}
            project, operation = str(uuid4()), str(uuid4())
            binding = {**identity, 'root': str(root), 'project_id': project, 'schema': 2}
            save_private(root, '.maven/project.json', binding)
            archive = base / ('.maven-preserved-' + operation)
            candidate = {'instance_id': identity['instance_id'], 'old_project': binding, 'root': str(root),
                         'operation_id': operation, 'preservation_path': str(archive)}
            save_private(home, f'runtime/project-switches/{operation}.json',
                         {'phase': 'completed', 'intent': {'candidate': candidate}})
            app = MavenApplication(home=home)
            client = Mock(config=identity)
            client.invoke.return_value = {'status': 'ready', 'data': {
                'total': 0, 'token': 'a'*64, 'targets': [], 'more': False}}
            app._unbound_client = client
            result = await create_server(app).call_tool('maven_reconcile', {
                'cwd': str(root), 'project_id': project, 'action': 'inspect'})
            self.assertTrue(result.is_error)
            self.assertIn(archive.name, result.structured_content['error'])
            recovery = result.structured_content['next_action']
            self.assertIn('original preserved directory', recovery)
            self.assertIn('another installation', recovery)
            self.assertEqual(json.loads(result.content[0].text)['next_action'], recovery)
            self.assertEqual([c.args[0] for c in client.invoke.call_args_list], ['/v1/reconcile/markers'])
            self.assertFalse(archive.exists())

    async def test_independent_harnesses_see_durable_local_knowledge_without_login_or_exit_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, home, ordinary = base / 'enabled', base / 'installation', base / 'ordinary'
            for path in (root, home, ordinary):
                path.mkdir(mode=0o700)
            child = root / 'nested'
            child.mkdir()
            project = str(uuid4())
            save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root), 'project_id': project,
                         'recovery_epoch': str(uuid4()), 'instance_id': str(uuid4())})
            # Isolate imports from the user's installation, including when the test runner is not in a venv.
            launch = ('import sys,json; sys.path[:]=json.loads(sys.argv.pop(1)); '
                      'from tools.mcp.server import main; main()')
            params = StdioServerParameters(command=sys.executable, args=['-I', '-c', launch,
                               json.dumps([str(Path(p).resolve()) for p in sys.path]), '--home', str(home)])
            async def call(client, name, args, *, ok=True):
                result = await client.call_tool(name, args)
                self.assertEqual(not result.is_error, ok, result.content)
                return result.structured_content
            selected = {'cwd': str(child), 'project_id': project}
            async with stdio_client(params) as (rx, tx), ClientSession(rx, tx) as client:
                await client.initialize()
                self.assertEqual((await call(client, 'maven_orient', {'cwd': str(ordinary)}))['status'], 'not_enabled')
                self.assertFalse((ordinary / '.maven').exists())
                context = await call(client, 'maven_context', {'cwd': str(child)})
                self.assertEqual(context['status'], 'partial')  # No configured Nexus.
                self.assertIsNone(context['nexus'])
                self.assertIn('Nexus Home', context['next_action'])
                invalid = await call(client, 'maven_reconcile', {'action': 'PRIVATE-INVALID-VALUE'}, ok=False)
                self.assertIn('argument', invalid['next_action'])
                self.assertNotIn('PRIVATE-INVALID-VALUE', json.dumps(invalid))
                self.assertNotIn('turn_key', context)
                state = context['active']
                write = await call(client, 'maven_local_write', {**selected, 'expected_token': state['token'],
                    'kind': 'brief', 'data': {'purpose': 'Measure verified offline progress.'}, 'reason': 'Owner scope'})
                self.assertEqual(write['id'], state['brief_id'])
                await call(client, 'maven_local_write', {**selected, 'expected_token': state['token'], 'kind': 'brief',
                    'data': {'purpose': 'Stale concurrent change'}, 'reason': 'Concurrent edit'}, ok=False)
            # A fresh transport knows nothing about its predecessor. No wrap-up or cloud credentials.
            async with stdio_client(params) as (rx, tx), ClientSession(rx, tx) as client:
                await client.initialize()
                context = await call(client, 'maven_context', {'cwd': str(root)})
                state = context['active']
                self.assertEqual(state['brief']['data']['purpose'], 'Measure verified offline progress.')
                self.assertFalse(context['cloud_checked'])
                other = {**selected, 'project_id': str(uuid4()), 'expected_token': state['token']}
                await call(client, 'maven_local_write', {**other, 'kind': 'brief', 'data': {'purpose': 'wrong scope'},
                           'reason': 'Wrong selected project'}, ok=False)
                await call(client, 'maven_reconcile', {**selected, 'action': 'verify'}, ok=False)
                change = await call(client, 'maven_local_write', {**selected, 'expected_token': state['token'],
                    'kind': 'outcome', 'data': {'title': 'Adjusted fixture files', 'occurred_on': '2026-11-27',
                    'observed': 'The generated files matched the expected result.'}, 'reason': 'Verified'})
                self.assertTrue(change['changed'])
                bad = await call(client, 'maven_local_write', {**selected, 'expected_token': change['token'],
                    'kind': 'setting', 'data': {'category': 'preference', 'summary': 'Unscoped'}, 'reason': 'Bad'},
                    ok=False)
                self.assertIn('preference', bad['error'])
                self.assertNotIn('changed', bad)
                oversized = await call(client, 'maven_local_write', {**selected, 'expected_token': change['token'],
                    'kind': 'reference', 'data': {'name': 'Test', 'read_when': 'At review', 'state': 'active',
                    'as_of': '2026-11-27', 'body': 'x' * 7001}, 'reason': 'Check rejection'}, ok=False)
                for detail in ('body', '7000', '7001'):
                    self.assertIn(detail, oversized['error'])
                self.assertNotIn('x' * 16, json.dumps(oversized))
                again = (await call(client, 'maven_context', {'cwd': str(child)}))['active']
                self.assertEqual(again['token'], change['token'])
                self.assertEqual(again['brief'], state['brief'])
                inventory = await call(client, 'maven_local_read', {**selected, 'kind': 'outcome'})
                self.assertEqual([i['id'] for i in inventory['items']], [change['id']])
                corrected = await call(client, 'maven_local_write', {**selected, 'expected_token': change['token'],
                    'id': change['id'], 'kind': 'outcome', 'data': {'title': 'Fixture reproduced',
                    'occurred_on': '2026-11-27', 'observed': 'A second run matched.'}, 'reason': 'Repeat run'})
                self.assertTrue(corrected['changed'])
                inventory = await call(client, 'maven_local_read', {**selected, 'kind': 'outcome'})
                exact = {**selected, 'expected_token': inventory['token'], 'ids': [change['id']]}
                current, = (await call(client, 'maven_local_read', exact))['records']
                self.assertNotIn('history', current)
                self.assertEqual(current['record']['data']['observed'], 'A second run matched.')
                full, = (await call(client, 'maven_local_read', {**exact, 'include_history': True}))['records']
                self.assertEqual(full['history'][0]['record']['data']['title'], 'Adjusted fixture files')
                self.assertFalse((home / 'runtime' / 'turns').exists())
                self.assertNotIn('maven_turn', [tool.name for tool in (await client.list_tools()).tools])
