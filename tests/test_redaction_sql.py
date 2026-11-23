"""Run only on the explicitly opted-in disposable PostgreSQL cluster (never AWS)."""
import hashlib
import json
import unittest
from uuid import uuid4

from test_memory_concurrency import ENABLED, PostgreSQLFixture, literal, write_expression
from test_memory_owner import SQL_HELPERS, record
from tools.reconcile_records import canonical, summarize


def uid():
    return str(uuid4())


def target(kind, identifier, **scope):
    return dict(kind=kind, id=identifier, **scope)


def apply(plan, job):
    return f"maven.redaction_apply('{job}',{literal(plan['targets'])},'{plan['plan_digest']}')"


OLD_FACT, OLD_SOURCE, OLD_OPERATION = uid(), uid(), uid()
CANARY = 'private-fixture-' + uuid4().hex
OLD_HASH = hashlib.sha256(CANARY.encode()).hexdigest()
OLD_ROOT, OLD_CHILD, OLD_CLEANUP = uid(), uid(), uid()
ROOT_REQUEST = {**record('entity', {'entity_type': 'project', 'name': CANARY, 'aliases': [CANARY]}), 'record_id': OLD_ROOT}
OLD_CANDIDATE = {'slug': CANARY, 'page_kind': 'skill', 'title': CANARY, 'body_markdown': CANARY,
                 'scope': {'project': OLD_ROOT}, 'authority_state': 'active'}
OLD_WIRE = {'record': {'id': OLD_CHILD, 'written_at': '2024-01-01T00:00:00Z', 'kind': 'note',
                       'data': {'title': CANARY}}, 'history': []}
# Present *before* 026. Seed the original, pre-memory_items representation, including the
# mutable projection, immutable snapshots and actor/reason/request hashes. No live credentials.
PRE_026 = f"""
INSERT INTO maven.records(id,kind,revision) VALUES('{OLD_FACT}','fact',1),('{OLD_SOURCE}','source',1);
INSERT INTO maven.sources(id,origin,excerpt) VALUES('{OLD_SOURCE}','{CANARY}','{CANARY}');
INSERT INTO maven.facts(id,predicate,value_type,value,source_id)
VALUES('{OLD_FACT}','{CANARY}','string',to_jsonb('{CANARY}'::text),'{OLD_SOURCE}');
INSERT INTO maven.record_versions(record_id,revision,snapshot,content_hash,actor_id,change_reason)
SELECT id,1,jsonb_build_object('private','{CANARY}'),'{OLD_HASH}','{CANARY}','{CANARY}' FROM maven.records;
INSERT INTO maven.record_sources(record_id,revision,source_id,source_revision) VALUES('{OLD_FACT}',1,'{OLD_SOURCE}',1);
INSERT INTO maven.operations(request_key,request_hash,record_id,revision,operation_kind)
VALUES('{OLD_OPERATION}','{OLD_HASH}','{OLD_FACT}',1,'create_fact');
INSERT INTO maven.service_state(singleton,instance_id,recovery_epoch,schema_version,data_scope,state,app_secret_arn,app_secret_version)
VALUES(true,gen_random_uuid(),gen_random_uuid(),2,'owner_memory','ready','fixture-app','1');
SET LOCAL maven.client_protocol='4';
{SQL_HELPERS}
SELECT pg_temp.write({literal(ROOT_REQUEST)});
SELECT pg_temp.publish({literal(OLD_CANDIDATE)});
INSERT INTO maven.instruction_receipts(request_key,request_hash,proposal_id,result)
SELECT gen_random_uuid(),'{OLD_HASH}',id,jsonb_build_object('private','{CANARY}') FROM maven.instruction_proposals;
INSERT INTO maven.memory_receipts(request_key,request_hash,action,record_id,revision,result)
VALUES(gen_random_uuid(),'{OLD_HASH}','write','{OLD_ROOT}',1,jsonb_build_object('private','{CANARY}'));
INSERT INTO maven.secret_adapter_consumptions(invocation_id,invocation_digest,home_id,adapter_page_id,
    adapter_revision,grant_page_id,grant_revision,single_use,actor_id)
SELECT gen_random_uuid(),'{OLD_HASH}','{OLD_ROOT}',id,1,id,1,true,'{CANARY}' FROM maven.pages;
INSERT INTO maven.cleanup_receipts(id,action,record_ids,revisions,actor,reason,approval_digest)
VALUES('{OLD_CLEANUP}','forget',ARRAY['{OLD_ROOT}'::uuid],'{{}}','{CANARY}','{CANARY}','{OLD_HASH}');
INSERT INTO maven.cleanup_operations(operation_id,request_hash,expected_revisions,result)
VALUES('{OLD_CLEANUP}','{OLD_HASH}','{{}}',jsonb_build_object('private','{CANARY}'));
INSERT INTO maven.reconcile_projects(project_id,version,record_count) VALUES('{OLD_ROOT}',gen_random_uuid(),1);
INSERT INTO maven.reconcile_records(project_id,id,wire,item)
VALUES('{OLD_ROOT}','{OLD_CHILD}','{canonical(OLD_WIRE)}',{literal(summarize(OLD_WIRE))});
INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id)
VALUES('{OLD_ROOT}',true,1,'{OLD_HASH}','{CANARY}');
DELETE FROM maven.service_state;
COMMIT; BEGIN;
"""


@unittest.skipUnless(ENABLED, 'requires the explicitly isolated PostgreSQL DSN, CREATEDB opt-in and psql')
class ReviewedRedactionTests(PostgreSQLFixture, unittest.TestCase):
    before_migration = {'026_reviewed_redaction.sql': [PRE_026]}

    def review(self, *targets):
        return self.observer.json(f'maven.redaction_review({literal(targets)})')

    def redact(self, *targets):
        plan = self.review(*targets)
        job = uid()
        return plan, self.observer.json(apply(plan, job))

    def assert_absent(self, *needles):
        # Inspect every logical table, not just public reads that may hide forgotten rows.
        names = self.observer.query("SELECT tablename FROM pg_tables WHERE schemaname='maven';")
        for name in names:
            self.assertTrue(name.replace('_', '').isalnum())
            self.assertFalse(self.observer.json(
                'to_jsonb(EXISTS(SELECT 1 FROM maven.' + name + ' r CROSS JOIN jsonb_array_elements_text('
                + literal(needles) + ') n WHERE strpos(to_jsonb(r)::text,n)>0))'), name)

    def test_populated_025_upgrade_scrubs_raw_fact_sources_and_cross_route_replay(self):
        untouched = self.create()['record_id']
        plan = self.review(target('source', OLD_SOURCE), target('project', OLD_ROOT))
        expected = [target('record', OLD_FACT), target('operation', OLD_OPERATION), target('citation', OLD_SOURCE),
                    target('cleanup', OLD_CLEANUP), target('project_record', OLD_CHILD, project_id=OLD_ROOT)]
        for related in expected:
            self.assertIn(related, plan['permanent_targets'])
        self.assertNotIn(CANARY, str(plan))
        self.assertNotIn(OLD_HASH, str(plan))
        job = uid()
        sql = apply(plan, job)
        result = self.observer.json(sql)
        self.assertEqual(result, {'job_id': job, 'live_data_redacted': True, 'pending_bundles': []})
        self.assertEqual(self.connect().json(sql), result)
        self.assertEqual(self.observer.json(f"maven.redaction_job('{job}')"), {**result, 'plan_digest': plan['plan_digest']})
        self.assertEqual(self.observer.json(self.observer.attempt(apply({**plan, 'plan_digest': '0' * 64}, job))),
                         {'error': 'cleanup_refused:operation_conflict'})
        self.assert_absent(CANARY, OLD_HASH)
        fresh = {**self.create(), 'request_key': OLD_OPERATION, 'record_id': uid()}
        self.assertEqual(self.observer.json(self.observer.attempt(write_expression(fresh))),
                         {'error': 'cleanup_refused:redacted_identifier'})
        self.assertEqual(self.observer.json(f"to_jsonb(EXISTS(SELECT 1 FROM maven.records WHERE id='{fresh['record_id']}'))"), False)
        self.assertEqual(self.observer.json(f"maven.memory_read({literal({'record_id': untouched})})")['record_id'], untouched)

    def proposal(self, candidate=None, *, page=None, base=0):
        marker = 'sensitive-skill-' + uuid4().hex
        candidate = candidate or {'slug': marker, 'page_kind': 'skill', 'title': marker, 'body_markdown': marker,
                                  'scope': {}, 'authority_state': 'active'}
        request = {'request_key': uid(), 'proposal_id': uid(), 'page_id': page or uid(), 'expected_revision': base,
                   'classification': 'Sensitive', 'data_scope': 'owner', 'candidate': candidate,
                   'source': {'origin': marker, 'excerpt': marker}, 'change_reason': marker}
        hash_ = hashlib.sha256(canonical(candidate).encode()).hexdigest()
        sql = f"maven.instruction_write('create',{literal(request)},'{hash_}','{hash_}','{marker}')"
        self.assertNotIn('status', self.observer.json(sql))
        return request, hash_, marker

    def resolve(self, request, hash_, challenge, marker):
        decision = {'request_key': uid(), 'proposal_id': request['proposal_id'], 'challenge_id': challenge['challenge_id'],
                    'candidate_hash': hash_, 'expected_revision': request['expected_revision'],
                    'approval_source': {'origin': marker, 'excerpt': marker}}
        return f"maven.instruction_write('resolve',{literal(decision)},'{hash_}',NULL,'{marker}')"

    def race(self, writer, plan):
        waiting = self.connect()
        job = uid()
        waiting.send('SELECT ' + waiting.attempt(apply(plan, job)) + ';')
        self.blocked_by(waiting, writer)
        writer.query('COMMIT;')
        self.assertEqual(json.loads(waiting.read()[-1]), {'error': 'cleanup_refused:revision_conflict'})
        self.assertEqual(self.observer.json(f"to_jsonb(maven.redaction_job('{job}') IS NULL)"), True)
        self.assertEqual(self.observer.json('to_jsonb((SELECT count(*) FROM maven.redaction_work))'), 0)

    def test_pending_challenge_and_publication_races_then_revoke_all_revisions_and_shared_bundle(self):
        marker = 'contained-' + uuid4().hex
        sha = hashlib.sha256(marker.encode()).hexdigest()
        candidate = {'slug': marker, 'page_kind': 'skill', 'title': marker, 'body_markdown': marker,
                     'scope': {}, 'authority_state': 'active', 'bundle': {'version': marker, 'sha256': sha,
                     'manifest_sha256': sha, 'size_bytes': 1, 'load_policy': 'on-demand'}}
        sibling = self.observer.json(f"to_jsonb(pg_temp.publish({literal({**candidate, 'slug': marker + '-peer'})}))")
        draft, original_hash, actor = self.proposal(candidate)
        proposal_id = draft['proposal_id']
        selected = target('proposal', proposal_id)
        plan = self.review(selected)
        writer = self.connect()
        writer.query('BEGIN;')
        challenge = writer.json(f"maven.instruction_challenge('{proposal_id}','{actor}')")
        self.race(writer, plan)
        plan = self.review(selected)
        writer.query('BEGIN;')
        publish = self.resolve(draft, original_hash, challenge, actor)
        self.assertEqual(writer.json(publish)['revision'], 1)
        self.race(writer, plan)
        newer = {**candidate, 'body_markdown': marker + '-v2'}
        self.observer.query(f"SELECT pg_temp.adapter_publish({literal(newer)},'{draft['page_id']}',1);")
        dependency = {**candidate, 'slug': marker + '-dependent', 'requires': [marker]}
        del dependency['bundle']
        dependent = self.observer.json(f'to_jsonb(pg_temp.publish({literal(dependency)}))')
        plan = self.review(selected)
        self.assertIn(target('page', sibling), plan['permanent_targets'])
        self.assertIn(target('page', dependent), plan['permanent_targets'])
        handle, = plan['bundles']
        self.assertEqual(self.observer.json(f"maven.redaction_bundle_keys({literal([handle])})"), {handle: sha})
        for value in (sha, marker, actor, original_hash):
            self.assertNotIn(value, str(plan))
        before = self.observer.json('to_jsonb((SELECT generation FROM maven.instruction_state))')
        result = self.observer.json(apply(plan, uid()))
        self.assertEqual(result['pending_bundles'], [handle])
        self.assertGreater(self.observer.json('to_jsonb((SELECT generation FROM maven.instruction_state))'), before)
        self.assertEqual(self.observer.json(publish)['status'], 'proposal_unavailable')
        self.assertEqual(self.observer.json(f"maven.instruction_read({literal({'slug': marker, 'revision': 1})})")['status'], 'page_unavailable')
        self.assertEqual(self.observer.json(f"maven.instruction_challenge('{proposal_id}','fixture')")['status'], 'proposal_unavailable')
        self.assert_absent(marker, actor, sha, original_hash)
        new_draft = {**draft, 'request_key': uid(), 'proposal_id': uid(), 'page_id': uid()}
        create = f"maven.instruction_write('create',{literal(new_draft)},'{sha}','{original_hash}','fixture')"
        self.assertEqual(self.observer.json(self.observer.attempt(create)), {'error': 'cleanup_refused:redacted_identifier'})

    def test_waiting_approval_cannot_bind_old_challenge_to_tombstone(self):
        draft, original_hash, actor = self.proposal()
        proposal = draft['proposal_id']
        challenge = self.observer.json(f"maven.instruction_challenge('{proposal}','{actor}')")
        publish = self.resolve(draft, original_hash, challenge, actor)
        plan = self.review(target('challenge', challenge['challenge_id']))
        admin, approval = self.connect(), self.connect()
        admin.query('BEGIN;')
        self.assertTrue(admin.json(apply(plan, uid()))['live_data_redacted'])
        approval.send('SELECT ' + publish + ';')
        self.blocked_by(approval, admin)
        admin.query('COMMIT;')
        self.assertEqual(json.loads(approval.read()[-1]), {'status': 'proposal_unavailable'})
        self.assertFalse(self.observer.json(f"to_jsonb(EXISTS(SELECT 1 FROM maven.pages WHERE id='{draft['page_id']}'))"))

    def test_catalog_redaction_competes_with_consumption_and_never_unspends_grant(self):
        request, catalog, _, grant = self.adapter_fixture()
        selected = target('record', catalog['record_id'])
        plan = self.review(selected)
        self.assertIn(target('page', grant), plan['permanent_targets'])
        writer = self.connect()
        writer.query('BEGIN;')
        self.assertTrue(writer.json(self.consume(request))['consumed'])
        self.race(writer, plan)
        plan, _ = self.redact(selected)
        self.assertIn(target('consumption', request['invocation']['invocation_id']), plan['permanent_targets'])
        self.assertEqual(self.observer.json(self.consume(request))['status'], 'reference_unavailable')
        self.assert_absent(request['invocation_digest'])

    def project(self):
        return self.create('entity', {'entity_type': 'project', 'name': uid(), 'aliases': []})['record_id']

    def reconcile(self, action, project, **more):
        request = {'project_id': project, **more}
        return f"maven.reconcile_request('{action}',maven.lock_service_state() || {literal(request)})::jsonb->'result'"

    def put(self, conn, project, rid, marker, prior=None):
        current = {'id': rid, 'written_at': '2026-01-02T00:00:00Z', 'kind': 'note', 'data': {'title': marker}}
        envelope = {'record': current, 'history': [{'record': {**current, 'written_at': '2025-01-01T00:00:00Z'},
                                                    'reason': marker + '-history'}]}
        change = {'wire': canonical(envelope), 'item': summarize(envelope), 'prior_digest': prior}
        token = conn.json(self.reconcile('inventory', project))['token']
        self.assertEqual(conn.json(self.reconcile('apply', project, expected_token=token, changes=[change]))['changed'], [rid])
        return change

    def test_project_wire_item_race_raw_absence_archived_project_and_identity_scoping(self):
        project, other, rid = self.project(), self.project(), uid()
        marker = 'reconcile-canary-' + uuid4().hex
        change = self.put(self.observer, project, rid, marker)
        self.put(self.observer, other, rid, 'unrelated project')
        selected = target('project_record', rid, project_id=project)
        plan = self.review(selected)
        writer = self.connect()
        writer.query('BEGIN;')
        self.put(writer, project, rid, marker + '-revised', change['item']['digest'])
        self.race(writer, plan)
        # Redaction does not depend on whether ordinary 025 read/apply is currently allowed.
        self.observer.query(f"INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id) "
                            f"VALUES('{project}',true,1,repeat('0',64),'fixture');")
        self.redact(selected)
        stored = self.observer.json(f"(SELECT jsonb_build_array(wire,item) FROM maven.reconcile_records WHERE id='{rid}' AND project_id='{project}')")
        envelope = {'history': [], 'record': {'id': rid, 'erased': True, 'written_at': '1970-01-01T00:00:00Z'}}
        self.assertEqual(stored, [canonical(envelope), summarize(envelope)])
        self.assert_absent(marker, change['item']['digest'])
        untouched = self.observer.json(self.reconcile('inventory', other))['items']
        self.assertNotEqual(untouched[0]['kind'], 'erased')
        self.redact(target('project', project))
        self.assertEqual(self.observer.json(self.reconcile('inventory', project)), {'status': 'project_unavailable'})
        self.assertEqual(self.observer.json(f"to_jsonb(maven.redaction_marked('project','{project}'))"), True)
        self.assertEqual(self.observer.json(self.reconcile('inventory', other))['items'], untouched)

    def test_restore_older_populated_image_and_absent_ids_without_live_cas(self):
        original = self.create()
        rid = original['record_id']
        job = uid()
        # ROLLBACK is the disposable pre-change image: neither the later revision nor any SQL
        # receipt/marker survives. Only the content-free target list is carried across it.
        db = self.observer
        db.query('BEGIN;')
        newer = {**original, 'request_key': uid(), 'expected_revision': 1, 'change_reason': 'private later reason'}
        self.assertEqual(db.json(write_expression(newer))['revision'], 2)
        plan = self.review(target('record', rid))
        db.json(apply(plan, job))
        db.query('ROLLBACK;')
        self.assertFalse(db.json(f"to_jsonb(maven.redaction_marked('record','{rid}'))"))
        replay = f"maven.redaction_replay('{job}',{literal(plan['permanent_targets'])})"
        self.assertEqual(db.json(db.attempt(replay)), {'error': 'cleanup_refused:restore_isolation'})
        db.query("BEGIN; UPDATE maven.service_state SET state='maintenance';")
        self.assertTrue(db.json(replay)['live_data_redacted'])
        self.assertTrue(db.json(replay)['live_data_redacted'])
        self.assertTrue(db.json(f"to_jsonb(maven.redaction_marked('memory_receipt','{newer['request_key']}'))"))
        self.assertTrue(db.json(f"to_jsonb(NOT EXISTS(SELECT 1 FROM maven.records WHERE id='{rid}'))"))
        self.assertEqual(db.json(db.attempt(write_expression(original))), {'error': 'cleanup_refused:redacted_identifier'})
        db.query("UPDATE maven.service_state SET state='ready'; COMMIT;")

    def test_restore_refuses_unreviewed_backup_only_dependencies_without_partial_scrub(self):
        project = self.project()
        plan = self.review(target('project', project))
        # A divergent backup: its additional child was NOT part of this job's authorization.
        child = self.create(project_id=project)['record_id']
        job = uid()
        db = self.observer
        db.query("BEGIN; UPDATE maven.service_state SET state='maintenance';")
        replay = f"maven.redaction_replay('{job}',{literal(plan['permanent_targets'])})"
        self.assertEqual(db.json(db.attempt(replay)), {'error': 'cleanup_refused:restore_scope_changed'})
        self.assertTrue(db.json(f"to_jsonb(EXISTS(SELECT 1 FROM maven.memory_items WHERE id='{child}'))"))
        self.assertFalse(db.json(f"to_jsonb(maven.redaction_marked('project','{project}'))"))
        self.assertTrue(db.json(f"to_jsonb(maven.redaction_job('{job}') IS NULL)"))
        db.query('ROLLBACK;')

    def test_typed_selection_rejects_aliasing_and_unreviewed_fields(self):
        project = self.project()
        for selection in ([], [target('unknown', project)], [target('record', project, project_id=project)],
                          [target('project_record', project)], [target('record', project, reason='do not retain')],
                          [target('project', project)] * 2):
            with self.subTest(selection=selection):
                statement = f'maven.redaction_review({literal(selection)})'
                self.assertEqual(self.observer.json(self.observer.attempt(statement)), {'error': 'cleanup_refused:redaction_targets'})

    def test_immutable_guard_and_admin_only_grants(self):
        db = self.observer
        record_id = self.create()['record_id']
        sql = f"DELETE FROM maven.record_versions WHERE record_id='{record_id}' RETURNING snapshot"
        db.query(f"BEGIN; SET LOCAL maven.erase_receipt='{uid()}';")
        self.assertEqual(db.json('pg_temp.try_sql(\'' + sql.replace("'", "''") + "')"), {'error': 'immutable history'})
        # Probe effective PUBLIC privileges for an unrelated built-in role. No global role
        # creation is needed (the disposable database fixture only requires CREATEDB).
        for signature in ('redaction_review(jsonb)', 'redaction_apply(uuid,jsonb,text)', 'redaction_replay(uuid,jsonb)',
                          'redaction_scrub(uuid,jsonb)', 'redaction_job(uuid)', 'redaction_salt()', 'redaction_bundle_keys(jsonb)'):
            self.assertFalse(db.json(f"to_jsonb(has_function_privilege('pg_monitor','maven.{signature}','EXECUTE'))"), signature)
        for table in ('redaction_markers', 'redaction_work', 'redaction_jobs', 'redaction_identity', 'redaction_versions', 'redaction_links'):
            self.assertFalse(db.json(f"to_jsonb(has_table_privilege('pg_monitor','maven.{table}','SELECT,INSERT,UPDATE,DELETE'))"), table)
        db.query('ROLLBACK;')
