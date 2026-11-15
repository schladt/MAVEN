"""Exercise the actual MCP boundary without credentials or a cloud service."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.mcp.server import MavenApplication, create_server
from test_secrets_store import CatalogCloud, HOME_ID, INSTANCE_ID, Session, SecretStore, VALUES, SOURCE


class Cloud(CatalogCloud):
    def close(self):
        pass


class SecretMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / 'home'
        self.home.mkdir(mode=0o700)
        self.workspace = Path(self.directory.name) / 'ordinary-folder'
        self.workspace.mkdir()
        self.input = self.home / 'private-input'
        self.input.write_bytes(b'MCP-SYNTHETIC-PRIVATE-VALUE')
        self.input.chmod(0o600)
        self.cloud = Cloud()
        self.session = Session()
        self.store = SecretStore(VALUES, self.session)
        for target, value in (
            ('tools.secret_catalog.load_environment', VALUES),
            ('tools.secret_catalog.load_home', {'project_id': HOME_ID, 'instance_id': INSTANCE_ID}),
            ('tools.secret_catalog.SecretStore', self.store),
            ('tools.mcp.secrets.load_environment', VALUES),
            ('tools.mcp.secrets.SecretStore', self.store),
        ):
            replacement = patch(target, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = MavenApplication(home=self.home)
        self.app._unbound_client = self.cloud
        self.addCleanup(self.app.close)
        self.server = create_server(self.app)

    async def call(self, tool, **arguments):
        result = await self.server.call_tool(tool, arguments)
        self.assertIsInstance(result.structured_content, dict)
        self.assertNotIn('MCP-SYNTHETIC-PRIVATE-VALUE', result.model_dump_json())
        return result, result.structured_content

    async def test_explicit_read_only_command_catalogs_one_secret_without_a_session(self):
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        result, stored = await self.call('maven_secrets', action='store_and_catalog', name='owner.api',
                                       source_path=str(self.input), source=SOURCE)
        self.assertFalse(result.is_error, stored)
        self.assertEqual(stored['state'], 'completed')
        _, record = await self.call('maven_secrets', action='lookup', name='owner.api')
        self.assertEqual(record['record']['data']['key'], 'secret:owner.api')
        self.assertEqual({item['kind'] for item in self.cloud.records.values()}, {'setting'})
        self.assertEqual({item['project_id'] for item in self.cloud.records.values()}, {HOME_ID})
        self.assertIsNone(self.app.binding)
        self.assertFalse((self.workspace / '.maven').exists())
        _, listed = await self.call('maven_secrets', action='list', domain='owner')
        self.assertEqual([item['name'] for item in listed['items']], ['owner.api'])

    async def test_off_and_unconfigured_refuse_store_lookup_and_adapters(self):
        for mode in (None, 'off'):
            if mode:
                await self.call('maven_start', choice=mode, cwd=str(self.workspace))
            for tool, arguments in (
                ('maven_secrets', {'action': 'list'}),
                ('maven_secrets', {'action': 'lookup', 'name': 'owner.api'}),
                ('maven_secrets', {'action': 'store_and_catalog', 'name': 'owner.api',
                                   'source_path': str(self.input), 'source': SOURCE}),
                ('maven_secret_adapter', {'action': 'policy', 'parameters': {'slug': 'secret-adapter-demo'}}),
            ):
                with self.subTest(mode=mode, tool=tool, action=arguments['action']):
                    result, _ = await self.call(tool, **arguments)
                    self.assertTrue(result.is_error)
        self.assertFalse(self.session.parameters.versions)
        self.assertFalse(self.cloud.records)

    async def test_pending_catalog_is_not_reported_as_saved_and_can_retry_from_read_only(self):
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        self.cloud.fail_before = 'temporarily_unavailable'
        _, pending = await self.call('maven_secrets', action='store_and_catalog', name='owner.api',
                                     source_path=str(self.input), source=SOURCE)
        self.assertEqual((pending['storage'], pending['catalog']), ('stored', 'pending'))
        _, completed = await self.call('maven_secrets', action='retry_catalog', operation_id=pending['operation_id'])
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 1)
        self.assertIsNone(self.app.binding)


if __name__ == '__main__':
    unittest.main()
