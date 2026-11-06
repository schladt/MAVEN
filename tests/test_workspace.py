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

    def session(self):
        return {'record_id': '22222222-2222-4222-8222-222222222222'}

    def read(self, identifier, revision=None):
        if identifier not in self.records:
            raise MemoryClientError('record_unavailable')
        versions = self.records[identifier]
        return deepcopy(versions[-1] if revision is None else versions[revision - 1])

    def write(self, kind, data, *, record_id=None, expected_revision=0, tags=None, reason=None):
        identifier = record_id or str(uuid.uuid4())
        versions = self.records.setdefault(identifier, [])
        if len(versions) != expected_revision:
            raise MemoryClientError('revision_conflict')
        record = dict(record_id=identifier, revision=len(versions) + 1,
                      kind=kind, data=deepcopy(data), tags=tags or [])
        versions.append(record)
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
        self.assertEqual(self.memory.read(plan['record_id'], 1)['data']['category'], 'plan')
        self.assertFalse((self.root / '.maven/planning').exists())


if __name__ == '__main__':
    unittest.main()
