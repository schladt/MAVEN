"""Private artifact boundaries exercised with real local records."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tools.memory.boundary import BoundaryError, save_private
from tools.reconcile_local import LocalStore
from tools.reconcile_records import TaxonomyError, make_change
from tools.workspace import SECTIONS, Workspace, WorkspaceMemory


def memory_at(root):
    return WorkspaceMemory(LocalStore(root, str(uuid.uuid4())))


def seed(memory, kind, data, record_id=None, *, legacy=False):
    record_id = record_id or str(uuid.uuid4())
    token, previous = memory.snapshot(record_id)
    envelope = make_change(previous, kind, data, 'Owner-reviewed fixture', record_id=record_id)
    memory.store.apply([envelope], token, strict_current=not legacy)
    return memory.read(record_id)


def seed_status(memory, text='Approved.', **extra):
    value = dict(external_status_enabled=True, publishable={section: text for section in SECTIONS}, **extra)
    return seed(memory, 'entity', dict(type='workspace-metadata', name='project-status', scope='project', value=value))


def seed_plan(memory, state='approved'):
    task = seed(memory, 'task', dict(title='Review report', state='completed', added_on='2026-11-27'))
    return seed(memory, 'plan', dict(task_id=task['record_id'], steps=['Read report', 'Sign report'],
                                    state=state, completion_check='Signed report', title='Review'))


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
        self.assertEqual((receipt['kind'], receipt['data']['type']), ('entity', 'workspace-metadata'))
        self.assertEqual(receipt['record_id'], str(uuid.uuid5(uuid.UUID(self.memory.project_id),
                                                              'workspace:' + receipt['data']['name'])))
        _, operation = self.memory.snapshot(result['operation_id'])
        for version, state in ((operation['record'], 'completed'), (operation['history'][0]['record'], 'started')):
            self.assertEqual(version['kind'], 'entity')
            self.assertEqual((version['data']['type'], version['data']['name'], version['data']['state']),
                             ('workspace-operation', 'intake', state))
        self.assertEqual(self.memory.store.context_view(active=True, nexus=False)['preferences'], [])
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
        record = seed_status(self.memory, 'Approved update.', internal='synthetic private detail')
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

    def test_legacy_plan_archive_preserves_identity_and_history(self):
        plan = seed(self.memory, 'plan', dict(title='Plan', body='Completed.', name='Original name',
                                              type='Original type', state='inactive'), legacy=True)
        result = self.workspace.archive_plan(**self.memory.ref(plan), confirm=True, completed=True)
        self.assertEqual(result['plan']['record_id'], plan['record_id'])
        current = self.memory.read(plan['record_id'])
        self.assertEqual(current['kind'], 'entity')
        self.assertEqual(current['data'], dict(type='workspace-archived-plan', name='Plan',
                                               state='archived', plan=plan['data']))
        _, envelope = self.memory.snapshot(plan['record_id'])
        self.assertEqual(envelope['history'][0]['record']['data'], plan['data'])
        self.assertFalse((self.root / '.maven/planning').exists())

    def test_task_plan_archive_marks_done_and_keeps_receipt_separate(self):
        for state in ('approved', 'done'):
            with self.subTest(state=state):
                plan = seed_plan(self.memory, state)
                result = self.workspace.archive_plan(**self.memory.ref(plan), confirm=True, completed=True)
                current = self.memory.read(plan['record_id'])
                self.assertEqual(result['plan'], self.memory.ref(current))
                self.assertEqual((current['kind'], current['data']), ('plan', dict(plan['data'], state='done')))
                receipt = self.memory.read(result['receipt']['record_id'])
                self.assertEqual((receipt['kind'], receipt['data']['type'], receipt['data']['name']),
                                 ('entity', 'workspace-metadata', 'archive-' + plan['record_id']))
                self.assertEqual(receipt['data']['value']['plan'], self.memory.ref(current))
                self.assertEqual(receipt['data']['value']['state'], 'archived')
                with self.assertRaises(BoundaryError):
                    self.workspace.archive_plan(**self.memory.ref(current), confirm=True, completed=True,
                                                operation_id=str(uuid.uuid4()))

    def test_orphaned_task_plan_is_not_archived_as_a_catalog_item(self):
        plan = seed(self.memory, 'plan', dict(task_id=str(uuid.uuid4()), steps=['Check'],
                    state='done', completion_check='Checked'), legacy=True)
        before = self.memory.store.inventory()
        with self.assertRaisesRegex(BoundaryError, 'plan.task_id'):
            self.workspace.archive_plan(**self.memory.ref(plan), confirm=True, completed=True)
        self.assertEqual(self.memory.store.inventory(), before)

    def test_status_pins_exact_entity_and_project_metadata_shape(self):
        record = seed_status(self.memory)
        for kind, data in [('entity', dict(record['data'], type='inventory')),
                           ('entity', dict(record['data'], name='other')),
                           ('entity', dict(record['data'], scope='nexus')),
                           ('entity', dict(record['data'], value=[])),
                           ('setting', dict(key='project-status', scope='project', value=record['data']['value']))]:
            with self.subTest(kind=kind, data=data):
                other = seed(self.memory, kind, data, legacy=kind == 'setting')
                with self.assertRaises(BoundaryError):
                    self.workspace.status(**self.memory.ref(other))
        duplicate = seed(self.memory, 'entity', record['data'])
        with self.assertRaises(BoundaryError):
            self.workspace.status(record_id=duplicate['record_id'], digest=record['digest'])

    def test_workspace_metadata_key_credential_checks_survive_retype(self):
        for name in ('password', 'secret-ref'):
            data = dict(type='workspace-metadata', name=name, scope='project', value='example')
            with self.subTest(name=name), self.assertRaisesRegex(TaxonomyError, 'entity.name'):
                seed(self.memory, 'entity', data)
            # Before v2, entity names were just labels. A prior version must remain readable.
            legacy = seed(self.memory, 'entity', data, legacy=True)
            current = seed(self.memory, 'entity', dict(type='inventory', name='Historical item'), legacy['record_id'])
            self.assertEqual(self.memory.snapshot(current['record_id'])[1]['history'][0]['record']['data'], data)

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
                            plan = seed_plan(memory)
                            arguments = dict(memory.ref(plan), confirm=True, completed=True)
                        else:
                            record = seed_status(memory)
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
                            self.assertEqual(memory.read(plan['record_id'])['data'], dict(plan['data'], state='done'))
                        else:
                            self.assertIn('Approved.', (root / 'team-status.md').read_text())

    def test_status_new_output_never_overwrites_racing_editor(self):
        record = seed_status(self.memory)
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
                    plan = seed_plan(self.memory)
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
        record = seed_status(self.memory, 'First.')
        self.workspace.status(**self.memory.ref(record), output='team-status.md')
        data = deepcopy(record['data'])
        data['value']['publishable']['Progress'] = 'Second.'
        record = seed(self.memory, 'entity', data, record_id=record['record_id'])
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

    def test_v1_archive_journal_resumes_both_sides_of_sqlite_commit(self):
        # A frozen pre-upgrade journal; its planned v1 envelope may or may not be current.
        for phase in ('uncommitted', 'committed', 'conflict'):
            with self.subTest(phase=phase):
                plan = seed(self.memory, 'plan', {'body': 'Completed.'}, legacy=True)
                record_id, operation_id = plan['record_id'], str(uuid.uuid4())
                request = dict(self.memory.ref(plan), confirm=True, completed=True,
                               source_path=None, source_disposition='retain')
                metadata = dict(plan=self.memory.ref(plan), owner_confirmed=True, actual_completion_attested=True,
                                state='archive-authorized', source_disposition='retain',
                                links='Stable DB record ID retained; old revisions remain readable')
                started = seed(self.memory, 'action', dict(name='archive-plan', state='started',
                               summary=json.dumps(metadata, sort_keys=True)), operation_id, legacy=True)
                token, original = self.memory.snapshot(record_id)
                old_archive = make_change(original, 'plan', dict(plan['data'], state='archived'),
                                          'Archive completed plan; retain history', record_id=record_id)
                if phase != 'uncommitted':
                    self.memory.store.apply([old_archive], token, strict_current=False)
                journal = dict(operation_id=operation_id, project_id=self.memory.project_id, name='archive-plan',
                               request=request, phase='preserved', metadata=metadata, plan=plan, initial=None,
                               effects={'started': {'before': None, 'envelope': self.memory.snapshot(operation_id)[1]},
                                        'archive': {'before': plan['digest'], 'envelope': old_archive}})
                save_private(self.root, f'.maven/runtime/workspace/{self.memory.project_id}/{operation_id}.json', journal)
                if phase == 'conflict':
                    changed = seed(self.memory, 'entity', dict(type='inventory', name='Unrelated edit'), record_id)
                    with self.assertRaisesRegex(BoundaryError, 'changed'):
                        self.workspace.archive_plan(**request, operation_id=operation_id)
                    self.assertEqual(self.memory.read(record_id), changed)
                    continue
                result = self.workspace.archive_plan(**request, operation_id=operation_id)
                self.assertEqual(result['operation_id'], operation_id)
                _, archived = self.memory.snapshot(record_id)
                self.assertEqual(archived['record']['data'], dict(type='workspace-archived-plan',
                                 name=record_id, state='archived', plan=plan['data']))
                history = [entry['record'] for entry in archived['history']]
                self.assertIn(original['record'], history)
                self.assertEqual(old_archive['record'] in history, phase == 'committed')
                operation = self.memory.read(started['record_id'])
                self.assertEqual((operation['kind'], operation['data']['state']), ('entity', 'completed'))
                receipt = self.memory.read(result['receipt']['record_id'])
                self.assertEqual(receipt['data']['value']['plan'], self.memory.ref(self.memory.view(archived)))

    def test_converted_status_receipts_prove_ownership_without_authorizing_edits(self):
        # Both the hand-selected source ID and the generated receipt ID predate conversion.
        source_data = dict(key='project-status', scope='project', value={
            'external_status_enabled': True, 'publishable': {s: 'Published.' for s in SECTIONS}})
        source = seed(self.memory, 'setting', source_data, legacy=True)
        published = '# Team Status\n\n' + '\n\n'.join('## ' + s + '\n\nPublished.' for s in SECTIONS) + '\n'
        output = self.source('team-status.md', published)
        operation_id = str(uuid.uuid4())
        metadata = dict(source=self.memory.ref(source), output_path='team-status.md', operation_id=operation_id,
                        output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
        key = 'team-status-receipt'
        old = seed(self.memory, 'setting', dict(key=key, scope='project', value=metadata),
                   self.workspace.metadata_id(key), legacy=True)
        journal = dict(project_id=self.memory.project_id, operation_id=operation_id, name='status', phase='completed',
                       result={'receipt': self.memory.ref(old)})
        save_private(self.root, f'.maven/runtime/workspace/{self.memory.project_id}/{operation_id}.json', journal)
        typed = seed(self.memory, 'entity', dict(type='workspace-metadata', name='project-status', scope='project',
                     value=source_data['value']), source['record_id'])
        receipt = seed(self.memory, 'entity', dict(type='workspace-metadata', name=key, scope='project',
                       value=metadata), old['record_id'])
        args = dict(self.memory.ref(typed), output='team-status.md', check=True)
        self.assertTrue(self.workspace.status(**args)['current'])
        with self.assertRaises(BoundaryError):
            self.workspace.status(**self.memory.ref(source))
        # A second edit retaining the old output hash is not a lossless, one-step conversion.
        seed(self.memory, 'entity', dict(receipt['data'], value=dict(metadata, edited=True)), old['record_id'])
        with self.assertRaisesRegex(BoundaryError, 'collision'):
            self.workspace.status(**args)
        self.assertEqual(output.read_text(), published)

    def test_v1_pending_status_resumes_from_converted_source_and_receipt(self):
        key = 'project-status'
        value = dict(external_status_enabled=True, publishable={s: 'Published.' for s in SECTIONS})
        old = seed(self.memory, 'setting', dict(key=key, scope='project', value=value), legacy=True)
        current = seed(self.memory, 'entity', dict(type='workspace-metadata', name=key, scope='project', value=value),
                       old['record_id'])
        operation_id = str(uuid.uuid4())
        request = dict(self.memory.ref(old), output='team-status.md')
        rendered = '# Team Status\n\n' + '\n\n'.join('## ' + s + '\n\nPublished.' for s in SECTIONS) + '\n'
        self.source('team-status.md', rendered)
        metadata = dict(source=self.memory.ref(old), output_path='team-status.md', operation_id=operation_id,
                        output_sha256=hashlib.sha256(rendered.encode()).hexdigest())
        started = seed(self.memory, 'action', dict(name='status', state='started',
                       summary=json.dumps(metadata, sort_keys=True)), operation_id, legacy=True)
        key = 'team-status-receipt'
        receipt = seed(self.memory, 'setting', dict(key=key, scope='project', value=metadata),
                       self.workspace.metadata_id(key), legacy=True)
        _, old_receipt = self.memory.snapshot(receipt['record_id'])
        seed(self.memory, 'entity', dict(type='workspace-metadata', name=key, scope='project', value=metadata),
             receipt['record_id'])
        journal = dict(project_id=self.memory.project_id, operation_id=operation_id, name='status', request=request,
                       phase='installed', before_sha256=None,
                       effects={'started': {'before': None, 'envelope': self.memory.snapshot(started['record_id'])[1]},
                                'receipt': {'before': None, 'envelope': old_receipt}})
        save_private(self.root, f'.maven/runtime/workspace/{self.memory.project_id}/{operation_id}.json', journal)
        result = self.workspace.status(**request, operation_id=operation_id)
        self.assertTrue(result['current'])
        self.assertEqual(result['source'], self.memory.ref(current))
        self.assertEqual(self.memory.read(operation_id)['data']['type'], 'workspace-operation')
        self.assertEqual(self.memory.read(result['receipt']['record_id'])['data']['type'], 'workspace-metadata')
        self.assertTrue(self.workspace.status(**request, operation_id=operation_id)['current'])

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
