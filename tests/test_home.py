"""Owner-approved local Home selection and pinned identity."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from tools.memory.boundary import save_private
from tools.home import HomeError, load_home, save_home, validate_root
from tools.setup_common import save_json

CONSENT = {'origin': 'Owner conversation', 'excerpt': 'Use nexus as my MAVEN Home.'}
INSTANCE, EPOCH = str(uuid.uuid4()), str(uuid.uuid4())


def enable(root, project_id=None):
    root.mkdir(parents=True, exist_ok=True)
    project_id = project_id or str(uuid.uuid4())
    save_private(root, '.maven/project.json', {'schema': 2, 'root': str(root.resolve()), 'project_id': project_id,
                                               'instance_id': INSTANCE, 'recovery_epoch': EPOCH})
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
        (self.home / '.env').unlink()
        self.assertEqual(load_home(self.home), saved)  # No credentials or SDK lookup needed.

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
        original = (self.home / 'home.json').read_bytes()
        with self.assertRaises(HomeError):
            self.save(project_id=str(uuid.uuid4()))
        self.assertEqual((self.home / 'home.json').read_bytes(), original)

    def test_invalid_replacement_preserves_previous_valid_selection(self):
        previous = self.save()
        before = (self.home / 'home.json').read_bytes()
        for changes in ({'project_id': str(uuid.uuid4())}, {'instance_id': 'different-instance'},
                        {'consent': {'origin': '', 'excerpt': 'invalid'}}):
            with self.subTest(changes=changes), self.assertRaises(HomeError):
                self.save(**changes)
            self.assertEqual((self.home / 'home.json').read_bytes(), before)
            self.assertEqual(load_home(self.home), previous)

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




if __name__ == '__main__':
    unittest.main()
