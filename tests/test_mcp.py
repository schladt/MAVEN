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
                state = await call(client, 'maven_orient', {'cwd': str(child)})
                write = await call(client, 'maven_local_write', {**selected, 'expected_token': state['token'],
                    'kind': 'brief', 'data': {'purpose': 'Measure verified offline progress.'}, 'reason': 'Owner scope'})
                self.assertEqual(write['id'], state['brief_id'])
                await call(client, 'maven_local_write', {**selected, 'expected_token': state['token'], 'kind': 'brief',
                    'data': {'purpose': 'Stale concurrent change'}, 'reason': 'Concurrent edit'}, ok=False)
            # A fresh transport knows nothing about its predecessor. No wrap-up or cloud credentials.
            async with stdio_client(params) as (rx, tx), ClientSession(rx, tx) as client:
                await client.initialize()
                state = await call(client, 'maven_orient', {'cwd': str(root)})
                self.assertEqual(state['brief']['data']['purpose'], 'Measure verified offline progress.')
                self.assertFalse(state['cloud_checked'])
                other = {**selected, 'project_id': str(uuid4()), 'expected_token': state['token']}
                await call(client, 'maven_local_write', {**other, 'kind': 'brief', 'data': {'purpose': 'wrong scope'},
                           'reason': 'Wrong selected project'}, ok=False)
                await call(client, 'maven_reconcile', {**selected, 'action': 'verify'}, ok=False)
                again = await call(client, 'maven_orient', {'cwd': str(root)})
                self.assertEqual(again['brief'], state['brief'])
