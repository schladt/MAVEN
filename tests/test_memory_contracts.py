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
        names = [name for name, _, _ in bundle]
        first_new = names.index('012_agentic_memory.sql')
        self.assertEqual(names[first_new:], ['012_agentic_memory.sql', '013_performance.sql', '014_authority_guard.sql',
                                             '015_home_context.sql', '016_cleanup.sql'])
        installed = {name: digest for name, digest, _ in bundle[:first_new]}
        old = {**store.control_document(CONFIG, 'ready'), 'schema_version': 1,
               'app_secret_arn': CONFIG['app_secret_arn'], 'app_secret_version': 'fixture-version'}
        database.scalar.side_effect = [True, True, json.dumps(installed), json.dumps(old)]
        secrets = Mock()
        admin._install(database, CONFIG, secrets, store.control_document(CONFIG, 'maintenance'), upgrade=True)
        secrets.get_secret_value.assert_not_called()
        registrations = [call for call in database.execute.call_args_list
                         if call.args[0].startswith('INSERT INTO maven.schema_migrations')]
        self.assertEqual([call.args[1] for call in registrations],
                         [{'name': name, 'sha': digest} for name, digest, _ in bundle[first_new:]])
        for _, _, statements in bundle[first_new:]:
            for statement in statements:
                database.execute.assert_any_call(statement, transaction='tx')
        executed = [call.args[0] for call in database.execute.call_args_list]
        self.assertTrue(any('maven.memory_request(text,jsonb,text,text,text,integer) TO maven_app' in sql for sql in executed))
        self.assertIn("ALTER ROLE maven_app SET statement_timeout = '5s'", executed)
        self.assertIn("ALTER ROLE maven_app SET lock_timeout = '1s'", executed)

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


class CleanupAdminTests(unittest.TestCase):
    DIGEST = 'a' * 64
    RECORD = '20000000-0000-4000-8000-000000000001'

    def run_cleanup(self, call, result, error=None):
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {
            'Arn': 'arn:aws:sts::123456789012:assumed-role/memory-admin/owner'}
        database = Mock()
        database.transaction.return_value.__enter__ = Mock(return_value='tx')
        database.transaction.return_value.__exit__ = Mock(return_value=False)
        database.json_scalar.side_effect = error if error else [json.dumps(result)]
        state = {**store.control_document(CONFIG, 'ready'), 'app_secret_arn': CONFIG['app_secret_arn'],
                 'app_secret_version': 'fixture-version'}
        with patch.object(admin, 'DataAPI', return_value=database) as adapter, \
                patch.object(admin, '_check_logging') as logging_check, \
                patch.object(admin, '_state', return_value=state):
            value = call(session)
        self.assertTrue(adapter.call_args.kwargs['admin'])
        logging_check.assert_called_once_with(database)
        return value, database.json_scalar.call_args

    def test_each_operation_is_one_admin_statement_with_bound_parameters(self):
        value, call = self.run_cleanup(lambda session: admin.cleanup_candidates(CONFIG, session),
                                       {'candidates': [], 'truncated': False})
        self.assertEqual(value, {'candidates': [], 'truncated': False})
        self.assertEqual(call.args[0], 'maven.cleanup_candidates()')
        value, call = self.run_cleanup(lambda session: admin.cleanup_erased_ids(CONFIG, session),
                                       {'record_ids': [self.RECORD]})
        self.assertEqual(value, [self.RECORD])
        value, call = self.run_cleanup(lambda session: admin.cleanup_describe(CONFIG, session, [self.RECORD]),
                                       [{'record_id': self.RECORD, 'found': False}])
        self.assertEqual(value, [{'record_id': self.RECORD, 'found': False}])
        self.assertEqual(call.args[1], {'ids': json.dumps([self.RECORD])})
        _, call = self.run_cleanup(lambda session: admin.forget_records(
            CONFIG, session, [self.RECORD], restore=True, actor='owner', reason='Restore', approval_digest=self.DIGEST),
            {'receipt_id': self.RECORD, 'changed': [self.RECORD], 'unchanged': []})
        self.assertTrue(call.args[0].startswith('maven.forget_records('))
        self.assertEqual(call.args[1], {'ids': json.dumps([self.RECORD]), 'restore': True, 'actor': 'owner',
                                        'reason': 'Restore', 'digest': self.DIGEST})
        _, call = self.run_cleanup(lambda session: admin.erase_records(
            CONFIG, session, [self.RECORD], actor='owner', reason='Erase', approval_digest=self.DIGEST),
            {'receipt_id': self.RECORD, 'records': []})
        self.assertTrue(call.args[0].startswith('maven.erase_records('))
        self.assertEqual(call.args[2], 'tx')

    def test_refusals_fail_before_or_from_sql_without_values(self):
        for record_ids, digest in (([], self.DIGEST), ([self.RECORD] * 201, self.DIGEST), ([self.RECORD], 'A' * 64)):
            with self.assertRaises(validation.MemoryError) as error:
                admin.erase_records(CONFIG, Mock(), record_ids, actor='owner', reason='Erase', approval_digest=digest)
            self.assertEqual(error.exception.status, 'cleanup_refused')
        refusal = Exception()
        refusal.response = {'Error': {'Code': 'DatabaseErrorException',
                                      'Message': 'ERROR: cleanup_refused:lifecycle_kind'}}
        with self.assertRaises(validation.MemoryError) as error:
            self.run_cleanup(lambda session: admin.forget_records(
                CONFIG, session, [self.RECORD], actor='owner', reason='Forget', approval_digest=self.DIGEST),
                None, refusal)
        self.assertEqual((error.exception.status, error.exception.http_status, error.exception.metadata),
                         ('cleanup_refused', 409, {'detail': 'lifecycle_kind'}))


if __name__ == '__main__':
    unittest.main()
