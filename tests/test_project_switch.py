"""Offline reviewed rebinding, interruption and old-runtime isolation regressions."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.coordination import CoordinationError, ensure_available, project_binding_lock
from tools.home import load_home, save_home
from tools.memory.boundary import save_private
from tools.memory.client import MemoryClientError
from tools.project import load_project
from tools import project_switch as switching
from tools.setup_common import SetupPending, read_private, save_json


class Crash(BaseException):
    """An abrupt process loss, not a recoverable application exception."""


class ProjectSwitchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'instance'
        self.home.mkdir(mode=0o700)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.old_id, self.new_id, self.instance = (str(uuid4()) for _ in range(3))
        self.operation_id = str(uuid4())
        self.source = {'origin': 'owner message', 'excerpt': 'Switch this exact folder and preserve old evidence.'}
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID=' + self.instance + '\n')
        (self.home / '.env').chmod(0o600)
        self.binding = {'connection': {'instance_id': self.instance, 'recovery_epoch': str(uuid4())},
                        'environment': {'MAVEN_MEMORY_INSTANCE_ID': self.instance}}
        self.old = {'schema': 2, 'root': str(self.root), 'project_id': self.old_id, **self.binding['connection'],
                    'git_enabled': False, 'checks': [], 'remote': None, 'accepted_url': None, 'branch': None}
        save_private(self.root, '.maven/project.json', self.old)
        self.entities = {identifier: {'record_id': identifier, 'revision': 1, 'kind': 'entity',
                                     'project_id': None, 'data_scope': 'owner', 'tags': [],
                                     'data': {'entity_type': 'project', 'name': identifier}}
                         for identifier in (self.old_id, self.new_id)}
        self.writes = []
        self.client = Mock()
        self.client.invoke.side_effect = self.invoke
        for target, options in (
                ('tools.memory.environment.configured_client', {'return_value': self.client}),
                ('tools.memory.reconcile_cloud.status', {'return_value': {**self.binding['connection'], 'capability': 'project_reconcile_v1'}}),
                ('tools.administration.AdminOperations._project_binding', {'side_effect': lambda: self.binding}),
                ('tools.administration.AdminOperations._home_client', {'return_value': self.client}),
                ('tools.setup_common.permission_preflight', {'return_value': {
                    'status': 'Passed', 'checks': [], 'limitations': [], 'remediation': ''}})):
            patched = patch(target, **options)
            patched.start()
            self.addCleanup(patched.stop)
        self.admin = AdminOperations(self.home)

    def invoke(self, route, body):
        if route == '/v1/memory/read':
            if body['record_id'] not in self.entities:
                raise MemoryClientError('record_unavailable')
            return {'data': deepcopy(self.entities[body['record_id']])}
        if route == '/v1/memory/search':
            return {'data': {'records': [deepcopy(item) for item in self.entities.values()
                                         if 'maven-home' in item['tags']]}}
        if route == '/v1/memory/write':
            self.writes.append(deepcopy(body))
            record = self.entities[body['record_id']]
            self.assertEqual(body['expected_revision'], record['revision'])
            record.update(tags=body['tags'], revision=record['revision'] + 1)
            return {'data': {'record_id': record['record_id'], 'revision': record['revision']}}
        self.fail('Unexpected cloud route: ' + route)

    def prepare(self, **options):
        return switching.prepare_switch(self.home, self.root, self.new_id, self.operation_id,
                                        self.binding, **options)

    def apply(self, candidate):
        return switching.apply_switch(self.home, candidate, self.binding, self.source)

    def local_records(self):
        path = '.maven/reconcile/' + self.old_id + '.sqlite3'
        save_private(self.root, path, {'unsynced': 'private work'})
        return path

    def make_home(self):
        return save_home(self.home, root=self.root, project_id=self.old_id,
                         instance_id=self.instance, label='Personal', consent=self.source)

    def answer(self, state, response):
        question = state['pending_interaction']
        return self.admin.respond(state['operation_id'], question['interaction_id'], question['revision'], response)

    def review(self, kind='project', *, replace=True):
        state = self.admin.begin(kind, {'root': str(self.root)})
        response = {'mode': 'switch', 'root': str(self.root), 'project_id': self.new_id}
        response.update({'name': ''} if kind == 'project' else {'label': 'Switched Home', 'replace': replace})
        state = self.answer(state, response)
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'awaiting_decision', state.get('error'))
        return state

    def approve(self, state):
        return self.answer(state, {'approved': True,
                                  'review_digest': state['pending_interaction']['review_digest'],
                                  'source': self.source})

    def test_switch_preserves_cloud_git_files_and_isolates_entire_old_runtime(self):
        record_path = self.local_records()
        self.old.update(git_enabled=True, remote='origin', accepted_url='git@example.org:owner/repo.git',
                        branch='main', checks=[['true']])
        save_private(self.root, '.maven/project.json', self.old)
        files = {'.git/config': '[remote "origin"]\nurl = unchanged\n', '.gitignore': 'unchanged\n',
                 'code.py': 'print("untouched")\n'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(content)
        before = deepcopy(self.entities)
        candidate = self.prepare()
        result = self.apply(candidate)
        archive = Path(result['preservation_path'])
        self.assertEqual(read_private(archive / record_path), {'unsynced': 'private work'})
        self.assertFalse((self.root / record_path).exists())
        self.assertEqual(list((self.root / '.maven').iterdir()), [self.root / '.maven/project.json'])
        self.assertEqual(load_project(self.root), {**self.old, 'project_id': self.new_id})
        self.assertEqual(self.entities, before)
        self.assertFalse(self.writes)
        for name, content in files.items():
            self.assertEqual((self.root / name).read_text(), content)
        self.assertEqual(self.apply(candidate), result)
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())

    def test_standalone_switch_updates_selected_home_with_exact_new_consent(self):
        self.make_home()
        candidate = self.prepare()
        self.assertTrue(candidate['update_home'])
        self.apply(candidate)
        current = load_home(self.home)
        self.assertEqual((current['root'], current['project_id'], current['label']),
                         (str(self.root), self.new_id, 'Personal'))
        self.assertEqual(current['consent'], self.source)
        self.assertFalse(self.writes)

    def test_switch_of_legacy_root_to_same_uuid_never_adopts_old_records(self):
        old = {key: value for key, value in self.old.items() if key not in ('instance_id', 'recovery_epoch')}
        save_private(self.root, '.maven/project.json', {**old, 'schema': 1})
        path = self.local_records()
        self.new_id = self.old_id
        result = self.apply(self.prepare())
        self.assertEqual(result['project'], self.old)
        self.assertFalse((self.root / path).exists())
        self.assertEqual(read_private(Path(result['preservation_path']) / path), {'unsynced': 'private work'})

    def test_new_epoch_preserves_old_store_even_with_same_uuid(self):
        path = self.local_records()
        self.new_id = self.old_id
        self.binding['connection']['recovery_epoch'] = str(uuid4())
        result = self.apply(self.prepare())
        self.assertEqual(load_project(self.root)['recovery_epoch'], self.binding['connection']['recovery_epoch'])
        self.assertFalse((self.root / path).exists())
        self.assertTrue((Path(result['preservation_path']) / path).exists())

    def test_nested_worktree_requires_existing_exclusion_before_preservation(self):
        subprocess.run(['git', 'init', '-q', str(self.base)], check=True, capture_output=True)
        archive = self.base / ('.maven-preserved-' + self.operation_id)
        with self.assertRaisesRegex(SetupPending, 'ancestor Git worktree'):
            self.prepare()
        self.assertFalse(archive.exists())
        self.assertFalse((self.base / '.gitignore').exists())
        exclusion = self.base / '.gitignore'
        exclusion.write_text('/' + archive.name + '/\n')
        candidate = self.prepare()
        exclusion.write_text('')
        with self.assertRaisesRegex(SetupPending, 'ancestor Git worktree'):
            self.apply(candidate)
        self.assertFalse(archive.exists())
        exclusion.write_text('/' + archive.name + '/\n')
        result = self.apply(candidate)
        self.assertEqual(result['preservation_path'], str(archive))
        self.assertEqual(exclusion.read_text(), '/' + archive.name + '/\n')
        visible = subprocess.run(['git', '-C', str(self.base), 'ls-files', '--others', '--exclude-standard'],
                                 check=True, capture_output=True, text=True).stdout
        self.assertNotIn(archive.name, visible)

    def test_changed_project_home_and_cloud_revision_invalidate_review(self):
        candidate = self.prepare()
        save_private(self.root, '.maven/project.json', {**self.old, 'checks': [['changed']]})
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        save_private(self.root, '.maven/project.json', self.old)
        self.make_home()
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        (self.home / 'home.json').unlink()
        self.entities[self.new_id]['revision'] += 1
        with self.assertRaisesRegex(SetupPending, 'Destination cloud project changed'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_destination_must_be_universal_owner_project(self):
        for changes in ({'data_scope': 'project'}, {'project_id': self.old_id}, {'kind': 'note'},
                        {'data': {'entity_type': 'person'}}):
            with self.subTest(changes=changes):
                original = deepcopy(self.entities[self.new_id])
                self.entities[self.new_id].update(changes)
                with self.assertRaisesRegex(SetupPending, 'universal owner project'):
                    self.prepare()
                self.entities[self.new_id] = original

    def test_symlink_runtime_and_preservation_collision_are_refused(self):
        (self.root / '.maven/runtime').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(SetupPending):
            self.prepare()
        (self.root / '.maven/runtime').unlink()
        archive = self.root.parent / ('.maven-preserved-' + self.operation_id)
        archive.mkdir(mode=0o700)
        with self.assertRaisesRegex(SetupPending, 'Preservation path already exists'):
            self.prepare()

    def test_admin_exact_review_and_stale_digest(self):
        review = self.review()
        candidate = review['pending_interaction']['review']['switch']
        self.assertEqual(candidate['old_project']['project_id'], self.old_id)
        self.assertEqual(candidate['new_project']['project_id'], self.new_id)
        self.assertIn(candidate['preservation_path'], review['pending_interaction']['description'])
        with self.assertRaises(AdminInputError):
            self.answer(review, {'approved': True, 'review_digest': 'stale', 'source': self.source})
        state = self.approve(review)
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'completed', state.get('error'))
        self.assertEqual(load_project(self.root)['project_id'], self.new_id)

    def test_changed_instance_binding_invalidates_admin_approval(self):
        state = self.approve(self.review())
        self.binding = {**self.binding, 'connection': {'instance_id': str(uuid4())}}
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'blocked')
        self.assertIn('approval is invalid', state['error'])
        self.assertEqual(load_project(self.root)['project_id'], self.old_id)

    def test_home_switch_requires_replace_and_changes_only_target_tag(self):
        self.make_home()
        self.entities[self.old_id]['tags'] = ['maven-home']
        old = deepcopy(self.entities[self.old_id])
        state = self.admin.begin('home', {'root': str(self.root)})
        state = self.answer(state, {'mode': 'switch', 'root': str(self.root), 'project_id': self.new_id,
                                    'label': 'Home', 'replace': False})
        blocked = self.admin.advance(state['operation_id'])
        self.assertEqual(blocked['state'], 'blocked')
        state = self.approve(self.review('home'))
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'completed', state.get('error'))
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)
        self.assertEqual(self.entities[self.old_id], old)
        self.assertEqual([item['record_id'] for item in self.writes], [self.new_id])

    def test_crash_after_each_rename_resumes_exact_archive_and_blocks_project_access(self):
        path = self.local_records()
        self.make_home()
        candidate = self.prepare()
        original = switching._rename
        for boundary in ('old', 'new'):
            def interrupt(source, destination):
                original(source, destination)
                if (boundary == 'old' and destination.name == '.maven' and destination.parent.name.startswith('.maven-preserved-')) or (
                        boundary == 'new' and destination == self.root / '.maven'):
                    raise Crash()
            with patch.object(switching, '_rename', side_effect=interrupt), self.assertRaises(Crash):
                self.apply(candidate)
            with self.assertRaises(CoordinationError):
                ensure_available(self.home, self.root)
            changed = deepcopy(candidate)
            changed['new_project']['project_id'] = str(uuid4())
            with self.assertRaisesRegex(SetupPending, 'Switch intent changed'):
                self.apply(changed)
        result = self.apply(candidate)
        self.assertTrue((Path(result['preservation_path']) / path).exists())
        self.assertFalse((self.root / '.maven/runtime').exists())
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)

    def test_crash_after_barrier_and_staged_write_and_completion_recovers(self):
        candidate = self.prepare()
        original = switching.save_json
        boundaries = ('barrier', 'stage', 'identity', 'completed')
        for boundary in boundaries:
            def interrupt(path, value):
                original(path, value)
                hit = ((boundary == 'barrier' and path.name.endswith('.barrier.json'))
                       or (boundary == 'stage' and path.name == 'project.json')
                       or (boundary == 'identity' and 'new_directory' in value)
                       or (boundary == 'completed' and value.get('phase') == 'completed'))
                if hit:
                    raise Crash()
            with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
                self.apply(candidate)
            self.assertTrue(switching.barrier_path(self.home, self.root).exists())
        result = self.apply(candidate)
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())

    def test_crash_after_home_write_and_before_admin_receipt_is_resumable(self):
        self.make_home()
        state = self.approve(self.review('home'))
        original = switching.save_json
        def interrupt(path, value):
            original(path, value)
            if path == self.home / 'home.json':
                raise Crash()
        with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
            self.admin.advance(state['operation_id'])
        resumed = self.admin.advance(state['operation_id'])
        self.assertEqual(resumed['state'], 'completed', resumed.get('error'))
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)

    def test_home_tag_crash_reuses_completed_local_switch_without_duplicate_tagging(self):
        self.make_home()
        state = self.approve(self.review('home'))
        original = self.admin._tag_home
        def interrupt(*args):
            original(*args)
            raise Crash()
        with patch.object(self.admin, '_tag_home', side_effect=interrupt), self.assertRaises(Crash):
            self.admin.advance(state['operation_id'])
        resumed = self.admin.advance(state['operation_id'])
        self.assertEqual(resumed['state'], 'completed', resumed.get('error'))
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]['record_id'], self.new_id)

    def test_directory_replacement_invalidates_review_even_with_identical_project_id(self):
        candidate = self.prepare()
        (self.root / '.maven').rename(self.root / 'original-runtime')
        save_private(self.root, '.maven/project.json', self.old)
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_tampered_stage_after_old_runtime_move_is_not_activated(self):
        candidate = self.prepare()
        original = switching._rename
        def interrupt(source, destination):
            original(source, destination)
            raise Crash()
        with patch.object(switching, '_rename', side_effect=interrupt), self.assertRaises(Crash):
            self.apply(candidate)
        stage = Path(candidate['preservation_path']) / 'new/project.json'
        save_json(stage, {**candidate['new_project'], 'project_id': str(uuid4())})
        with self.assertRaisesRegex(SetupPending, 'Staged replacement changed'):
            self.apply(candidate)
        self.assertFalse((self.root / '.maven').exists())
        self.assertTrue(switching.barrier_path(self.home, self.root).exists())

    def test_project_write_cannot_cross_exclusive_binding_gate(self):
        # Separate flock descriptors in the same process still contend, without threads or cloud calls.
        with project_binding_lock(self.home, self.root, shared=False):
            with patch('tools.coordination.LOCK_TIMEOUT', 0), self.assertRaises(CoordinationError):
                with project_binding_lock(self.home, self.root):
                    self.fail('Shared gate entered while cutover owned it')


if __name__ == '__main__':
    unittest.main()
