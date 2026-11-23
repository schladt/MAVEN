"""Offline contract tests and opt-in end-to-end PostgreSQL reconciliation.

Run ``python -m unittest discover -s tests -p test_reconcile_cloud.py``. To enable
real independent PostgreSQL connections, set MAVEN_MEMORY_TEST_DSN (a *disposable*
local cluster with CREATEDB), MAVEN_MEMORY_TEST_ALLOW_CREATE_DATABASE=1 and put
psql on PATH. Only a new random fixture database is created/dropped. No AWS calls.
"""
from contextlib import contextmanager
import json
import os
import shutil
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.memory import admin, service
from tools.memory.client import MemoryClientError
from tools.memory.reconcile_cloud import CloudStore, status
from tools.memory.store import Store, control_document
from tools.memory.validation import CLIENT_PROTOCOL, MemoryError, validate
from tools.reconcile_records import canonical, digest, make_change, summarize, tombstone


PROJECT = str(uuid4())
CONFIG = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4()),
          'client_role_arn': 'arn:aws:iam::123456789012:role/MavenClient',
          'approval_role_arn': 'arn:aws:iam::123456789012:role/MavenApproval',
          'cluster_arn': 'fixture-cluster', 'database': 'fixture', 'app_secret_arn': 'fixture-secret',
          'journal_bucket': 'fixture-journal'}
TARGET = {k: CONFIG[k] for k in ('instance_id', 'recovery_epoch')}
TOKEN = 'a' * 64


def note(title='fixture', **data):
    return make_change(None, 'note', {'title': title, **data}, 'initial', record_id=str(uuid4()))


def event(action, request, role='MavenClient'):
    return {'rawPath': '/v1/reconcile/' + action, 'headers': {'x-maven-memory-protocol': str(CLIENT_PROTOCOL)},
            'requestContext': {'http': {'method': 'POST'}, 'authorizer': {'iam': {
                'userArn': f'arn:aws:sts::123456789012:assumed-role/{role}/session'}}}, 'body': canonical(request)}


def ready(data):
    return {'status': 'ready', 'data': data}


class ReconciliationBoundaryTests(unittest.TestCase):
    def test_every_route_refuses_approval_role_before_provider_access(self):
        with patch.object(service, 'configuration', return_value=CONFIG), patch.object(service, 'make_store') as provider:
            for action in ('status', 'inventory', 'read', 'apply', 'markers'):
                with self.subTest(action=action):
                    self.assertEqual(service.handler(event(action, {}, 'MavenApproval'), None)['statusCode'], 403)
            provider.assert_not_called()

    def test_size_scope_credentials_and_continuation_rejected_before_provider(self):
        base = {**TARGET, 'project_id': PROJECT}
        record = note()
        with patch.object(service, 'configuration', return_value=CONFIG), patch.object(service, 'make_store') as provider:
            for action, body in [('inventory', {**base, 'after_id': str(uuid4())}), ('inventory', TARGET),
                                 ('inventory', {**base, 'scope': 'all'}),
                                 ('apply', {**base, 'expected_token': TOKEN, 'changes': [record, record]}),
                                 ('apply', {**base, 'expected_token': TOKEN, 'changes': [dict(record, record={
                                     **record['record'], 'data': {'password': 'prohibited'}})]}),
                                 ('apply', {**base, 'expected_token': TOKEN, 'changes': [dict(record, record={
                                     **record['record'], 'data': {'text': 'x' * 1048576}})]})]:
                self.assertEqual(service.handler(event(action, body), None)['statusCode'], 422)
            provider.assert_not_called()

    def test_facade_never_returns_half_an_inventory_or_detail(self):
        client = Mock(config=CONFIG)
        cloud = CloudStore(client, PROJECT)
        item = summarize(note())
        for response in ({'token': TOKEN, 'total': 2, 'items': [item], 'next_after_id': None},
                         {'token': TOKEN, 'total': 1, 'items': [{**item, 'data': 'hidden body'}], 'next_after_id': None}):
            client.invoke.return_value = ready(response)
            with self.assertRaisesRegex(MemoryClientError, 'snapshot_incomplete'):
                cloud.inventory()
        client.invoke.return_value = ready({'token': TOKEN, 'records': [], 'remaining': 1})
        with self.assertRaisesRegex(MemoryClientError, 'snapshot_incomplete'):
            cloud.read([item['id']], TOKEN)

    def test_instance_capability_is_pinned(self):
        client = Mock(config=CONFIG)
        client.invoke.return_value = ready({**TARGET, 'recovery_epoch': str(uuid4()), 'capability': 'project_reconcile_v1'})
        with self.assertRaisesRegex(MemoryClientError, 'context_stale'):
            status(client)
        client.invoke.assert_called_once_with('/v1/reconcile/status', TARGET)


class SQLTransport:
    """Test driver replacing only the physical RDS Data API with a local psql connection."""
    def __init__(self, conn):
        self.conn = conn

    def json_scalar(self, expression, values, tx=None, **kwargs):
        if expression == 'maven.redaction_delivery(CAST(:request AS jsonb))':
            from test_memory_concurrency import literal
            return '\n'.join(self.conn.query('SELECT maven.redaction_delivery(' + literal(json.loads(values['request'])) + ');'))
        if expression != 'maven.reconcile_request(:action, CAST(:request AS jsonb))':
            raise AssertionError('unexpected expression')
        return '\n'.join(self.conn.query('SELECT ' + self.expression(values['action'], json.loads(values['request'])) + ';'))

    @staticmethod
    def expression(action, request):
        from test_memory_concurrency import literal
        assert action in {'status', 'inventory', 'read', 'prior', 'apply'}
        return f"maven.reconcile_request('{action}',{literal(request)})"

    @contextmanager
    def transaction(self, **kwargs):
        self.conn.query('BEGIN;')
        try:
            yield self.conn
        except BaseException:
            self.conn.query('ROLLBACK;')
            raise
        else:
            self.conn.query('COMMIT;')


class EraseFenceBucket:
    def __init__(self):
        self.keys = set()

    def put_object(self, *, Bucket, Key, IfNoneMatch, **fields):
        assert Bucket == CONFIG['journal_bucket'] and IfNoneMatch == '*' and Key not in self.keys
        self.keys.add(Key)
        return {'VersionId': str(uuid4()), 'ETag': '"fixture"'}


class Endpoint:
    config = CONFIG

    def __init__(self, conn):
        self.store = Store(CONFIG, None, EraseFenceBucket(), None)
        self.store.db = SQLTransport(conn)
        self.store.control = lambda: {**control_document(CONFIG, 'ready'), 'redaction_checkpoint': 'a' * 64}
        self.actions = []

    def invoke(self, route, request):
        self.actions.append(route.rsplit('/', 1)[1])
        try:
            result, _ = self.store.perform(route, validate(route, request), 'fixture', client_protocol=CLIENT_PROTOCOL)
        except MemoryError as exc:
            raise MemoryClientError(exc.status) from None
        return ready(result)


@unittest.skipUnless(shutil.which('psql') and os.getenv('MAVEN_MEMORY_TEST_DSN')
                     and os.getenv('MAVEN_MEMORY_TEST_ALLOW_CREATE_DATABASE') == '1',
                     'requires psql and an explicitly disposable local PostgreSQL cluster')
class PostgreSQLReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_memory_concurrency import database_dsn, run_sql
        parent = os.environ['MAVEN_MEMORY_TEST_DSN']
        if run_sql(parent, "SELECT to_regnamespace('maven') IS NULL;") != 't':
            raise AssertionError('refusing non-fixture database')
        db = 'maven_reconcile_test_' + uuid4().hex
        run_sql(parent, f'CREATE DATABASE {db};')
        cls.addClassCleanup(run_sql, parent, f'DROP DATABASE {db} WITH (FORCE);')
        cls.dsn = database_dsn(parent, db)
        statements = ['BEGIN; CREATE SCHEMA maven;']
        for _, _, migration in admin.migrations():
            statements.extend(s + ';' for s in migration)
        statements.append("INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,state,app_secret_arn,app_secret_version,redaction_checkpoint) "
                          f"VALUES(true,'{TARGET['instance_id']}','{TARGET['recovery_epoch']}',2,'owner_memory','ready','fixture','1',repeat('a',64)); COMMIT;")
        run_sql(cls.dsn, '\n'.join(statements))

    def connect(self):
        from test_memory_concurrency import Connection
        conn = Connection(self.dsn, helpers=False)
        self.addCleanup(conn.close)
        return conn

    def setUp(self):
        from test_memory_concurrency import literal
        self.conn = self.connect()
        self.project = str(uuid4())
        self.project_request = {'record_id': self.project, 'request_key': str(uuid4()), 'expected_revision': 0,
                                'kind': 'entity', 'classification': 'Sensitive', 'data_scope': 'owner',
                                'data': {'entity_type': 'project', 'name': 'Reconcile fixture', 'aliases': []},
                                'source': {'origin': 'test', 'excerpt': 'project selected'}, 'change_reason': 'fixture'}
        # Reviewed project administration remains available without creating any approved pages.
        self.assertEqual(self.conn.json("maven.memory_request('/v1/memory/write'," + literal(self.project_request)
                         + f",'{digest(self.project_request)}',NULL,'fixture',{CLIENT_PROTOCOL})->'result'")['revision'], 1)
        self.client = Endpoint(self.conn)
        self.cloud = CloudStore(self.client, self.project)

    def test_scope_and_archive_barriers_without_session_or_instructions(self):
        self.assertEqual(status(self.client), {**TARGET, 'capability': 'project_reconcile_v1'})
        token = self.cloud.inventory()['token']
        unknown = CloudStore(self.client, str(uuid4()))
        for op in (unknown.inventory, lambda: unknown.apply([], token), lambda: unknown.read([], token)):
            with self.assertRaisesRegex(MemoryClientError, 'project_unavailable'):
                op()
        self.conn.query("INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id) "
                        f"VALUES('{self.project}',true,1,repeat('a',64),'fixture');")
        for op in (self.cloud.inventory, lambda: self.cloud.apply([], token), lambda: self.cloud.read([], token)):
            with self.assertRaisesRegex(MemoryClientError, 'project_archived'):
                op()
        self.conn.query(f"UPDATE maven.project_lifecycle SET archived=false,generation=2 WHERE project_id='{self.project}';")
        self.assertNotEqual(self.cloud.inventory()['token'], token)
        self.assertEqual(self.conn.json(SQLTransport.expression('inventory', {**TARGET, 'project_id': self.project,
                                        'recovery_epoch': str(uuid4())}))['result']['status'], 'context_stale')

    def test_history_erasure_and_fenced_legacy_writes(self):
        from test_memory_concurrency import literal
        first = note('former-unique-wording', number=-0.0, power=1e-25, language='日本語')
        rid = first['record']['id']
        empty = self.cloud.inventory()['token']
        receipt = self.cloud.apply([first], empty)
        self.assertEqual(self.cloud.apply([first], receipt['token']), {'token': receipt['token'], 'changed': []})
        self.assertEqual(digest(self.cloud.read([rid], receipt['token'])[0]), digest(first))
        revised = make_change(first, 'note', {'title': 'updated wording'}, 'new evidence', record_id=rid)
        with self.assertRaisesRegex(MemoryClientError, 'invalid_request'):
            self.cloud.apply([{**revised, 'history': []}], receipt['token'])
        updated = self.cloud.apply([revised], receipt['token'])
        marker = tombstone(revised, record_id=rid)
        erased = self.cloud.apply([marker], updated['token'])
        self.assertEqual(self.cloud.read([rid], erased['token']), [marker])
        with self.assertRaisesRegex(MemoryClientError, 'erased_record'):
            self.cloud.apply([note(), revised], erased['token'])
        self.assertEqual(self.cloud.inventory(), {'token': erased['token'], 'items': [summarize(marker)]})
        stored = self.conn.query(f"SELECT wire||item::text FROM maven.reconcile_records WHERE project_id='{self.project}' AND id='{rid}';")[-1]
        self.assertNotIn('former-unique-wording', stored)
        self.assertNotIn('updated wording', stored)
        dto = {**self.project_request, 'kind': 'note', 'project_id': self.project, 'data': {'title': 'legacy'}}
        result = self.conn.json("maven.memory_request('/v1/memory/write'," + literal(dto) + ",repeat('a',64),NULL,'fixture',4)")
        self.assertEqual(result['result']['status'], 'client_incompatible')

    def test_paged_inventory_compact_and_read_complete_in_caller_order(self):
        batch = [note(f'title {i}', body='never load full bodies for inventory' * 480) for i in range(39)]
        token = self.cloud.apply(batch, self.cloud.inventory()['token'])['token']
        self.client.actions.clear()
        inventory = self.cloud.inventory()
        self.assertEqual(self.client.actions, ['inventory', 'inventory'])
        self.assertEqual(inventory['items'], sorted(map(summarize, batch), key=lambda x: x['id']))
        self.assertLess(len(canonical(inventory)), len(canonical(batch)) // 8)
        ids = [env['record']['id'] for env in reversed(batch)]
        self.assertEqual(self.cloud.read(ids, token), list(reversed(batch)))
        self.assertEqual(self.client.actions[2:], ['read', 'read'])
        with self.assertRaisesRegex(MemoryClientError, 'record_unavailable'):
            self.cloud.read([ids[0], str(uuid4())], token)

    def test_waiting_reader_and_second_writer_see_new_different_id(self):
        token = self.cloud.inventory()['token']
        reader, writer = self.connect(), self.connect()
        def body(env):
            return {**TARGET, 'project_id': self.project, 'expected_token': token,
                    'changes': [{'wire': canonical(env), 'item': summarize(env), 'prior_digest': None}]}
        self.conn.query('BEGIN;')
        added = note('material new evidence')
        self.assertEqual(self.conn.json(SQLTransport.expression('apply', body(added)))['result']['changed'], [added['record']['id']])
        reader.send('SELECT ' + SQLTransport.expression('read', {**TARGET, 'project_id': self.project,
                                                      'expected_token': token, 'ids': []}) + ';')
        writer.send('SELECT ' + SQLTransport.expression('apply', body(note('stale conclusion'))) + ';')
        deadline = time.monotonic() + 5
        while not self.conn.json(f'to_jsonb({self.conn.pid}=ANY(pg_blocking_pids({reader.pid})) AND EXISTS '
                                 f'(SELECT 1 FROM unnest(pg_blocking_pids({writer.pid}))))'):
            if time.monotonic() >= deadline:
                self.fail('connections did not block on competing project apply')
            time.sleep(0.01)
        self.conn.query('COMMIT;')
        for connection in (reader, writer):
            self.assertEqual(json.loads(connection.read()[-1])['result']['status'], 'view_conflict')
        self.assertEqual([item['id'] for item in self.cloud.inventory()['items']], [added['record']['id']])


if __name__ == '__main__':
    unittest.main()
