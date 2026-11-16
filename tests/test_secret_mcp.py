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

    async def test_adapter_draft_distinguishes_rejection_from_unknown_outcome(self):
        from tools.memory.client import MemoryClientError
        from test_secret_adapters import definition
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        parameters = {'slug': 'secret-adapter-fixture', 'title': 'Fixture adapter',
                      'document': definition(), 'owner_source': SOURCE}
        original = None
        for code, status in (('database_statement_rejected', 'failed'), ('outcome_unknown', 'outcome_unknown')):
            with self.subTest(code=code), patch.object(self.cloud, 'invoke', side_effect=MemoryClientError(code)) as invoke:
                response, result = await self.call('maven_secret_adapter', action='draft', parameters=parameters)
                self.assertEqual(response.is_error, status == 'failed', result)
                self.assertEqual(result['status'], status)
                if status == 'failed':
                    self.assertEqual(result['error'], code)
                original = original or result['request']
                self.assertEqual(result['request'], original)
                self.assertEqual(result['request_key'], original['request_key'])
                invoke.assert_called_once()
        self.assertIsNone(self.app.binding)
        self.assertFalse(self.cloud.records)
        self.assertFalse(self.session.parameters.versions)

    async def test_consumed_and_unauthorized_grants_have_specific_safe_mcp_errors(self):
        from tools.memory.client import MemoryClientError
        from tools.memory.adapter_contracts import POLICY_ROUTE, CONSUME_ROUTE
        from test_secret_adapters import invocation, policy, ready
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        request = {**invocation(), 'home_id': HOME_ID, 'store_id': self.store.store_id,
                   'secret_ref': {'provider': 'ssm', 'name': '/synthetic/secrets/owner.api:1'}}
        for code, expected in (('claim_conflict', 'invocation_consumed'),
                               ('not_authorized', 'adapter_not_authorized')):
            with self.subTest(code=code):
                def invoke(route, body):
                    if route == POLICY_ROUTE:
                        return ready({**policy(), 'home_id': HOME_ID})
                    self.assertEqual(route, CONSUME_ROUTE)
                    raise MemoryClientError(code, detail='MCP-SYNTHETIC-PRIVATE-VALUE')
                with patch.object(self.cloud, 'invoke', side_effect=invoke), \
                        patch.object(self.store, 'read_secret') as read_secret, \
                        patch('tools.secret_adapters.run_adapter') as run:
                    _, result = await self.call('maven_secret_adapter', action='execute', parameters={
                        'invocation': request, 'grant': 'secret-grant-fixture', 'grant_revision': 1})
                self.assertEqual((result['status'], result['error']), ('blocked', expected))
                self.assertIn('next_action', result)
                read_secret.assert_not_called()
                run.assert_not_called()

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

    async def test_reference_corrections_are_returned_before_spending_a_grant(self):
        from test_secret_adapters import invocation
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        for reference, error in (
                ({'provider': 'ssm', 'name': '/synthetic/secrets/owner.api'}, 'secret_reference_migration_required'),
                ({'provider': 'secretsmanager', 'name': 'legacy/secret'}, 'unsupported_secret_provider')):
            request = {**invocation(), 'home_id': HOME_ID, 'store_id': self.store.store_id, 'secret_ref': reference}
            with self.subTest(error=error), patch.object(self.cloud, 'invoke') as invoke, \
                    patch.object(self.store, 'read_secret') as read:
                _, result = await self.call('maven_secret_adapter', action='execute', parameters={
                    'invocation': request, 'grant': 'secret-grant-fixture', 'grant_revision': 1})
            self.assertEqual((result['status'], result['error']), ('blocked', error))
            self.assertIn('catalog', result['next_action'])
            invoke.assert_not_called()
            read.assert_not_called()

    async def test_conflict_resolution_uses_reviewed_revision_without_reupload(self):
        await self.call('maven_start', choice='read_only', cwd=str(self.workspace))
        await self.call('maven_secrets', action='store_and_catalog', name='owner.api',
                        source_path=str(self.input), source=SOURCE)
        self.cloud.fail_before = 'temporarily_unavailable'
        _, pending = await self.call('maven_secrets', action='store_and_catalog', name='owner.api',
                                     source_path=str(self.input), source=SOURCE, replace=True)
        self.cloud.records['owner.api']['revision'] += 1
        _, conflict = await self.call('maven_secrets', action='retry_catalog', operation_id=pending['operation_id'])
        self.assertEqual(conflict['state'], 'catalog_conflict')
        with patch.object(self.store, 'put') as put:
            _, result = await self.call('maven_secrets', action='resolve_catalog', operation_id=pending['operation_id'],
                expected_revision=conflict['conflict']['expected_revision'], source=SOURCE)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['storage_receipt'], pending['storage_receipt'])
        put.assert_not_called()
        self.assertIsNone(self.app.binding)


if __name__ == '__main__':
    unittest.main()
