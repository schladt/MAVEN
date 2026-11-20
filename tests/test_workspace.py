"""Private artifact boundaries exercised with real local records."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tools.memory.boundary import BoundaryError
from tools.reconcile_local import LocalStore
from tools.reconcile_records import make_change
from tools.workspace import SECTIONS, Workspace, WorkspaceMemory


def memory_at(root):
    return WorkspaceMemory(LocalStore(root, str(uuid.uuid4())))


def seed(memory, kind, data, record_id=None):
    record_id = record_id or str(uuid.uuid4())
    token, previous = memory.snapshot(record_id)
    envelope = make_change(previous, kind, data, 'Owner-reviewed fixture', record_id=record_id)
    memory.store.apply([envelope], token)
    return memory.read(record_id)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.memory = memory_at(self.root)
        self.workspace = Workspace(self.memory)

    def source(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_intake_requires_approval_and_preserves_private_original(self):
        source = self.source('.maven/incoming/item.txt', 'synthetic private original')
        fields = dict(path='.maven/incoming/item.txt', kind='reference',
                      origin='owner-selected fixture', sensitivity='Sensitive',
                      authorization='reviewed fixture', disposition='delete')
        with self.assertRaises(BoundaryError):
            self.workspace.intake(**fields)
        self.assertTrue(source.exists())
        result = self.workspace.intake(**fields, confirm=True, reviewed=True)
        receipt = self.memory.read(result['receipt']['record_id'])
        stored = self.root / receipt['data']['value']['stored_path']
        self.assertEqual(stored.read_text(), 'synthetic private original')
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertFalse(source.exists())
        view = self.memory.store.inventory()
        records = self.memory.store.read([item['id'] for item in view['items']], view['token'])
        self.assertNotIn('synthetic private original', json.dumps(records))

    def test_unsafe_intake_preserves_source_and_rejects_credentials_before_journaling(self):
        source = self.source('.maven/incoming/original', 'private')
        fields = dict(path='.maven/incoming/original', kind='reference', sensitivity='Sensitive',
                      authorization='approved', disposition='delete', confirm=True, reviewed=True)
        with self.assertRaises(BoundaryError):
            self.workspace.intake(**fields, origin='Bearer ' + 'A' * 32)
        self.assertFalse((self.root / '.maven/runtime/workspace').exists())
        (source.parent / 'alias').symlink_to(source)
        with self.assertRaises(BoundaryError):
            self.workspace.intake(**dict(fields, path='.maven/incoming/alias', origin='fixture'))
        with self.assertRaises(BoundaryError):
            self.workspace.intake(**dict(fields, confirm='yes', origin='fixture'))
        self.assertEqual(source.read_text(), 'private')

    def test_pending_does_not_open_source_content(self):
        self.source('.maven/incoming/item.txt', 'unread fixture')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('raw read')):
            result = self.workspace.pending()
        self.assertEqual(result['pending']['files'], 1)
        self.assertFalse(result['contents_opened'])

    def test_status_excludes_internal_fields_and_preserves_unowned_output(self):
        record = seed(self.memory, 'setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'internal': 'synthetic private detail',
            'publishable': {section: 'Approved update.' for section in SECTIONS}}))
        result = self.workspace.status(**self.memory.ref(record))
        self.assertNotIn('synthetic private detail', json.dumps(result))
        token = self.memory.store.inventory()['token']
        self.assertFalse(self.workspace.status(**self.memory.ref(record), output='team-status.md', check=True)['current'])
        self.assertFalse((self.root / 'team-status.md').exists())
        self.assertEqual(self.memory.store.inventory()['token'], token)
        target = self.source('team-status.md', 'unrelated bytes')
        with self.assertRaises(BoundaryError):
            self.workspace.status(**self.memory.ref(record), output='team-status.md')
        self.assertEqual(target.read_text(), 'unrelated bytes')

    def test_local_plan_archive_preserves_identity_and_history(self):
        plan = seed(self.memory, 'plan', dict(title='Plan', body='Completed.'))
        result = self.workspace.archive_plan(**self.memory.ref(plan), confirm=True, completed=True)
        self.assertEqual(result['plan']['record_id'], plan['record_id'])
        self.assertEqual(self.memory.read(plan['record_id'])['data']['state'], 'archived')
        _, envelope = self.memory.snapshot(plan['record_id'])
        self.assertEqual(envelope['history'][0]['record']['data'], plan['data'])
        self.assertFalse((self.root / '.maven/planning').exists())

    def test_operations_resume_every_local_effect_with_original_identity(self):
        for action, writes in (('intake', 4), ('archive_plan', 5), ('status', 3)):
            for fail_at in range(1, writes + 1):
                for after in (False, True):
                    with self.subTest(action=action, fail_at=fail_at, after=after), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        memory = memory_at(root)
                        workspace = Workspace(memory)
                        operation_id = str(uuid.uuid4())
                        if action == 'intake':
                            source = root / '.maven/incoming/source'
                            source.parent.mkdir(parents=True)
                            source.write_text('private source')
                            arguments = dict(path='.maven/incoming/source', kind='reference', origin='fixture',
                                             sensitivity='Sensitive', authorization='owner reviewed', disposition='delete',
                                             confirm=True, reviewed=True)
                        elif action == 'archive_plan':
                            plan = seed(memory, 'plan', dict(title='Plan', body='Completed.'))
                            arguments = dict(memory.ref(plan), confirm=True, completed=True)
                        else:
                            record = seed(memory, 'setting', dict(key='project-status', scope='project', value={
                                'external_status_enabled': True, 'publishable': {section: 'Approved.' for section in SECTIONS}}))
                            arguments = dict(memory.ref(record), output='team-status.md')
                        write = memory.store.apply
                        count = 0
                        def interrupted(*args, **kwargs):
                            nonlocal count
                            count += 1
                            if count == fail_at and not after:
                                raise OSError('interrupted before submission')
                            result = write(*args, **kwargs)
                            if count == fail_at and after:
                                raise OSError('lost response after submission')
                            return result
                        with patch.object(memory.store, 'apply', side_effect=interrupted), self.assertRaises(OSError):
                            getattr(workspace, action)(**arguments, operation_id=operation_id)
                        result = getattr(Workspace(memory), action)(**arguments, operation_id=operation_id)
                        self.assertEqual(result['operation_id'], operation_id)
                        self.assertEqual(memory.read(operation_id)['data']['state'], 'completed')
                        self.assertEqual(len(memory.snapshot(operation_id)[1]['history']), 1)
                        before = memory.store.inventory()
                        self.assertEqual(getattr(Workspace(memory), action)(**arguments, operation_id=operation_id), result)
                        self.assertEqual(memory.store.inventory(), before)
                        if action == 'intake':
                            self.assertFalse(source.exists())
                        elif action == 'archive_plan':
                            self.assertEqual(memory.read(plan['record_id'])['data']['state'], 'archived')
                        else:
                            self.assertIn('Approved.', (root / 'team-status.md').read_text())

    def test_status_new_output_never_overwrites_racing_editor(self):
        record = seed(self.memory, 'setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'publishable': {section: 'Approved.' for section in SECTIONS}}))
        import os
        link = os.link
        def competing_writer(source, target):
            Path(target).write_text('external editor')
            return link(source, target)
        with patch('tools.workspace.os.link', side_effect=competing_writer), self.assertRaises(FileExistsError):
            self.workspace.status(**self.memory.ref(record), output='team-status.md')
        self.assertEqual((self.root / 'team-status.md').read_text(), 'external editor')
        with self.assertRaises(BoundaryError):
            self.workspace.status(**self.memory.ref(record), output='team-status.md')

    def test_workspace_writers_are_serialized(self):
        from tools.workspace import workspace_lock
        from tools.coordination import private_lock
        with workspace_lock(self.root):
            with self.assertRaises(BoundaryError):
                with private_lock(self.root, '.maven/runtime/workspace.lock', blocking=False):
                    self.fail('concurrent lock acquired')
        with workspace_lock(self.root):
            pass

    def test_crashed_workspace_owner_does_not_leave_kernel_lock_held(self):
        import subprocess
        import sys
        from tools.workspace import workspace_lock
        script = ("import sys, time\nfrom tools.workspace import workspace_lock\n"
                  "with workspace_lock(sys.argv[1]):\n print('held', flush=True)\n time.sleep(60)\n")
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'held')
        finally:
            child.terminate()
            child.communicate(timeout=10)
        with workspace_lock(self.root):
            pass

    def test_intake_and_archive_resume_after_unlink_before_phase_receipt(self):
        for action in ('intake', 'archive_plan'):
            with self.subTest(action=action):
                relative = '.maven/incoming/item' if action == 'intake' else '.maven/planning/plan'
                source = self.source(relative, 'preserved original')
                if action == 'intake':
                    arguments = dict(path=relative, kind='reference', origin='fixture', sensitivity='Sensitive',
                                     authorization='reviewed', disposition='delete', confirm=True, reviewed=True)
                else:
                    plan = seed(self.memory, 'plan', dict(title='Plan', body='Done.'))
                    arguments = dict(self.memory.ref(plan), confirm=True, completed=True,
                                     source_path=relative, source_disposition='archive')
                phase = self.workspace._phase
                def interrupt(path, journal, state, **kwargs):
                    if state == 'removed':
                        raise OSError('interrupted after unlink')
                    return phase(path, journal, state, **kwargs)
                with patch.object(self.workspace, '_phase', side_effect=interrupt), self.assertRaises(OSError):
                    getattr(self.workspace, action)(**arguments)
                self.assertFalse(source.exists())
                journals = list((self.root / '.maven/runtime/workspace' / self.memory.project_id).glob('*.json'))
                original = next(json.loads(path.read_text()) for path in journals
                                if json.loads(path.read_text())['name'] == action.replace('_', '-'))
                result = getattr(Workspace(self.memory), action)(**arguments, operation_id=original['operation_id'])
                self.assertEqual(result['operation_id'], original['operation_id'])
                self.assertEqual(self.memory.read(original['operation_id'])['data']['state'], 'completed')

    def test_status_existing_output_resumes_after_rename_before_receipt(self):
        import os
        record = seed(self.memory, 'setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'publishable': {section: 'First.' for section in SECTIONS}}))
        self.workspace.status(**self.memory.ref(record), output='team-status.md')
        data = deepcopy(record['data'])
        data['value']['publishable']['Progress'] = 'Second.'
        record = seed(self.memory, 'setting', data, record_id=record['record_id'])
        replace = os.replace
        def interrupted(source, target):
            replace(source, target)
            if Path(target).name == 'team-status.md':
                raise OSError('interrupted after rename')
        with patch('tools.workspace.os.replace', side_effect=interrupted), self.assertRaises(OSError):
            self.workspace.status(**self.memory.ref(record), output='team-status.md')
        result = Workspace(self.memory).status(**self.memory.ref(record), output='team-status.md')
        self.assertTrue(result['current'])
        self.assertIn('Second.', (self.root / 'team-status.md').read_text())

    def test_receipt_is_local_and_project_scoped(self):
        self.source('.maven/incoming/item', 'original')
        result = self.workspace.intake(path='.maven/incoming/item', kind='reference', origin='fixture',
                                       sensitivity='Sensitive', authorization='reviewed', disposition='retain')
        identifier = result['operation_id']
        self.assertEqual(self.workspace.receipt(operation_id=identifier)['phase'], 'completed')
        with self.assertRaises(BoundaryError):
            Workspace(memory_at(self.root)).receipt(operation_id=identifier)


if __name__ == '__main__':
    unittest.main()
