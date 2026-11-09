"""Schema cutover and memory contract regressions using existing offline fixtures."""
import json
import unittest
from unittest.mock import Mock, patch

from tools.memory import admin, bootstrap, client, store, validation

CONFIG = {
    'cluster_arn': 'arn:aws:rds:us-east-1:123456789012:cluster:fixture',
    'app_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:app',
    'admin_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:admin',
    'database': 'maven', 'journal_bucket': 'fixture-memory-test', 'region': 'us-east-1',
    'instance_id': '10000000-0000-4000-8000-000000000001',
    'recovery_epoch': '10000000-0000-4000-8000-000000000002',
    'client_role_arn': 'arn:aws:iam::123456789012:role/memory-client',
    'admin_role_arn': 'arn:aws:iam::123456789012:role/memory-admin',
    'approval_role_arn': 'arn:aws:iam::123456789012:role/memory-approval',
    'endpoint': 'https://fixture123.execute-api.us-east-1.amazonaws.com', 'schema_version': 2,
}


class CloudContractTests(unittest.TestCase):
    def config(self, version=2):
        return {**CONFIG, 'endpoint': 'https://fixture123.execute-api.us-east-1.amazonaws.com',
                'schema_version': version}

    def test_connection_review_fences_all_locators_before_config_mutation(self):
        cloud = Mock()
        cloud.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE'}]}
        changed = {**self.config(), 'journal_bucket': 'different-reviewed-bucket'}
        with patch.object(bootstrap, '_outputs', return_value=changed), \
                patch.object(bootstrap, 'load_environment') as load, \
                patch.object(bootstrap.tempfile, 'mkstemp') as create:
            with self.assertRaises(client.MemoryClientError) as error:
                bootstrap.connect({'MAVEN_STACK_NAME': 'fixture'}, '/unused/environment',
                                  confirm_instance=CONFIG['instance_id'], cloud=cloud,
                                  reviewed_config=self.config())
        self.assertEqual(error.exception.code, 'reviewed_connection_changed')
        load.assert_not_called()
        create.assert_not_called()

    def test_ordinary_client_refuses_old_schema_before_transport(self):
        with self.assertRaises(client.MemoryClientError) as error:
            client.MemoryClient(self.config(1), Mock())
        self.assertEqual(error.exception.code, 'invalid_configuration')
        self.assertEqual(client.validate_config(self.config())['schema_version'], 2)

    def test_reviewed_upgrade_consumes_old_binding_without_rewriting_it(self):
        values = {'MAVEN_MEMORY_SCHEMA_VERSION': '1'}
        session = Mock()
        memory = Mock()
        memory.load_context.return_value = {'data': {'authority_generation': 4, 'minimum_client_protocol': 3}}
        with patch('tools.memory.environment.memory_config', return_value=self.config()) as load, \
                patch.object(bootstrap, 'selected_session', return_value=session), \
                patch.object(bootstrap, 'MemoryClient', return_value=memory), \
                patch.object(admin, 'migrate', return_value={'status': 'ready', 'schema_version': 2}) as migrate:
            result = bootstrap.initialize(values, confirm_instance=CONFIG['instance_id'], upgrade=True)
        self.assertEqual(load.call_args.args[0]['MAVEN_MEMORY_SCHEMA_VERSION'], '2')
        self.assertEqual(values['MAVEN_MEMORY_SCHEMA_VERSION'], '1')
        migrate.assert_called_once_with(self.config(), session)
        self.assertEqual(result['minimum_client_protocol'], 3)
        memory.close.assert_called_once()

    def test_old_control_is_accepted_only_for_explicit_upgrade(self):
        old = {**store.control_document(CONFIG, 'maintenance'), 'schema_version': 1}
        with self.assertRaises(validation.MemoryError):
            store.parse_control(json.dumps(old), CONFIG, allow_maintenance=True)
        self.assertEqual(store.parse_control(json.dumps(old), CONFIG, allow_maintenance=True,
                                             allow_legacy=True), old)

    def test_ordered_schema_one_upgrade_preserves_secret_and_registers_new_bytes(self):
        database = Mock()
        database.transaction.return_value.__enter__ = Mock(return_value='tx')
        database.transaction.return_value.__exit__ = Mock(return_value=False)
        bundle = admin.migrations()
        installed = {name: digest for name, digest, _ in bundle[:-1]}
        old = {**store.control_document(CONFIG, 'ready'), 'schema_version': 1,
               'app_secret_arn': CONFIG['app_secret_arn'], 'app_secret_version': 'fixture-version'}
        database.scalar.side_effect = [True, True, json.dumps(installed), json.dumps(old)]
        secrets = Mock()
        admin._install(database, CONFIG, secrets, store.control_document(CONFIG, 'maintenance'), upgrade=True)
        secrets.get_secret_value.assert_not_called()
        name, digest, statements = bundle[-1]
        self.assertEqual(name, '012_agentic_memory.sql')
        registrations = [call for call in database.execute.call_args_list
                         if call.args[0].startswith('INSERT INTO maven.schema_migrations')]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0].args[1], {'name': name, 'sha': digest})
        for statement in statements:
            database.execute.assert_any_call(statement, transaction='tx')

    def test_explicit_scope_is_transported_without_rewriting_legacy_discovery(self):
        memory = client.MemoryClient(self.config(), Mock())
        memory._post = Mock(return_value=(200, {'status': 'ready', 'data': {'records': []}}))
        try:
            for request in ({'kind': 'entity'}, {'scope': 'all'}, {'scope': 'universal'},
                            {'scope': 'context', 'project_id': CONFIG['instance_id']}):
                memory.invoke('/v1/memory/search', request)
                self.assertEqual(memory._post.call_args.args[1], request)
        finally:
            memory.close()

    def test_reference_refusal_is_not_a_success_or_blind_retry(self):
        memory = client.MemoryClient(self.config(), Mock())
        key = CONFIG['instance_id']
        memory._post = Mock(return_value=(404, {'status': 'reference_unavailable'}))
        try:
            with self.assertRaises(client.MemoryClientError) as error:
                memory.invoke('/v1/memory/write', {'request_key': key})
            self.assertEqual(error.exception.code, 'reference_unavailable')
            self.assertEqual(error.exception.request_key, key)
            memory._post.assert_called_once()
        finally:
            memory.close()


if __name__ == '__main__':
    unittest.main()
