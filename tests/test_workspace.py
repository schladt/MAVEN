"""Offline boundaries for private originals and cloud-only workspace metadata."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tools.memory.boundary import BoundaryError
from tools.memory.environment import MemoryClientError
from tools.workspace import SECTIONS, Workspace


class Memory:
    """Revision-aware test double; production always uses the cloud boundary."""
    project_id = '11111111-1111-4111-8111-111111111111'

    def __init__(self, root):
        self.root = root
        self.records = {}
        self.requests = {}

    def session(self):
        return {'record_id': '22222222-2222-4222-8222-222222222222'}

    def read(self, identifier, revision=None, *, include_archived=False):
        if identifier not in self.records:
            raise MemoryClientError('record_unavailable')
        versions = self.records[identifier]
        if revision is not None and revision > len(versions):
            raise MemoryClientError('record_unavailable')
        return deepcopy(versions[-1] if revision is None else versions[revision - 1])

    def write(self, kind, data, *, record_id=None, expected_revision=0, tags=None, reason=None):
        identifier = record_id or str(uuid.uuid4())
        request = (identifier, expected_revision, json.dumps(
            {'kind': kind, 'data': data, 'tags': tags or [], 'reason': reason}, sort_keys=True))
        if request in self.requests:
            return deepcopy(self.requests[request])
        versions = self.records.setdefault(identifier, [])
        if len(versions) != expected_revision:
            raise MemoryClientError('revision_conflict')
        record = dict(record_id=identifier, revision=len(versions) + 1,
                      kind=kind, data=deepcopy(data), tags=tags or [])
        versions.append(record)
        self.requests[request] = deepcopy(record)
        return deepcopy(record)

    @staticmethod
    def ref(record):
        return {key: record[key] for key in ('record_id', 'revision')}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.memory = Memory(self.root)
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
        self.assertNotIn('synthetic private original', json.dumps(self.memory.records))

    def test_pending_does_not_open_source_content(self):
        self.source('.maven/incoming/item.txt', 'unread fixture')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('raw read')):
            result = self.workspace.pending()
        self.assertEqual(result['pending']['files'], 1)
        self.assertFalse(result['contents_opened'])

    def test_status_excludes_internal_fields_and_preserves_unowned_output(self):
        record = self.memory.write('setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'internal': 'synthetic private detail',
            'publishable': {section: 'Approved update.' for section in SECTIONS}}))
        result = self.workspace.status(record_id=record['record_id'], revision=1)
        self.assertNotIn('synthetic private detail', json.dumps(result))
        target = self.source('team-status.md', 'unrelated bytes')
        with self.assertRaises(BoundaryError):
            self.workspace.status(record_id=record['record_id'], revision=1, output='team-status.md')
        self.assertEqual(target.read_text(), 'unrelated bytes')

    def test_cloud_plan_archive_preserves_identity_and_history(self):
        plan = self.memory.write('note', dict(title='Plan', body_markdown='Completed.', category='plan'))
        result = self.workspace.archive_plan(record_id=plan['record_id'], revision=1,
                                             confirm=True, completed=True)
        self.assertEqual(result['plan']['record_id'], plan['record_id'])
        self.assertEqual(self.memory.read(plan['record_id'])['data']['category'], 'archived-plan')
        self.assertIn('archived', self.memory.read(plan['record_id'])['tags'])
        self.assertEqual(self.memory.read(plan['record_id'], 1)['data']['category'], 'plan')
        self.assertFalse((self.root / '.maven/planning').exists())

    def test_operations_resume_every_cloud_effect_with_original_identity(self):
        for action, writes in (('intake', 4), ('archive_plan', 5), ('status', 3)):
            for fail_at in range(1, writes + 1):
                for after in (False, True):
                    with self.subTest(action=action, fail_at=fail_at, after=after), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        memory = Memory(root)
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
                            plan = memory.write('note', dict(title='Plan', body_markdown='Completed.', category='plan'))
                            arguments = dict(record_id=plan['record_id'], revision=1, confirm=True, completed=True)
                        else:
                            record = memory.write('setting', dict(key='project-status', scope='project', value={
                                'external_status_enabled': True, 'publishable': {section: 'Approved.' for section in SECTIONS}}))
                            arguments = dict(record_id=record['record_id'], revision=1, output='team-status.md')
                        write = memory.write
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
                        with patch.object(memory, 'write', side_effect=interrupted), self.assertRaises(OSError):
                            getattr(workspace, action)(**arguments, operation_id=operation_id)
                        result = getattr(Workspace(memory), action)(**arguments, operation_id=operation_id)
                        self.assertEqual(result['operation_id'], operation_id)
                        self.assertEqual(memory.read(operation_id)['data']['state'], 'completed')
                        self.assertEqual(len(memory.records[operation_id]), 2)
                        before = deepcopy(memory.records)
                        self.assertEqual(getattr(Workspace(memory), action)(**arguments, operation_id=operation_id), result)
                        self.assertEqual(memory.records, before)
                        if action == 'intake':
                            self.assertFalse(source.exists())
                        elif action == 'archive_plan':
                            self.assertIn('archived', memory.read(plan['record_id'])['tags'])
                        else:
                            self.assertIn('Approved.', (root / 'team-status.md').read_text())

    def test_status_new_output_never_overwrites_racing_editor(self):
        record = self.memory.write('setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'publishable': {section: 'Approved.' for section in SECTIONS}}))
        import os
        link = os.link
        def competing_writer(source, target):
            Path(target).write_text('external editor')
            return link(source, target)
        with patch('tools.workspace.os.link', side_effect=competing_writer), self.assertRaises(FileExistsError):
            self.workspace.status(record_id=record['record_id'], revision=1, output='team-status.md')
        self.assertEqual((self.root / 'team-status.md').read_text(), 'external editor')
        with self.assertRaises(BoundaryError):
            self.workspace.status(record_id=record['record_id'], revision=1, output='team-status.md')

    def test_kernel_lock_persists_and_legacy_lock_requires_quiescence(self):
        from tools.workspace import workspace_lock
        from tools.coordination import private_lock
        with workspace_lock(self.root):
            with self.assertRaises(BoundaryError):
                with private_lock(self.root, '.maven/runtime/cloud-workspace-v2.lock', blocking=False):
                    self.fail('concurrent lock acquired')
        with workspace_lock(self.root):
            pass
        legacy = self.root / '.maven/runtime/cloud-workspace.lock'
        legacy.touch(mode=0o600)
        with self.assertRaises(BoundaryError):
            with workspace_lock(self.root):
                self.fail('legacy lock silently adopted')
        with workspace_lock(self.root, writers_quiescent=True):
            pass
        self.assertFalse(legacy.exists())
        self.assertTrue((legacy.parent / 'cloud-workspace-legacy.json').exists())

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
                    plan = self.memory.write('note', dict(title='Plan', body_markdown='Done.', category='plan'))
                    arguments = dict(record_id=plan['record_id'], revision=1, confirm=True, completed=True,
                                     source_path=relative, source_disposition='archive')
                phase = self.workspace._phase
                def interrupt(path, journal, state, **kwargs):
                    if state == 'removed':
                        raise OSError('interrupted after unlink')
                    return phase(path, journal, state, **kwargs)
                with patch.object(self.workspace, '_phase', side_effect=interrupt), self.assertRaises(OSError):
                    getattr(self.workspace, action)(**arguments)
                self.assertFalse(source.exists())
                journals = list((self.root / '.maven/runtime/workspace').glob('*.json'))
                original = next(json.loads(path.read_text()) for path in journals
                                if json.loads(path.read_text())['name'] == action.replace('_', '-'))
                result = getattr(Workspace(self.memory), action)(**arguments, operation_id=original['operation_id'])
                self.assertEqual(result['operation_id'], original['operation_id'])
                self.assertEqual(self.memory.read(original['operation_id'])['revision'], 2)

    def test_status_existing_output_resumes_after_rename_before_receipt(self):
        import os
        record = self.memory.write('setting', dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'publishable': {section: 'First.' for section in SECTIONS}}))
        self.workspace.status(record_id=record['record_id'], revision=1, output='team-status.md')
        data = deepcopy(record['data'])
        data['value']['publishable']['Progress'] = 'Second.'
        self.memory.write('setting', data, record_id=record['record_id'], expected_revision=1)
        replace = os.replace
        def interrupted(source, target):
            replace(source, target)
            if Path(target).name == 'team-status.md':
                raise OSError('interrupted after rename')
        with patch('tools.workspace.os.replace', side_effect=interrupted), self.assertRaises(OSError):
            self.workspace.status(record_id=record['record_id'], revision=2, output='team-status.md')
        result = Workspace(self.memory).status(record_id=record['record_id'], revision=2, output='team-status.md')
        self.assertTrue(result['current'])
        self.assertIn('Second.', (self.root / 'team-status.md').read_text())

    def test_receipt_diagnosis_does_not_connect_or_register_writer(self):
        import io
        from contextlib import redirect_stdout
        from tools.workspace import main
        self.source('.maven/incoming/item', 'original')
        result = self.workspace.intake(path='.maven/incoming/item', kind='reference', origin='fixture',
                                       sensitivity='Sensitive', authorization='reviewed', disposition='retain')
        with patch('tools.memory.environment.configured_client', side_effect=AssertionError('cloud connection')), \
                patch('tools.coordination.SessionRegistry.writer', side_effect=AssertionError('writer registration')), \
                redirect_stdout(io.StringIO()) as output:
            code = main(['--root', str(self.root), '--session', self.memory.session()['record_id'],
                         'receipt', '--operation-id', result['operation_id']])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['operation']['phase'], 'completed')


if __name__ == '__main__':
    unittest.main()
