"""MAVEN Home selection, start-choice log and start hint (local files only)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tools import home as maven_home
from tools.home import (HomeError, last_choice, load_home, record_choice, save_home, start_hint,
                        validate_root)
from tools.memory.boundary import save_private
from tools.setup_common import save_json

CONSENT = {'origin': 'Owner conversation', 'excerpt': 'Use nexus as my MAVEN Home.'}
INSTANCE = 'instance-one'


def enable(root, project_id=None):
    root.mkdir(parents=True, exist_ok=True)
    project_id = project_id or str(uuid.uuid4())
    save_private(root, '.maven/project.json', {'schema': 1, 'root': str(root.resolve()), 'project_id': project_id})
    return project_id


def instance_home(path, instance=INSTANCE):
    path.mkdir(mode=0o700, exist_ok=True)
    (path / '.env').write_text(f'MAVEN_MEMORY_INSTANCE_ID="{instance}"\n')
    (path / '.env').chmod(0o600)
    return path


class HomeSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = instance_home(self.base / 'maven-home')
        self.root = self.base / 'nexus'
        self.project = enable(self.root)

    def save(self, **overrides):
        values = dict(root=self.root, project_id=self.project, instance_id=INSTANCE, label='nexus', consent=CONSENT)
        values.update(overrides)
        return save_home(self.home, **values)

    def rewrite(self, **changes):
        record = json.loads((self.home / 'home.json').read_text())
        record.update(changes)
        save_json(self.home / 'home.json', record)

    def test_valid_selection_round_trips_and_is_private(self):
        self.assertIsNone(load_home(self.home))
        saved = self.save()
        self.assertEqual(oct((self.home / 'home.json').stat().st_mode & 0o777), '0o600')
        loaded = load_home(self.home)
        self.assertEqual(loaded, saved)
        self.assertEqual((loaded['root'], loaded['project_id'], loaded['label']),
                         (str(self.root), self.project, 'nexus'))

    def test_permissions_and_symlink_are_invalid(self):
        self.save()
        (self.home / 'home.json').chmod(0o644)
        self.assertIn('invalid', load_home(self.home))
        (self.home / 'home.json').chmod(0o600)
        target = self.base / 'elsewhere.json'
        os.replace(self.home / 'home.json', target)
        (self.home / 'home.json').symlink_to(target)
        self.assertEqual(load_home(self.home), {'invalid': 'home.json must not be a symlink'})

    def test_schema_label_and_consent_are_required(self):
        self.save()
        for changes in ({'schema': 2}, {'label': '  '}, {'consent': None}, {'consent': {'origin': '', 'excerpt': ''}}):
            with self.subTest(changes=changes):
                self.rewrite(**changes)
                self.assertIn('invalid', load_home(self.home))
                (self.home / 'home.json').unlink()
                self.save()

    def test_project_and_instance_must_match(self):
        self.save()
        self.rewrite(project_id=str(uuid.uuid4()))
        self.assertEqual(load_home(self.home), {'invalid': 'Home folder belongs to a different MAVEN project'})
        self.rewrite(project_id=self.project, instance_id='instance-two')
        self.assertEqual(load_home(self.home), {'invalid': 'Home belongs to a different MAVEN instance'})
        with self.assertRaises(HomeError):
            self.save(project_id=str(uuid.uuid4()))
        self.assertFalse((self.home / 'home.json').exists())

    def test_root_must_be_outside_installation_and_checkout(self):
        inside = self.home / 'projects' / 'inner'
        inside.mkdir(parents=True)
        with self.assertRaises(HomeError):
            validate_root(self.home, inside)
        checkout = self.base / 'checkout'
        (checkout / 'docs').mkdir(parents=True)
        save_json(self.home / 'installation.json', {'source': str(checkout)})
        for rejected in (checkout, checkout / 'docs', Path('relative'), Path('/'), Path.home(), self.base / 'missing'):
            with self.subTest(root=rejected), self.assertRaises(HomeError):
                validate_root(self.home, rejected)
        self.assertEqual(validate_root(self.home, self.root), self.root)
        self.save()
        enable(checkout / 'docs', self.project)
        self.rewrite(root=str(checkout / 'docs'))
        self.assertIn('invalid', load_home(self.home))


class StartChoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = instance_home(self.base / 'maven-home')
        self.folder = self.base / 'scratch'
        self.folder.mkdir()
        self.log = self.home / 'runtime' / 'start-choices.jsonl'

    def test_choices_append_privately_and_last_choice_is_per_folder(self):
        other = self.base / 'other'
        other.mkdir()
        self.assertIsNone(last_choice(self.home, self.folder))
        record_choice(self.home, self.folder, 'home', harness='omp', harness_session='s1')
        record_choice(self.home, other, 'off')
        record_choice(self.home, self.folder, 'read_only')
        self.assertEqual(oct(self.log.stat().st_mode & 0o777), '0o600')
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([entry['choice'] for entry in entries], ['home', 'off', 'read_only'])
        self.assertEqual((entries[0]['harness'], entries[0]['harness_session']), ('omp', 's1'))
        self.assertEqual(last_choice(self.home, self.folder), 'read_only')
        self.assertEqual(last_choice(self.home, other), 'off')
        with self.assertRaises(HomeError):
            record_choice(self.home, self.folder, 'everything')
        self.log.chmod(0o644)
        with self.assertRaises(HomeError):
            record_choice(self.home, self.folder, 'off')

    def test_log_rotates_and_keeps_previous_history(self):
        record_choice(self.home, self.folder, 'home')
        with patch.object(maven_home, 'CHOICE_LOG_LIMIT', 10):
            record_choice(self.home, self.base, 'off')
        rotated = self.log.with_name('start-choices.jsonl.1')
        self.assertEqual(json.loads(rotated.read_text())['choice'], 'home')
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(last_choice(self.home, self.folder), 'home')
        self.assertEqual(last_choice(self.home, self.base), 'off')

    def test_start_hint_inside_project_and_with_or_without_home(self):
        enabled = self.base / 'enabled'
        project = enable(enabled)
        (enabled / 'src').mkdir()
        self.assertIsNone(start_hint(self.home, enabled / 'src'))
        hint = start_hint(self.home, self.folder)
        self.assertEqual((hint['mode'], hint['options'], hint['default']), ('choose', ['project', 'read_only'], 'read_only'))
        self.assertEqual(hint['folder'], str(self.folder))
        self.assertNotIn('home_invalid', hint)
        self.assertIsNone(hint['last_choice'])
        save_home(self.home, root=enabled, project_id=project, instance_id=INSTANCE, label='nexus', consent=CONSENT)
        record_choice(self.home, self.folder, 'home')
        hint = start_hint(self.home, self.folder)
        self.assertEqual(hint['options'], ['home', 'project', 'read_only'])
        self.assertEqual((hint['label'], hint['project_id'], hint['last_choice']), ('nexus', project, 'home'))
        self.assertIn('last time here: home', hint['question'])
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID="instance-two"\n')
        hint = start_hint(self.home, self.folder)
        self.assertEqual(hint['options'], ['project', 'read_only'])
        self.assertIn('different MAVEN instance', hint['home_invalid'])


if __name__ == '__main__':
    unittest.main()
