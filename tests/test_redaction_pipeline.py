"""Exercise real SQL review/outbox and historic erase replay with synthetic object stores."""
from contextlib import contextmanager
import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_memory_concurrency import ENABLED, PostgreSQLFixture, cleanup_expression
from test_memory_service import CONFIG
from test_redaction_ledger import Objects
from test_redaction_objects import S3
from test_redaction_restore import LocalDB
from tools.memory import admin, erase_fence, redaction_ledger as ledger, redaction_objects as objects
from tools.memory.validation import MemoryError


def uid():
    return str(uuid4())


@unittest.skipUnless(ENABLED, 'requires disposable PostgreSQL and explicit opt-in')
class RedactionPipelineTests(PostgreSQLFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = LocalDB(self.observer, self.database, 'unused_fixture_role')
        self.s3 = Objects()
        self.config = {**CONFIG, **self.observer.json('maven.lock_service_state()'), 'skill_bucket': 'private-bundle'}
        self.provider, self.physical = S3(), 'f' * 64
        self.session = self.provider.session

    def test_live_commit_then_provider_crash_and_resume_exact_outbox_from_new_controller(self):
        job = uid()
        canary = 'redaction-fixture-canary-' + uid()
        record = self.create('note', {'title': canary, 'category': 'reference', 'body_markdown': canary})
        target = [{'kind': 'record', 'id': record['record_id']}]
        with self.db.transaction() as tx:
            self.db.execute("UPDATE maven.service_state SET state='maintenance' WHERE singleton")
            ledger.genesis(self.db, tx, self.s3, self.config, self.physical)
            self.db.execute("UPDATE maven.service_state SET state='ready' WHERE singleton")
        sha = hashlib.sha256(canary.encode()).hexdigest()
        key = f"skills/{self.config['instance_id']}/bundles/{sha}.zip"
        self.provider.add(key, canary.encode())
        self.provider.add(key, canary.encode())
        bundle = objects.target(self.config, self.db.scalar('SELECT maven.redaction_salt()::text'), 'skill_bundle', sha)
        target.append(bundle)

        @contextmanager
        def bound(config, session, *, job=None, review_digest=None):
            self.assertIs(config, self.config)
            with self.db.transaction() as tx:
                ledger.verify(self.db, tx, self.s3, config, self.physical, job=job, plan_digest=review_digest)
                yield self.db, tx, self.s3, self.physical

        with patch.object(admin, '_admin_transaction', bound):
            listing = admin.redaction_candidates(self.config, self.session)
            self.assertIn(target[0], listing['candidates'])
            self.assertIn(bundle, listing['candidates'])
            continuation = admin.redaction_candidates(self.config, self.session, after=bundle)
            self.assertNotIn(bundle, continuation['candidates'])
            review = admin.review_redaction(self.config, self.session, target)
            self.assertNotIn(canary, str(review))
            self.provider.partial = True
            result = admin.apply_redaction(self.config, self.session, operation_id=job, review=review)
            self.assertTrue(result['live_data_redacted'])
            self.assertEqual(result['objects']['status'], 'blocked')
            self.assertEqual(admin.redaction_pending(self.config, self.session), [{'job_id': job, 'objects': [bundle]}])
            self.assertEqual(self.db.scalar('SELECT count(*) FROM maven.record_versions WHERE record_id=CAST(:id AS uuid)',
                                            {'id': target[0]['id']}), 0)
            self.assertEqual(admin.review_redaction_objects(self.config, self.session, job), review['objects'])
            self.assertEqual(admin.finish_redaction_objects(self.config, self.session, job)['status'], 'objects_verified_purged')
            self.assertEqual([row[1] for row in self.provider.rows['private-bundle', key]], [b''])
            with self.assertRaises(MemoryError) as changed:
                admin.apply_redaction(self.config, self.session, operation_id=job, review={**review, 'bundles': [uid()]})
            self.assertEqual(changed.exception.status, 'redaction_scope_changed')
            self.assertTrue(self.db.scalar('SELECT verified FROM maven.redaction_outbox WHERE job_id=CAST(:job AS uuid)', {'job': job}))
            self.assertNotIn(canary.encode(), b''.join(self.s3.bytes.values()))

    def test_reapply_narrow_legacy_erase_without_touching_unselected_record_and_detect_missing_evidence(self):
        record, keep = self.create(), self.create()
        job, absent, project = uid(), uid(), uid()
        self.observer.query('BEGIN; SAVEPOINT before_erase;')
        try:
            self.observer.json(cleanup_expression(record, job, 1))
            erase_fence.initialize(self.db, self.db, self.s3, self.config)
            erase_fence.record_project_erases(self.s3, self.config, project, [absent])
            # The independent witnesses are NOT rolled back with the database.
            self.observer.query("ROLLBACK TO before_erase; UPDATE maven.service_state SET state='maintenance';")
            with self.assertRaises(MemoryError):
                erase_fence.verify_restored(self.db, self.db, self.s3, self.config)
            erase_fence.restore(self.db, self.db, self.s3, self.config)
            erase_fence.verify_restored(self.db, self.db, self.s3, self.config)
            self.assertEqual(self.observer.json(f"(SELECT snapshot FROM maven.record_versions WHERE record_id='{record['record_id']}')")['erased'], True)
            self.assertNotIn('erased', self.observer.json(f"(SELECT snapshot FROM maven.record_versions WHERE record_id='{keep['record_id']}')"))
            self.assertTrue(self.observer.json(f"to_jsonb(EXISTS(SELECT FROM maven.narrow_absent_ids WHERE id='{absent}' AND project='{project}'))"))
            del self.s3.bytes[next(key for key in self.s3.bytes if '/entries/legacy/' in key)]
            with self.assertRaises(MemoryError):
                erase_fence.verify_restored(self.db, self.db, self.s3, self.config)
        finally:
            self.observer.query('ROLLBACK;')
