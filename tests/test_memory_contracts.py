"""Schema cutover and memory contract regressions using existing offline fixtures."""
import json
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

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
        memory.config = self.config()
        identity = {key: CONFIG[key] for key in ('instance_id', 'recovery_epoch')}
        memory.invoke.return_value = {'status': 'ready', 'data': {**identity, 'capability': 'project_reconcile_v1'}}
        with patch('tools.memory.environment.memory_config', return_value=self.config()) as load, \
                patch.object(bootstrap, 'selected_session', return_value=session), \
                patch.object(bootstrap, 'MemoryClient', return_value=memory), \
                patch.object(admin, 'migrate', return_value={'status': 'ready', 'schema_version': 2}) as migrate:
            result = bootstrap.initialize(values, confirm_instance=CONFIG['instance_id'], upgrade=True)
        self.assertEqual(load.call_args.args[0]['MAVEN_MEMORY_SCHEMA_VERSION'], '2')
        self.assertEqual(values['MAVEN_MEMORY_SCHEMA_VERSION'], '1')
        migrate.assert_called_once_with(self.config(), session)
        self.assertEqual(result['capability'], 'project_reconcile_v1')
        memory.invoke.assert_called_once_with('/v1/reconcile/status', identity)
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
                                             '015_home_context.sql', '016_cleanup.sql', '017_project_archive_visibility.sql',
                                             '018_skill_discovery.sql', '019_secret_catalog.sql',
                                             '020_secret_adapter_grants.sql', '021_secret_adapter_scope_guard.sql',
                                             '022_reviewed_cleanup.sql', '023_retired_work_visibility.sql',
                                             '024_immutable_secret_consumption.sql', '025_project_reconciliation.sql',
                                             '026_reviewed_redaction.sql', '027_redaction_delivery.sql', '028_redaction_outbox.sql',
                                             '029_narrow_erase_restore.sql', '030_reconcile_taxonomy.sql'])
        installed = {name: digest for name, digest, _ in bundle[:first_new]}
        old = {**store.control_document(CONFIG, 'ready'), 'schema_version': 1,
               'app_secret_arn': CONFIG['app_secret_arn'], 'app_secret_version': 'fixture-version'}
        database.scalar.side_effect = [True, True, json.dumps(installed), json.dumps(old)]
        secrets = Mock()
        admin._install(database, CONFIG, secrets, store.control_document(CONFIG, 'maintenance'), ledger_gate=Mock(), upgrade=True)
        secrets.get_secret_value.assert_not_called()
        registrations = [call for call in database.execute.call_args_list
                         if call.args[0].startswith('INSERT INTO maven.schema_migrations')]
        self.assertEqual([call.args[1] for call in registrations],
                         [{'name': name, 'sha': digest} for name, digest, _ in bundle[first_new:]])
        for _, _, statements in bundle[first_new:]:
            for statement in statements:
                database.execute.assert_any_call(statement, transaction='tx')
        executed = [call.args[0] for call in database.execute.call_args_list]
        grants = '\n'.join(sql for sql in executed if sql.startswith('GRANT EXECUTE ON FUNCTION'))
        self.assertIn('maven.memory_request(text,jsonb,text,text,text,integer)', grants)
        self.assertIn('maven.reconcile_request(text,jsonb)', grants)
        self.assertIn('maven.reconcile_taxonomy(jsonb)', grants)
        revocations = '\n'.join(sql for sql in executed if sql.startswith('REVOKE EXECUTE') and 'FROM maven_app' in sql)
        for obsolete in ('maven.memory_request_before_reconcile(text,jsonb,text,text,text,integer)',
                         'maven.memory_write(text,jsonb,text,text)', 'maven.change_fact(jsonb,text,text)'):
            self.assertIn(obsolete, revocations)
        self.assertIn("ALTER ROLE maven_app SET statement_timeout = '5s'", executed)
        self.assertIn("ALTER ROLE maven_app SET lock_timeout = '1s'", executed)

    def test_failed_migration_reports_safe_location_and_verified_rollback(self):
        bundle = admin.migrations()
        name, _, statements = next(item for item in bundle if item[0] == '020_secret_adapter_grants.sql')
        installed = {name: sha for name, sha, _ in bundle[:17]}
        state = {**store.control_document(CONFIG, 'ready'),
                 'app_secret_arn': CONFIG['app_secret_arn'], 'app_secret_version': 'fixture-version'}
        for sqlstate, rollback_fails, expected in (
                ('42601', False, 'migration_rejected'),
                ('57014', False, 'migration_failed'),
                ('42601', True, 'migration_outcome_unknown')):
            with self.subTest(sqlstate=sqlstate, rollback_fails=rollback_fails):
                data = Mock()
                data.get_caller_identity.return_value = {'Arn': CONFIG['admin_role_arn']}
                data.begin_transaction.return_value = {'transactionId': 'tx'}
                provider_error = ClientError({'Error': {
                    'Code': 'DatabaseErrorException',
                    'Message': f'private-provider-value SQLState: {sqlstate}'}}, 'ExecuteStatement')

                def execute(**kwargs):
                    if kwargs['sql'] == statements[9]:
                        raise provider_error
                    return {}

                data.execute_statement.side_effect = execute
                if rollback_fails:
                    data.rollback_transaction.side_effect = TimeoutError('private-rollback-value')
                session = Mock()
                session.client.return_value = data
                with patch.object(admin, '_config'), patch.object(admin, 'authorized_actor'), \
                        patch.object(admin, '_check_logging'), \
                        patch.object(admin.redaction_ledger, 'cluster_identity', return_value='a'*64), \
                        patch.object(admin, '_control', return_value=(store.control_document(CONFIG, 'maintenance'), 'etag')), \
                        patch.object(store.DataAPI, 'scalar', side_effect=[
                            True, True, json.dumps(installed), json.dumps(state)]):
                    with self.assertRaises(validation.MemoryError) as caught:
                        admin._run(CONFIG, session, upgrade=True)
                error = caught.exception
                self.assertEqual(error.status, expected)
                self.assertEqual(error.http_status, 503 if rollback_fails else 422)
                self.assertIn(f'Migration: {name}; statement: 10; SQLSTATE: {sqlstate}', error.metadata['detail'])
                self.assertIn('rollback unconfirmed' if rollback_fails else 'rollback confirmed', error.metadata['detail'])
                self.assertNotIn('private-', json.dumps(error.metadata))
                data.rollback_transaction.assert_called_once()
                data.commit_transaction.assert_not_called()
                data.put_object.assert_not_called()  # Never clear maintenance after a failed migration.

    def test_rollback_confirmation_does_not_survive_another_transaction(self):
        data = Mock()
        data.begin_transaction.return_value = {'transactionId': 'tx'}
        db = store.DataAPI(data, CONFIG, admin=True)
        with self.assertRaises(ValueError):
            with db.transaction():
                raise ValueError('rejected statement')
        self.assertTrue(db.rollback_confirmed)
        data.begin_transaction.side_effect = TimeoutError('no transaction obtained')
        with self.assertRaises(TimeoutError):
            with db.transaction():
                self.fail('Transaction must not start')
        self.assertFalse(db.rollback_confirmed)

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
    OPERATION = '20000000-0000-4000-8000-000000000002'

    def run_cleanup(self, call, result, error=None, *, capability=True):
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {
            'Arn': 'arn:aws:sts::123456789012:assumed-role/memory-admin/owner'}
        database = Mock()
        database.transaction.return_value.__enter__ = Mock(return_value='tx')
        database.transaction.return_value.__exit__ = Mock(return_value=False)
        database.scalar.return_value = capability
        database.json_scalar.side_effect = error if error else [json.dumps(result)]
        state = {**store.control_document(CONFIG, 'ready'), 'app_secret_arn': CONFIG['app_secret_arn'],
                 'app_secret_version': 'fixture-version'}
        with patch.object(admin, 'DataAPI', return_value=database) as adapter, \
                patch.object(admin, '_check_logging') as logging_check, \
                patch.object(admin, 'fetch_control', return_value=({'redaction_bound': True}, 'etag')), \
                patch.object(admin.redaction_ledger, 'cluster_identity', return_value='a'*64), \
                patch.object(admin.redaction_ledger, 'verify'), \
                patch.object(admin.erase_fence, 'record_legacy_erase'), \
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
            CONFIG, session, [self.RECORD], operation_id=self.OPERATION, expected_revisions={self.RECORD: 1},
            restore=True, actor='owner', reason='Restore', approval_digest=self.DIGEST),
            {'receipt_id': self.RECORD, 'changed': [self.RECORD], 'unchanged': []})
        self.assertTrue(call.args[0].startswith('maven.forget_records('))
        self.assertEqual(call.args[1], {'ids': json.dumps([self.RECORD]), 'restore': True, 'actor': 'owner',
                                        'reason': 'Restore', 'digest': self.DIGEST, 'operation_id': self.OPERATION,
                                        'expected_revisions': validation.canonical({self.RECORD: 1})})
        _, call = self.run_cleanup(lambda session: admin.erase_records(
            CONFIG, session, [self.RECORD], operation_id=self.OPERATION, expected_revisions={self.RECORD: 1},
            actor='owner', reason='Erase', approval_digest=self.DIGEST),
            {'receipt_id': self.RECORD, 'records': []})
        self.assertTrue(call.args[0].startswith('maven.erase_records('))
        self.assertEqual(call.args[2], 'tx')

    def test_refusals_fail_before_or_from_sql_without_values(self):
        for record_ids, digest in (([], self.DIGEST), ([self.RECORD] * 201, self.DIGEST), ([self.RECORD], 'A' * 64)):
            with self.assertRaises(validation.MemoryError) as error:
                admin.erase_records(CONFIG, Mock(), record_ids, operation_id=self.OPERATION,
                                    expected_revisions={self.RECORD: 1}, actor='owner', reason='Erase', approval_digest=digest)
            self.assertEqual(error.exception.status, 'cleanup_refused')
        refusal = Exception()
        refusal.response = {'Error': {'Code': 'DatabaseErrorException',
                                      'Message': 'ERROR: cleanup_refused:lifecycle_kind'}}
        with self.assertRaises(validation.MemoryError) as error:
            self.run_cleanup(lambda session: admin.forget_records(
                CONFIG, session, [self.RECORD], operation_id=self.OPERATION, expected_revisions={self.RECORD: 1},
                actor='owner', reason='Forget', approval_digest=self.DIGEST),
                None, refusal)
        self.assertEqual((error.exception.status, error.exception.http_status, error.exception.metadata),
                         ('cleanup_refused', 409, {'detail': 'lifecycle_kind'}))

    def test_old_schema_refuses_only_cleanup_mutation(self):
        with self.assertRaises(validation.MemoryError) as error:
            self.run_cleanup(lambda session: admin.erase_records(
                CONFIG, session, [self.RECORD], operation_id=self.OPERATION, expected_revisions={self.RECORD: 1},
                actor='owner', reason='Erase', approval_digest=self.DIGEST), None, capability=False)
        self.assertEqual(error.exception.status, 'cleanup_upgrade_required')
        result, _ = self.run_cleanup(lambda session: admin.cleanup_describe(CONFIG, session, [self.RECORD]),
                                     [], capability=False)
        self.assertEqual(result, [])

    def test_exact_review_is_required_before_transport(self):
        for revisions in ({}, {self.RECORD: True}, {self.RECORD: 0}, {self.RECORD: 2147483648},
                          {self.RECORD: 1, self.OPERATION: 1}, None):
            with self.subTest(revisions=revisions), patch.object(admin, '_cleanup') as transport:
                with self.assertRaises(validation.MemoryError) as error:
                    admin.erase_records(CONFIG, Mock(), [self.RECORD], operation_id=self.OPERATION,
                                        expected_revisions=revisions, actor='owner', reason='Erase',
                                        approval_digest=self.DIGEST)
                self.assertEqual(error.exception.metadata, {'detail': 'expected_revisions'})
                transport.assert_not_called()

    def test_lost_response_retry_preserves_exact_operation_and_review(self):
        args = dict(operation_id=self.OPERATION, expected_revisions={self.RECORD: 7},
                    actor='owner', reason='Erase', approval_digest=self.DIGEST)
        original = {'receipt_id': self.OPERATION, 'operation_id': self.OPERATION, 'records': []}
        with patch.object(admin, '_cleanup', side_effect=[validation.MemoryError('outcome_unknown'), original]) as transport:
            with self.assertRaises(validation.MemoryError):
                admin.erase_records(CONFIG, Mock(), [self.RECORD], **args)
            result = admin.erase_records(CONFIG, Mock(), [self.RECORD], **args)
        self.assertEqual(result, original)
        self.assertEqual(transport.call_args_list[0].args[2:], transport.call_args_list[1].args[2:])


if __name__ == '__main__':
    unittest.main()
