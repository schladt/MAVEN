"""Synthetic local replicas. Optional SQL test uses ONLY disposable PostgreSQL."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from tools.memory.boundary import save_private
from tools.memory.client import MemoryClientError
from tools.memory.reconcile_cloud import CloudStore
from tools.memory.validation import MUTATION_ROUTES, MemoryError, validate
from tools.reconcile import inspect_views
from tools.reconcile_local import LocalStore
from tools.reconcile_records import make_change
from tools.reconcile_redaction import Replica, sweep_reviewed
from tools.setup_common import SetupPending


def uid():
    return str(uuid4())


SENSITIVE = 'fictitious payroll phrase ABC7'


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.project, self.rid = uid(), uid()
        self.target = {'kind': 'project_record', 'project_id': self.project, 'id': self.rid}
        self.identity = {'instance_id': uid(), 'recovery_epoch': uid()}
        self.first = make_change(None, 'note', {'text': SENSITIVE + ' first'}, 'observed', record_id=self.rid)
        self.edit = make_change(self.first, 'note', {'text': SENSITIVE + ' second'}, SENSITIVE + ' correction', record_id=self.rid)
        self.other = make_change(None, 'note', {'text': 'unrelated local-only contribution'}, 'local', record_id=uid())

    def machine(self, name):
        root = self.base / name
        home = self.base / (name + '-home')
        for directory in (root, home):
            directory.mkdir(mode=0o700)
        save_private(root, '.maven/project.json', {**self.identity, 'schema': 2, 'root': str(root), 'project_id': self.project})
        store = LocalStore(root, self.project)
        store.apply([self.edit, self.other], store.inventory()['token'])
        store.replica = Replica(home, self.identity['instance_id'])
        return store

    def deliver(self, store, targets, *, denied='project_archived'):
        client = Mock(config=self.identity)
        def invoke(route, request):
            validate(route, request)
            if route == '/v1/reconcile/markers':
                return {'status': 'ready', 'data': {'total': len(targets), 'targets': targets, 'more': False, 'token': 'a'*64}}
            if route == '/v1/reconcile/inventory':
                raise MemoryClientError(denied)
            self.fail('stale replica tried to submit a mutation')
        client.invoke.side_effect = invoke
        with store.replica.locked():
            return inspect_views(store, CloudStore(client, self.project))

    def test_archived_two_machines_and_old_sqlite_restore_offline(self):
        a, b = self.machine('a'), self.machine('b')
        old = b.path.read_bytes()
        for machine in (a, b):
            with self.assertRaisesRegex(MemoryClientError, 'project_archived'):
                self.deliver(machine, [self.target])
            self.assertNotIn(SENSITIVE.encode(), machine.path.read_bytes())
            self.assertEqual(machine.lookup(self.rid, machine.inventory()['token'])['history'], [])
            self.assertEqual(machine.lookup(self.other['record']['id'], machine.inventory()['token']), self.other)
            if machine is a:
                # A disconnected device is *not* already clean.
                self.assertIn(SENSITIVE.encode(), b.path.read_bytes())
        b.path.write_bytes(old)
        from tools.mcp.server import MavenApplication
        # Replacing the project DB alone cannot roll back learned markers, even without .env/cloud.
        app = MavenApplication(home=b.replica.home)
        with app.local(b.root) as offline:
            token = offline.inventory()['token']
            self.assertTrue(offline.read([self.rid], token)[0]['record']['erased'])
            with self.assertRaisesRegex(ValueError, 'erased'):
                offline.apply([self.edit], token)
        self.assertNotIn(SENSITIVE.encode(), b.path.read_bytes())
        self.assertNotIn(SENSITIVE.encode(), b.replica.path.read_bytes())

    def test_retired_project_blocks_future_offline_access_preserves_unselected_work(self):
        store = self.machine('barred')
        from tools.project import save_project_name
        save_project_name(store.root, {**self.identity, 'project_id': self.project}, SENSITIVE)
        targets = [{'kind': 'project', 'id': self.project}, self.target]
        with self.assertRaisesRegex(SetupPending, 'permanently retired'):
            self.deliver(store, targets, denied='project_unavailable')
        self.assertFalse((store.root / '.maven/project-name.json').exists())
        self.assertNotIn(SENSITIVE.encode(), store.path.read_bytes())
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(json.loads(db.execute('SELECT record FROM records WHERE id=?', (self.other['record']['id'],)).fetchone()[0]), self.other['record'])
        from tools.mcp.server import MavenApplication
        with self.assertRaisesRegex(SetupPending, 'permanently retired'):
            with MavenApplication(home=store.replica.home).local(store.root):
                self.fail('retired project opened through workspace/record route')
        with self.assertRaisesRegex(ValueError, 'retired'):
            LocalStore(store.root, self.project).orient()

    def test_known_sweep_registry_skills_uncertain_pending_and_admin_only_exact_artifacts(self):
        store = self.machine('mixed')
        root, home = store.root, store.replica.home
        session, legacy, page, bundle, keep_id = (uid() for _ in range(5))
        prefix = '.maven/runtime/sessions/' + session
        save_private(home, 'runtime/sessions.json', {'schema': 1, 'sessions': {session: {'root': str(root)}}})
        pending = {'route': 'write', 'session_id': session, 'body': {'record_id': legacy, 'request_key': uid(), 'data': {'body': SENSITIVE}}}
        save_private(root, prefix + '/pending-memory.json', pending)
        original = {'body': {'record_id': keep_id, 'data': {'record_id': legacy, 'note': 'unrelated'}}}
        save_private(root, prefix + '/failed-memory-write.json', original)
        assignment = uid()
        save_private(root, prefix + '/maintenance.json', {'schema': 1, 'assignments': {assignment: {
            'operation_id': assignment, 'state': 'uncertain', 'description': SENSITIVE, 'effects': [{'request': pending['body']}]}}, 'checkpoints': {}})
        op, keep_op = uid(), uid()
        admin = {'schema': 1, 'operation_id': op, 'home': str(home), 'kind': 'cleanup', 'revision': 2,
                 'state': 'working', 'presentation': {'target': self.identity['instance_id']},
                 'parameters': {'action': 'forget', 'record_ids': [legacy], 'reason': SENSITIVE}}
        save_private(home, 'runtime/administration/' + op + '.json', admin)
        other_admin = dict(admin, operation_id=keep_op, parameters={'action': 'restore', 'record_ids': [keep_id]})
        save_private(home, 'runtime/administration/' + keep_op + '.json', other_admin)
        workspace_op = uid()
        journal_name = f'.maven/runtime/workspace/{self.project}/{workspace_op}.json'
        save_private(root, journal_name, {'operation_id': workspace_op, 'project_id': self.project, 'phase': 'prepared',
                     'request': {}, 'effects': {'prepared': {'envelope': self.edit}}, 'result': {'review': SENSITIVE}})
        content = (SENSITIVE + ' binary').encode()
        sha = hashlib.sha256(content).hexdigest()
        directory = root / prefix / 'skills'
        relative_registry = prefix + '/skills/registry.json'
        registry = {'session_id': session, 'project_id': self.project, 'authority_generation': 3,
                    'pages': [{'page_id': page, 'title': SENSITIVE}, {'page_id': keep_id, 'title': 'keep'}]}
        save_private(root, relative_registry, registry)
        zipfile = directory / ('old-skill-' + sha + '.zip')
        zipfile.write_bytes(content)
        zipfile.chmod(0o600)
        authored = root / zipfile.name
        authored.write_bytes(content)
        targets = [self.target, {'kind': 'page', 'id': page}, {'kind': 'skill_bundle', 'id': bundle}, {'kind': 'record', 'id': legacy}]
        # Interrupted between marker delivery and physical cleanup: status must
        # already hide the stale review without rewriting it or letting it resume.
        with store.replica.locked():
            store.replica.remember(targets)
        from tools.administration import AdminOperations
        self.assertEqual(AdminOperations(home)._load(op)['state'], 'cancelled')
        self.assertIn(SENSITIVE, (home / f'runtime/administration/{op}.json').read_text())
        actual = sweep_reviewed(home, self.identity['instance_id'], targets, current_operation=uid(), bundle_check=lambda batch: {batch.index(sha)})
        self.assertEqual(actual['offline_copies'], 'unverified')
        self.assertEqual(actual['known_roots_checked'], 1)
        self.assertFalse(zipfile.exists())
        self.assertEqual(authored.read_bytes(), content)
        self.assertEqual(json.loads((root / relative_registry).read_text())['pages'], registry['pages'][1:])
        self.assertEqual(json.loads((root / prefix / 'failed-memory-write.json').read_text()), original)
        self.assertNotIn(SENSITIVE, (root / prefix / 'pending-memory.json').read_text())
        self.assertFalse(json.loads((root / prefix / 'pending-memory.json').read_text())['replayable'])
        self.assertNotIn(SENSITIVE, (root / prefix / 'maintenance.json').read_text())
        self.assertNotIn(SENSITIVE, (root / journal_name).read_text())
        actual_admin = json.loads((home / f'runtime/administration/{op}.json').read_text())
        self.assertEqual((actual_admin['state'], actual_admin['pending_interaction']), ('cancelled', None))
        self.assertNotIn(SENSITIVE, json.dumps(actual_admin))
        self.assertEqual(json.loads((home / f'runtime/administration/{keep_op}.json').read_text()), other_admin)

    def test_preserved_archive_on_renumbered_device_is_swept_without_touching_neighbor(self):
        store = self.machine('switched')
        ident = uid()
        archive = self.base / ('.maven-preserved-' + ident)
        archive.mkdir(mode=0o700)
        (store.root / '.maven').rename(archive / '.maven')
        preserved = LocalStore(archive, self.project)
        info = (archive / '.maven').stat()
        candidate = {'old_project': dict(self.identity, project_id=self.project, root=str(store.root)),
                     'root': str(store.root), 'operation_id': ident, 'instance_id': self.identity['instance_id'],
                     'old_directory': {'device': info.st_dev + 1, 'inode': info.st_ino}, 'preservation_path': str(archive)}
        save_private(store.replica.home, f'runtime/project-switches/{ident}.json', {'phase': 'completed', 'intent': {'candidate': candidate}})
        neighbor = self.machine('not-a-managed-preservation')
        current = uid()
        save_private(store.root, '.maven/project.json', dict(self.identity, schema=2, root=str(store.root), project_id=current))
        active = LocalStore(store.root, current)
        active.replica = store.replica
        client = Mock(config=self.identity)
        requested = []
        def markers(route, request):
            self.assertEqual(route, '/v1/reconcile/markers')
            requested.append(request['project_id'])
            targets = [self.target] if request['project_id'] == self.project else []
            return {'status': 'ready', 'data': {'total': len(targets), 'targets': targets, 'more': False, 'token': 'a'*64}}
        client.invoke.side_effect = markers
        with active.replica.locked():
            active.replica.receive(active, CloudStore(client, current))
        self.assertEqual(requested, [current, self.project])
        self.assertNotIn(SENSITIVE.encode(), preserved.path.read_bytes())
        self.assertIn(SENSITIVE.encode(), neighbor.path.read_bytes())
        # A planted path in a known place must never reach an outside target.
        preserved.path.unlink()
        preserved.path.symlink_to(neighbor.path)
        with self.assertRaises((ValueError, OSError, SetupPending)):
            sweep_reviewed(store.replica.home, self.identity['instance_id'], [self.target], current_operation=uid())
        self.assertIn(SENSITIVE.encode(), neighbor.path.read_bytes())

    def test_membership_and_marker_route_are_read_only_and_fail_closed(self):
        route = '/v1/reconcile/markers'
        self.assertNotIn(route, MUTATION_ROUTES)
        for extra in ({'targets': [self.target]}, {'after_id': self.rid}, {'bundle_hashes': [SENSITIVE]}):
            with self.assertRaises(MemoryError):
                validate(route, {**self.identity, 'project_id': self.project, **extra})
        client = Mock(config=self.identity)
        cloud = CloudStore(client, self.project)
        for target in (dict(self.target, project_id=uid()), dict(self.target, original_hash='0'*64)):
            client.invoke.return_value = {'status': 'ready', 'data': {'total': 1, 'targets': [target], 'more': False, 'token': 'a'*64}}
            with self.assertRaisesRegex(MemoryClientError, 'snapshot_incomplete'):
                list(cloud.marker_pages())
        client.invoke.return_value = {'status': 'ready', 'data': {'retired': [1]}}
        with self.assertRaisesRegex(MemoryClientError, 'snapshot_incomplete'):
            cloud.retired_bundles(['0'*64])


@unittest.skipUnless(shutil.which('psql') and os.getenv('MAVEN_MEMORY_TEST_DSN')
                     and os.getenv('MAVEN_MEMORY_TEST_ALLOW_CREATE_DATABASE') == '1',
                     'requires explicitly disposable PostgreSQL')
class SQLDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use exactly the same empty-random-db guard, migration and teardown as 025.
        import test_reconcile_cloud as pg
        pg.PostgreSQLReconciliationTests.setUpClass.__func__(cls)

    def test_archived_project_receives_markers_without_normal_content_or_mutation(self):
        import test_reconcile_cloud as pg
        from test_memory_concurrency import literal
        conn = pg.PostgreSQLReconciliationTests.connect(self)
        client = pg.Endpoint(conn)
        project = uid()
        root = {'record_id': project, 'request_key': uid(), 'expected_revision': 0, 'kind': 'entity',
                'classification': 'Sensitive', 'data_scope': 'owner', 'data': {'entity_type': 'project', 'name': 'Fixture', 'aliases': []},
                'source': {'origin': 'test', 'excerpt': 'test'}, 'change_reason': 'fixture'}
        from tools.reconcile_records import digest
        result = conn.json("maven.memory_request('/v1/memory/write'," + literal(root) + f",'{digest(root)}',NULL,'fixture',4)->'result'")
        self.assertEqual(result['revision'], 1)
        cloud = CloudStore(client, project)
        rid = uid()
        env = make_change(None, 'note', {'text': SENSITIVE}, 'local fixture', record_id=rid)
        cloud.apply([env], cloud.inventory()['token'])
        conn.query('INSERT INTO maven.project_lifecycle(project_id,archived,generation,action_key,actor_id) '
                   f"VALUES('{project}',true,1,repeat('a',64),'fixture');")
        target = {'kind': 'project_record', 'id': rid, 'project_id': project}
        selection = literal([target])
        plan = conn.json(f'maven.redaction_review({selection})')
        conn.json(f"maven.redaction_apply('{uid()}',{selection},'{plan['plan_digest']}')")
        with self.assertRaisesRegex(MemoryClientError, 'project_archived'):
            cloud.inventory()
        self.assertEqual(list(cloud.marker_pages()), [[target]])
        self.assertEqual(list(CloudStore(client, uid()).marker_pages()), [[]])
        self.assertEqual(cloud.retired_bundles(['0'*64]), set())
