"""Clone an actual pre-026 PostgreSQL database, then recover it using the production gate.

Both DBs and the role are UUID-named in the explicitly disposable test cluster.
Provider APIs are in-memory; no AWS credentials or network operations are used.
"""
from contextlib import contextmanager
import json
import os
import re
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from test_memory_concurrency import Connection, ENABLED, cleanup_expression, database_dsn, literal, run_sql, write_expression
from test_memory_owner import SQL_HELPERS, record
from test_memory_service import CONFIG
from test_redaction_ledger import Objects, rds, uid
from tools.memory import admin, erase_fence, redaction_ledger as ledger, redaction_objects, redaction_recovery as restore
from tools.memory.store import control_document
from tools.memory.validation import MemoryError, canonical, digest


class LocalDB:
    """Run the DataAPI admin/app SQL on a real PostgreSQL connection. No SQL emulation."""
    def __init__(self, conn, name, role, *, app=False):
        self.conn, self.name, self.role, self.app = conn, name, role, app

    def render(self, sql, params):
        def parameter(match):
            key = match[1]
            if key not in (params or {}):
                return match[0]
            value = params[key]
            return 'NULL' if value is None else "'" + str(value).replace("'", "''") + "'"
        sql = re.sub(r'(?<!:):([a-z_]+)', parameter, sql)
        # The production privilege checker is deliberately bound to the 'maven'
        # DB and 'maven_app' login. Use only our two fixture identifiers instead.
        sql = re.sub(r'\bmaven_app\b', self.role, sql)
        sql = sql.replace("current_user = 'maven_admin'", 'current_user = session_user')
        return sql.replace("has_database_privilege(rolname, 'maven'", f"has_database_privilege(rolname, '{self.name}'")

    @contextmanager
    def transaction(self):
        self.conn.query('BEGIN;')
        try:
            if self.app:
                self.conn.query(f'SET LOCAL ROLE {self.role};')
            yield self
        except BaseException:
            self.conn.query('ROLLBACK;')
            raise
        else:
            self.conn.query('COMMIT;')

    def execute(self, sql, values=None, transaction=None):
        return self.conn.query(self.render(sql, values) + ';')

    def scalar(self, sql, values=None, transaction=None):
        sql = self.render(sql, values)
        return json.loads(self.conn.query("WITH q(value) AS (" + sql + ") SELECT COALESCE((SELECT to_jsonb(value) FROM q),'null'::jsonb);")[-1])

    def json_scalar(self, expression, values=None, transaction=None):
        return canonical(self.conn.json(self.render(expression, values)))


@unittest.skipUnless(ENABLED, 'requires explicitly disposable local PostgreSQL and CREATEDB opt-in')
class IsolatedRestoreTests(unittest.TestCase):
    def test_pre_migration_clone_cannot_use_app_until_ledger_and_external_work_are_verified(self):
        parent = os.environ['MAVEN_MEMORY_TEST_DSN']
        self.assertEqual(run_sql(parent, "SELECT to_regnamespace('maven') IS NULL"), 't')
        role = 'restore_app_' + uuid4().hex
        source, dest = ('restore_test_' + uuid4().hex for _ in range(2))
        run_sql(parent, 'CREATE ROLE ' + role + ' LOGIN NOINHERIT')
        self.addCleanup(run_sql, parent, 'DROP ROLE ' + role)
        run_sql(parent, 'CREATE DATABASE ' + source)
        self.addCleanup(run_sql, parent, 'DROP DATABASE ' + source + ' WITH (FORCE)')
        src_dsn, dst_dsn = database_dsn(parent, source), database_dsn(parent, dest)
        config = {**CONFIG, 'instance_id': uid(), 'recovery_epoch': uid()}
        new = {**config, 'recovery_epoch': uid(), 'cluster_arn': config['cluster_arn'] + '-restored'}
        canary, rid = 'synthetic-snapshot-' + uuid4().hex, uid()
        note = record(data={'title': 'Narrow erase fixture', 'body_markdown': canary, 'category': 'reference'})
        setup = Connection(src_dsn, helpers=False)
        try:
            db = LocalDB(setup, source, role)
            with db.transaction() as tx:
                db.execute('CREATE SCHEMA maven; CREATE TABLE maven.schema_migrations(name text PRIMARY KEY,sha256 text NOT NULL,installed_at timestamptz DEFAULT now())')
                for name, sha, statements in admin.migrations()[:25]:
                    admin._apply_migration(db, tx, name, sha, statements)
                db.execute("INSERT INTO maven.service_state VALUES(true,CAST(:instance AS uuid),CAST(:epoch AS uuid),2,'owner_memory','ready',:secret,'1')",
                           {'instance': config['instance_id'], 'epoch': config['recovery_epoch'], 'secret': config['app_secret_arn']})
                db.execute("INSERT INTO maven.cleanup_receipts(id,action,record_ids,revisions,actor,reason,approval_digest) VALUES(CAST(:rid AS uuid),'forget',ARRAY[gen_random_uuid()],'{}',:secret,:secret,repeat('a',64))", {'rid': rid, 'secret': canary})
                db.execute("SET LOCAL maven.client_protocol='4';" + SQL_HELPERS)
                self.assertEqual(setup.json(write_expression(note))['revision'], 1)
                db.execute(f'GRANT USAGE ON SCHEMA maven TO {role}; GRANT SELECT ON maven.service_state,maven.record_versions TO {role}; REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        finally:
            setup.close()
        # Physical backup/restore predates every ledger-related migration.
        run_sql(parent, f'CREATE DATABASE {dest} TEMPLATE {source}')
        self.addCleanup(run_sql, parent, 'DROP DATABASE ' + dest + ' WITH (FORCE)')
        run_sql(parent, f'REVOKE ALL ON DATABASE {dest} FROM PUBLIC; GRANT CONNECT ON DATABASE {dest} TO {role}')
        source_conn, dst_conn = Connection(src_dsn, helpers=False), Connection(dst_dsn, helpers=False)
        self.addCleanup(source_conn.close)
        self.addCleanup(dst_conn.close)
        db, restored = LocalDB(source_conn, source, role), LocalDB(dst_conn, dest, role)
        s3, old_rds, new_rds = Objects(), rds(config), rds(new, 'cluster-NEWRESOURCE1234')
        physical = ledger.cluster_identity(old_rds, config)
        target, job = [{'kind': 'cleanup', 'id': rid}], uid()
        session = Mock()
        ec2, sts, data = Mock(), Mock(), Mock()
        ec2.describe_security_groups.return_value = {'SecurityGroups': [{'GroupId': 'sg-fixture', 'IpPermissions': []}]}
        sts.get_caller_identity.return_value = {'Arn': 'arn:aws:sts::123456789012:assumed-role/memory-admin/fixture'}
        services = {'s3': s3, 'rds': new_rds, 'ec2': ec2, 'sts': sts, 'rds-data': data}
        session.client.side_effect = lambda name, **_: services[name]
        with db.transaction() as tx:
            for name, sha, statements in admin.migrations()[25:]:
                admin._apply_migration(db, tx, name, sha, statements)
            db.execute("UPDATE maven.service_state SET state='maintenance' WHERE singleton")
            ledger.genesis(db, tx, s3, config, physical)
            erase_fence.initialize(db, tx, s3, config)
            db.execute("UPDATE maven.service_state SET state='ready' WHERE singleton")
        with db.transaction() as tx:
            narrow_job = uid()
            source_conn.json(cleanup_expression(note, narrow_job, 1))
            erase_fence.record_legacy_erase(db, tx, s3, config, narrow_job)
        s3.bytes['control.json'] = canonical(control_document(config, 'ready', redaction_bound=True)).encode()
        with db.transaction() as tx:
            plan = source_conn.json(f'maven.redaction_review({literal(target)})')
            reviewed = redaction_objects.review(config, session, db.scalar('SELECT maven.redaction_salt()::text'), [])
            digest_before = digest({'plan': plan, 'objects': reviewed})
            source_conn.json(f"maven.redaction_apply('{job}',{literal(target)},'{plan['plan_digest']}')")
            ledger.record_applied(db, tx, s3, config, physical, job, plan['permanent_targets'], digest_before, object_plan=reviewed)
        self.assertEqual(db.scalar('SELECT count(*) FROM maven.cleanup_receipts WHERE id=CAST(:r AS uuid)', {'r': rid}), 0)
        self.assertEqual(restored.scalar('SELECT reason FROM maven.cleanup_receipts'), canary)
        self.assertNotIn(canary.encode(), b''.join(s3.bytes.values()))
        login_observations = []
        def connect(data_client, cfg, *, admin=False):
            self.assertIs(data_client, data)
            self.assertEqual(cfg, new)
            if admin:
                return restored
            # Before issuing the first app credential, check the committed DB,
            # independent seal and quarantine state (not merely the result object).
            self.assertEqual(restored.scalar('SELECT count(*) FROM maven.cleanup_receipts WHERE id=CAST(:r AS uuid)', {'r': rid}), 0)
            self.assertEqual(restored.scalar('SELECT visibility_state FROM maven.records WHERE id=CAST(:r AS uuid)',
                                             {'r': note['record_id']}), 'forgotten')
            self.assertNotIn(canary, restored.scalar('SELECT snapshot::text FROM maven.record_versions WHERE record_id=CAST(:r AS uuid)',
                                                      {'r': note['record_id']}))
            self.assertEqual(restored.scalar('SELECT verified FROM maven.redaction_outbox'), True)
            self.assertEqual(restored.scalar('SELECT state FROM maven.service_state'), 'maintenance')
            entry = ledger.latest(s3, new)
            self.assertEqual(entry['epoch'], new['recovery_epoch'])
            self.assertEqual(restored.scalar('SELECT redaction_checkpoint FROM maven.service_state'), digest(entry))
            self.assertTrue(restored.scalar('SELECT rolcanlogin FROM pg_roles WHERE rolname=:r', {'r': role}))
            login_observations.append(True)
            return LocalDB(dst_conn, dest, role, app=True)
        with patch.object(restore, 'DataAPI', side_effect=connect), patch.object(admin, '_check_logging'):
            # The shared RDS parameter-group routine has dedicated offline tests;
            # the replay, schema verification, and real app privilege queries run here.
            approval = restore.review(new, session)
            with patch.object(redaction_objects, 'restore', side_effect=redaction_objects.ObjectRedactionError('object_retained')):
                with self.assertRaises(MemoryError):
                    restore.apply(new, session, approval)
            self.assertFalse(restored.scalar('SELECT rolcanlogin FROM pg_roles WHERE rolname=:r', {'r': role}))
            self.assertEqual(login_observations, [])
            self.assertEqual(ledger.latest(s3, config)['epoch'], config['recovery_epoch'])
            run_sql(dst_dsn, f'GRANT SELECT ON maven.redaction_jobs TO {role}')
            with self.assertRaises(MemoryError):
                restore.apply(new, session, approval)
            self.assertFalse(restored.scalar('SELECT rolcanlogin FROM pg_roles WHERE rolname=:r', {'r': role}))
            self.assertEqual(login_observations, [])
            run_sql(dst_dsn, f'REVOKE SELECT ON maven.redaction_jobs FROM {role}')
            done = restore.apply(new, session, approval)
            self.assertEqual(done['status'], 'restore_verified')
            self.assertEqual(login_observations, [True])
            self.assertEqual(restored.scalar('SELECT state FROM maven.service_state'), 'ready')
            self.assertEqual(restore.apply(new, session, approval), done)
            self.assertEqual(login_observations, [True])
