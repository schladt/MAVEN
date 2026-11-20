"""Real competing PostgreSQL sessions; no AWS or installed-runtime changes.

Requires psql, MAVEN_MEMORY_TEST_DSN, and the explicit isolated-cluster opt-in
MAVEN_MEMORY_TEST_ALLOW_CREATE_DATABASE=1. The role must have CREATEDB. Tests
create/drop only their own UUID-named database. The application-verifier test
instead uses a transaction rolled back on disconnect in a disposable `maven` database.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from tools.memory import admin
from test_memory_owner import SQL_HELPERS, record
from test_secret_adapters import SQL_ADAPTER_HELPERS, candidate, definition


PSQL = shutil.which('psql')
ENABLED = (PSQL and os.environ.get('MAVEN_MEMORY_TEST_DSN')
           and os.environ.get('MAVEN_MEMORY_TEST_ALLOW_CREATE_DATABASE') == '1')


def literal(value):
    return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def database_dsn(dsn, name):
    if dsn.startswith(('postgres://', 'postgresql://')):
        parsed = urlsplit(dsn)
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k != 'dbname']
        return urlunsplit((parsed.scheme, parsed.netloc, '/' + name, urlencode(query), parsed.fragment))
    return (dsn + ' ' if '=' in dsn else '') + 'dbname=' + name


def run_sql(dsn, sql):
    result = subprocess.run([PSQL, '-d', dsn, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'], input=sql,
                            text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class Connection:
    """Persistent psql pipe with explicit completion markers and bounded I/O."""
    def __init__(self, dsn, *, helpers=True):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen([PSQL, '-d', dsn, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        self.pending = None
        self.buffer = b''
        self.pid = int(self.query('SELECT pg_backend_pid();')[-1])
        if not helpers:
            return
        self.query("SET maven.client_protocol='4'; SET statement_timeout='15s'; SET lock_timeout='12s';"
                   + SQL_HELPERS + SQL_ADAPTER_HELPERS + r"""
CREATE FUNCTION pg_temp.try_sql(statement text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb;
BEGIN EXECUTE statement INTO result; RETURN result;
EXCEPTION WHEN invalid_parameter_value OR insufficient_privilege OR object_not_in_prerequisite_state
  THEN RETURN jsonb_build_object('error',SQLERRM);
END $$;
""")

    def send(self, sql):
        if self.pending is not None:
            raise AssertionError('connection already has a pending statement')
        self.pending = ('complete_' + uuid4().hex).encode()
        self.process.stdin.write(sql.encode() + b'\n\\echo ' + self.pending + b'\n')
        self.process.stdin.flush()

    def read(self):
        deadline = time.monotonic() + 20
        lines = []
        while True:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if line == self.pending:
                    self.pending = None
                    return [line.decode() for line in lines if line]
                lines.append(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], max(0, remaining))[0]:
                raise AssertionError('PostgreSQL fixture response timed out')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.errors.seek(0)
                raise AssertionError(self.errors.read().decode())
            self.buffer += chunk

    def query(self, sql):
        self.send(sql)
        return self.read()

    def json(self, expression):
        return json.loads(self.query('SELECT ' + expression + ';')[-1])

    def attempt(self, expression):
        statement = ('SELECT ' + expression).replace("'", "''")
        return "pg_temp.try_sql('" + statement + "')"

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=10)
        self.errors.close()


def write_expression(body, action='write'):
    return f"pg_temp.write({literal(body)},'{action}')"


def cleanup_expression(body, operation, revision, action='erase', reason='Fixture reviewed cleanup'):
    args = (f"ARRAY['{body['record_id']}']::uuid[]," + ('false,' if action == 'forget' else '')
            + f"'{operation}'::uuid,{literal({body['record_id']: revision})},'fixture-owner',"
            + "'" + reason.replace("'", "''") + "',repeat('d',64)")
    return f'maven.{action}_records({args})'


class VerificationConnection(Connection):
    """Run the installer's scalar queries under the real application role."""
    @contextmanager
    def transaction(self):
        self.query('SAVEPOINT verify; SET LOCAL ROLE maven_app;')
        try:
            yield self
        finally:
            self.query('ROLLBACK TO verify; RELEASE verify;')

    def scalar(self, sql, *, transaction):
        return transaction.json(f'(SELECT to_jsonb(value) FROM ({sql}) AS result(value))')


@unittest.skipUnless(ENABLED, 'requires an explicitly disposable PostgreSQL cluster and psql')
class ApplicationVerificationTests(unittest.TestCase):
    def test_post_migration_application_privileges(self):
        dsn = os.environ['MAVEN_MEMORY_TEST_DSN']
        if run_sql(dsn, 'SELECT current_database();') != 'maven':
            self.skipTest('verifier checks privileges on database maven by name')
        db = VerificationConnection(dsn, helpers=False)
        self.addCleanup(db.close)  # Disconnect rolls back all DDL and role grants.
        db.query('BEGIN; CREATE SCHEMA maven; CREATE ROLE maven_app;')
        for _, _, statements in admin.migrations():
            db.query(';\n'.join(statements) + ';')
        config = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4()), 'app_secret_arn': 'fixture'}
        db.query(f"""
            INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,
              state,app_secret_arn,app_secret_version) VALUES(true,'{config["instance_id"]}',
              '{config["recovery_epoch"]}',{admin.SCHEMA_VERSION},'{admin.ROLLOUT_SCOPE}','maintenance','fixture','1');
            REVOKE ALL ON DATABASE maven FROM PUBLIC;
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            GRANT CONNECT ON DATABASE maven TO maven_app;
            GRANT USAGE ON SCHEMA maven TO maven_app;
            GRANT SELECT ON maven.service_state, maven.record_versions TO maven_app;
            GRANT EXECUTE ON FUNCTION maven.reconcile_request(text,jsonb), maven.instruction_read(jsonb) TO maven_app;
        """)
        admin._verify_application(db, config)
        db.query('SAVEPOINT missing_grant; REVOKE EXECUTE ON FUNCTION maven.reconcile_request(text,jsonb) FROM maven_app;')
        with self.assertRaises(admin.MemoryError) as denied:
            admin._verify_application(db, config)
        self.assertEqual(denied.exception.status, 'bootstrap_refused')
        db.query('ROLLBACK TO missing_grant; RELEASE missing_grant;')
        for privilege in ('EXECUTE ON FUNCTION maven.change_fact(jsonb,text,text)',
                          'EXECUTE ON FUNCTION maven.memory_write(text,jsonb,text,text)',
                          'EXECUTE ON FUNCTION maven.memory_request_before_reconcile(text,jsonb,text,text,text,integer)',
                          'SELECT ON maven.reconcile_records', 'UPDATE ON maven.reconcile_projects',
                          'EXECUTE ON FUNCTION maven.forget_records(uuid[],boolean,uuid,jsonb,text,text,text)',
                          'EXECUTE ON FUNCTION maven.erase_records(uuid[],uuid,jsonb,text,text,text)',
                          'EXECUTE ON FUNCTION maven.cleanup_mutate(text,uuid[],uuid,jsonb,text,text,text)',
                          'SELECT ON maven.cleanup_operations'):
            with self.subTest(privilege=privilege):
                db.query(f'SAVEPOINT bad_grant; GRANT {privilege} TO maven_app;')
                with self.assertRaises(admin.MemoryError) as denied:
                    admin._verify_application(db, config)
                self.assertEqual(denied.exception.status, 'bootstrap_refused')
                db.query('ROLLBACK TO bad_grant; RELEASE bad_grant;')


@unittest.skipUnless(ENABLED, 'requires explicit isolated PostgreSQL DSN/create-database opt-in and psql')
class ConcurrentPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent_dsn = os.environ['MAVEN_MEMORY_TEST_DSN']
        if run_sql(cls.parent_dsn, "SELECT to_regnamespace('maven') IS NULL;") != 't':
            raise AssertionError('refuse supplied database with existing maven schema')
        cls.database = 'maven_test_' + uuid4().hex
        cls.dsn = database_dsn(cls.parent_dsn, cls.database)
        run_sql(cls.parent_dsn, 'CREATE DATABASE "' + cls.database + '";')
        try:
            statements = ['BEGIN; CREATE SCHEMA maven;']
            for _, _, migration in admin.migrations():
                statements.extend(statement + ';' for statement in migration)
            statements.append("INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,state,app_secret_arn,app_secret_version) "
                              "VALUES(true,gen_random_uuid(),gen_random_uuid(),2,'owner_memory','ready','fixture-app','fixture-version'); COMMIT;")
            run_sql(cls.dsn, '\n'.join(statements))
            connection = Connection(cls.dsn)
            try:
                skill = {'slug': 'maven-secrets', 'page_kind': 'skill', 'title': 'Secrets fixture',
                         'body_markdown': 'Fixture governing authority', 'scope': {}, 'authority_state': 'active', 'requires': []}
                connection.query(f'SELECT pg_temp.publish({literal(skill)});')
            finally:
                connection.close()
        except BaseException:
            run_sql(cls.parent_dsn, 'DROP DATABASE "' + cls.database + '" WITH (FORCE);')
            raise

    @classmethod
    def tearDownClass(cls):
        run_sql(cls.parent_dsn, 'DROP DATABASE "' + cls.database + '" WITH (FORCE);')

    def setUp(self):
        self.observer = self.connect()

    def connect(self):
        connection = Connection(self.dsn)
        self.addCleanup(connection.close)
        return connection

    def blocked_by(self, waiting, holder):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.observer.json(f'to_jsonb({holder.pid}=ANY(pg_blocking_pids({waiting.pid})))'):
                return
            time.sleep(0.01)
        self.fail('expected real competing transaction never blocked on its holder')

    def create(self, kind='note', data=None, **extra):
        body = {**record(kind, data), **extra}
        self.assertNotIn('status', self.observer.json(write_expression(body)))
        return body

    def test_cleanup_compares_review_after_competing_writer_commits(self):
        body = self.create()
        writer, cleanup = self.connect(), self.connect()
        writer.query('BEGIN;')
        revised = {**body, 'request_key': str(uuid4()), 'expected_revision': 1,
                   'data': {**body['data'], 'title': 'Revised after cleanup review'}}
        self.assertEqual(writer.json(write_expression(revised))['revision'], 2)
        operation = str(uuid4())
        cleanup.send('SELECT ' + cleanup.attempt(cleanup_expression(body, operation, 1)) + ';')
        self.blocked_by(cleanup, writer)
        writer.query('COMMIT;')
        self.assertEqual(json.loads(cleanup.read()[-1]), {'error': 'cleanup_refused:revision_conflict'})
        self.assertEqual(self.observer.json(f"maven.memory_read({literal({'record_id': body['record_id']})})")['revision'], 2)
        self.assertEqual(self.observer.json(f"to_jsonb((SELECT count(*) FROM maven.cleanup_operations WHERE operation_id='{operation}'))"), 0)

    def test_cleanup_commit_replays_original_outcome_and_fences_waiting_writer(self):
        body = self.create()
        cleanup, retry, writer = self.connect(), self.connect(), self.connect()
        operation = str(uuid4())
        expression = cleanup_expression(body, operation, 1, reason='Synthetic private review title')
        cleanup.query('BEGIN;')
        original = cleanup.json(expression)
        retry.send('SELECT ' + expression + ';')
        revised = {**body, 'request_key': str(uuid4()), 'expected_revision': 1}
        writer.send('SELECT ' + write_expression(revised) + ';')
        self.blocked_by(retry, cleanup)
        self.blocked_by(writer, cleanup)
        cleanup.query('COMMIT;')
        self.assertEqual(json.loads(retry.read()[-1]), original)
        self.assertEqual(json.loads(writer.read()[-1])['status'], 'record_unavailable')
        # A new connection recovers the exact committed result after delivery was lost.
        recovered = self.connect()
        self.assertEqual(recovered.json(expression), original)
        self.assertEqual(recovered.json(recovered.attempt(cleanup_expression(body, operation, 1))),
                         {'error': 'cleanup_refused:operation_conflict'})
        audit = self.observer.json(f"(SELECT to_jsonb(c) FROM maven.cleanup_receipts c WHERE id='{operation}')")
        self.assertNotIn('Synthetic private', json.dumps(audit))
        self.assertEqual(audit['reason'], 'erase')
        # A committed receipt cannot authorize later history changes, even with its exact tombstone.
        recovered.query(f"BEGIN; SET LOCAL maven.erase_receipt='{operation}';")
        attempt = f"UPDATE maven.record_versions SET snapshot={literal(original['tombstone'])} WHERE record_id='{body['record_id']}' RETURNING snapshot"
        escaped = attempt.replace("'", "''")
        self.assertEqual(recovered.json("pg_temp.try_sql('" + escaped + "')"), {'error': 'immutable history'})
        recovered.query('ROLLBACK;')

    def test_old_service_dispatcher_cannot_repopulate_retired_work(self):
        project = self.create('entity', {'entity_type': 'project', 'name': 'Guard fixture', 'aliases': []})['record_id']
        body = {**record('session', {'state': 'active', 'summary': 'Old client'}), 'project_id': project}
        task = str(uuid4())
        claim = {'request_key': str(uuid4()), 'record_id': task, 'expected_revision': 1,
                 'session_id': body['record_id'], 'action': 'claim', 'lease_seconds': 60,
                 'source': body['source'], 'change_reason': 'Old client'}
        for route, request in (('/v1/memory/write', body), ('/v1/memory/claim', claim)):
            with self.subTest(route=route):
                response = self.observer.json(
                    f"maven.memory_request('{route}',{literal(request)},repeat('a',64),NULL,'fixture-owner',4)")
                self.assertEqual(response['result'], {'status': 'client_incompatible'})
        self.assertFalse(self.observer.json(
            f"to_jsonb(EXISTS(SELECT 1 FROM maven.records WHERE id='{body['record_id']}'))"))

    def test_uuid_scope_case_matches_without_rewriting_reviewed_content(self):
        project = self.create('entity', {'entity_type': 'project', 'name': 'Scope fixture', 'aliases': []},
                              record_id='abcdefab-1234-4000-8000-123456abcdef')['record_id']
        slug = 'scope-fixture-' + uuid4().hex
        page = {'slug': slug, 'page_kind': 'skill', 'title': 'Scoped fixture', 'body_markdown': 'Exact reviewed content',
                'scope': {'project': project.upper()}, 'authority_state': 'active', 'requires': []}
        self.observer.query(f'SELECT pg_temp.publish({literal(page)});')
        context = self.observer.json(f"maven.memory_context({literal({'project_id': project, 'skill_slug': slug})})")
        self.assertEqual(next(p for p in context['pages'] if p['slug'] == slug)['scope'], page['scope'])
        index = self.observer.json(f"maven.instruction_index({literal({'project_id': project, 'scope': 'context'})})")
        self.assertIn(slug, [p['slug'] for p in index['pages']])
        self.observer.query(f"BEGIN; SELECT generation FROM maven.instruction_state WHERE singleton FOR UPDATE; "
                            f"SELECT pg_advisory_xact_lock(hashtextextended('project-work:{project}',0)); "
                            f"INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id) "
                            f"VALUES('{project}',true,1,repeat('a',64),'fixture-owner'); COMMIT;")
        proposal = {**page, 'slug': 'blocked-' + uuid4().hex}
        request = {'request_key': str(uuid4()), 'proposal_id': str(uuid4()), 'page_id': str(uuid4()), 'expected_revision': 0,
                   'classification': 'Sensitive', 'data_scope': 'owner', 'candidate': proposal,
                   'source': {'origin': 'fixture', 'excerpt': 'Fixture scope'}, 'change_reason': 'Fixture'}
        expression = f"maven.instruction_write('create',{literal(request)},repeat('a',64),repeat('b',64),'fixture-owner')"
        self.assertEqual(self.observer.json(self.observer.attempt(expression)), {'error': 'project_archived'})
        self.observer.query("SELECT maven.require_project_scope('{\"project\":\"LegacyTeam\"}');")
        self.assertEqual(self.observer.json("to_jsonb(maven.project_scope_key('LegacyTeam'))"), 'LegacyTeam')

    def adapter_fixture(self, *, mode='single-use', ref=None):
        home = self.create('entity', {'entity_type': 'project', 'name': 'Grant fixture Home', 'aliases': []})['record_id']
        reference = ref or {'provider': 'ssm', 'name': '/fixture/concurrent:1'}
        catalog = self.create('setting', {'key': 'secret:fixture.concurrent', 'scope': 'project', 'value': {'store_id': 'a' * 64},
                                          'secret_ref': reference}, project_id=home)
        suffix = uuid4().hex
        adapter_slug, grant_slug = 'secret-adapter-' + suffix, 'secret-grant-' + suffix
        adapter_page, grant_page = str(uuid4()), str(uuid4())
        adapter_candidate = {**candidate(definition()), 'slug': adapter_slug, 'scope': {'project': home}}
        self.observer.query(f"SELECT pg_temp.adapter_publish({literal(adapter_candidate)},'{adapter_page}');")
        invocation = {'home_id': home, 'invocation_id': str(uuid4()), 'adapter': adapter_slug, 'adapter_revision': 1,
                      'secret_name': 'fixture.concurrent', 'secret_ref': reference, 'purpose': 'Concurrent fixture',
                      'operation': 'check', 'destination': 'fixture.invalid', 'parameters': {},
                      'catalog_record_id': catalog['record_id'], 'catalog_revision': 1, 'store_id': 'a' * 64}
        digest = hashlib.sha256(json.dumps(invocation, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        grant = {key: value for key, value in invocation.items() if key not in ('home_id', 'invocation_id')}
        grant.update(kind='secret-grant', version=1, mode=mode, expires_at='2099-01-01T00:00:00Z')
        if mode == 'single-use':
            grant['invocation_digest'] = digest
        grant_candidate = {**adapter_candidate, 'slug': grant_slug, 'body_markdown': json.dumps(grant)}
        self.observer.query(f"SELECT pg_temp.adapter_publish({literal(grant_candidate)},'{grant_page}');")
        request = {'invocation': invocation, 'invocation_digest': digest, 'grant': grant_slug, 'grant_revision': 1}
        return request, catalog, grant_candidate, grant_page

    def consume(self, request):
        return f"maven.secret_adapter_consume({literal(request)},'fixture-owner')"

    def test_cross_connection_single_use_grant_has_one_spend_and_no_successful_replay(self):
        request, _, _, _ = self.adapter_fixture()
        first, second = self.connect(), self.connect()
        first.query('BEGIN;')
        self.assertTrue(first.json(self.consume(request))['consumed'])
        second.send('SELECT ' + self.consume(request) + ';')
        self.blocked_by(second, first)
        first.query('COMMIT;')
        self.assertEqual(json.loads(second.read()[-1]), {'status': 'claim_conflict'})
        self.assertEqual(self.observer.json(self.consume(request)), {'status': 'claim_conflict'})
        invocation_id = request['invocation']['invocation_id']
        self.assertEqual(self.observer.json(f"to_jsonb((SELECT count(*) FROM maven.secret_adapter_consumptions WHERE invocation_id='{invocation_id}'))"), 1)
        sql = f"UPDATE maven.secret_adapter_consumptions SET actor_id='changed' WHERE invocation_id='{invocation_id}' RETURNING to_jsonb(invocation_id)"
        self.assertEqual(self.observer.json("pg_temp.try_sql('" + sql.replace("'", "''") + "')"), {'error': 'immutable history'})

    def test_catalog_rotation_and_revocation_serialize_before_spend(self):
        for change in ('catalog', 'revocation'):
            with self.subTest(change=change):
                request, catalog, grant, page = self.adapter_fixture()
                writer, consumer = self.connect(), self.connect()
                writer.query('BEGIN;')
                if change == 'catalog':
                    revised = {**catalog, 'request_key': str(uuid4()), 'expected_revision': 1,
                               'data': {**catalog['data'], 'secret_ref': {'provider': 'ssm', 'name': '/fixture/concurrent:2'}}}
                    self.assertEqual(writer.json(write_expression(revised))['revision'], 2)
                else:
                    writer.query(f"SELECT pg_temp.adapter_publish({literal({**grant, 'authority_state': 'revoked'})},'{page}',1);")
                consumer.send('SELECT ' + self.consume(request) + ';')
                self.blocked_by(consumer, writer)
                writer.query('COMMIT;')
                self.assertEqual(json.loads(consumer.read()[-1]), {'status': 'reference_unavailable'})
                invocation_id = request['invocation']['invocation_id']
                self.assertEqual(self.observer.json(f"to_jsonb((SELECT count(*) FROM maven.secret_adapter_consumptions WHERE invocation_id='{invocation_id}'))"), 0)

    def test_unusable_reference_never_spends_an_otherwise_exact_grant(self):
        # Deliberately bypass Python consumer validation to exercise the SQL boundary.
        for ref in ({'provider': 'ssm', 'name': '/fixture/mutable'},
                    {'provider': 'ssm', 'name': '/fixture/mutable:label'},
                    {'provider': 'secretsmanager', 'name': 'fixture/unsupported'}):
            with self.subTest(ref=ref):
                request, _, _, _ = self.adapter_fixture(ref=ref)
                self.assertEqual(self.observer.json(self.consume(request)), {'status': 'invalid_request'})
                invocation_id = request['invocation']['invocation_id']
                self.assertEqual(self.observer.json(f"to_jsonb((SELECT count(*) FROM maven.secret_adapter_consumptions WHERE invocation_id='{invocation_id}'))"), 0)


if __name__ == '__main__':
    unittest.main()
