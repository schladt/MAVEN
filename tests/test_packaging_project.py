"""Local configuration and installation boundaries; no live cloud operations."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from maven_resources import resource_path
from tools.project import (
    ProjectError, _initialize, default_environment, load_project, project_values, validate_accepted_url,
)


class ProjectPackagingTests(unittest.TestCase):
    def test_resources_are_contained(self):
        self.assertTrue(resource_path('templates/environment.example').is_file())
        self.assertTrue(resource_path('templates/harness/startup.md').is_file())
        self.assertFalse((resource_path('templates/harness') / 'maven-turn.js').exists())
        skills = resource_path('templates/skills')
        self.assertEqual(list(skills.rglob('SKILL.md')), [skills / 'maven/SKILL.md'])
        router = (skills / 'maven/SKILL.md').read_text()
        for name in ('project', 'reconcile', 'secrets', 'update'):
            relative = f'references/{name}.md'
            self.assertIn(relative, router)
            self.assertTrue((skills / 'maven' / relative).read_text().strip())
        for value in ('../pyproject.toml', '/etc/passwd', ''):
            with self.assertRaises(ValueError):
                resource_path(value)

    def test_installed_wheel_provisions_complete_bundle_outside_checkout(self):
        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name in ('pyproject.toml', 'README.md'):
                shutil.copy2(checkout / name, source / name)
            for name in ('maven_resources', 'tools'):
                shutil.copytree(checkout / name, source / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            pip = [sys.executable, '-m', 'pip', '--disable-pip-version-check']
            subprocess.run([*pip, 'wheel', '--no-deps', '--no-index', '--no-build-isolation',
                            '--wheel-dir', str(root), str(source)], cwd=root, check=True)
            wheel, = root.glob('*.whl')
            subprocess.run([*pip, 'install', '--no-index', '--no-deps', '--target', 'installed',
                            str(wheel)], cwd=root, check=True)
            shutil.rmtree(source)
            subprocess.run([sys.executable, '-I', '-c', '''
from pathlib import Path
import sys
sys.path.insert(0, str(Path('installed').resolve()))
from maven_resources import resource_path
from tools.harness import skill_plan, write_skill
packaged = resource_path('templates/skills')
assert packaged.is_relative_to(Path('installed').resolve())
assert not (resource_path('templates/harness') / 'maven-turn.js').exists()
from importlib.util import find_spec
assert all(find_spec('tools.' + name) is None for name in ('turn_hook', 'turn_native', 'turn_runtime'))
root = Path('custom profile/skills').resolve()
for target, body, expected in skill_plan([root]):
    write_skill(target, body, expected)
files = {'maven/SKILL.md', *('maven/references/' + name + '.md'
                             for name in ('project', 'reconcile', 'secrets', 'update'))}
for tree in (packaged, root):
    assert {p.relative_to(tree).as_posix() for p in tree.rglob('*') if p.is_file()} == files
for name in files:
    assert (root / name).read_bytes() == (packaged / name).read_bytes()
    if name != 'maven/SKILL.md':
        assert name.removeprefix('maven/') in (root / 'maven/SKILL.md').read_text()
'''], cwd=root, check=True)

    def test_git_destinations_cannot_embed_credentials(self):
        for value in (
            'https://example.com/repo.git', 'ssh://developer@example.com:22/repo.git',
            'developer@example.com:repo.git', 'git@[::1]:repo.git',
            '/tmp/remote.git', './remote.git', '../remote.git', 'file:///tmp/remote.git',
        ):
            self.assertEqual(validate_accepted_url(value), value)
        for value in (
            'https://user:password@example.com/repo.git', 'https://git@example.com/repo.git',
            'https://example.com/repo.git?signature=example', 'https://example.com/repo.git#fragment',
            'ssh://user:password@example.com/repo.git', 'user:password@example.com:repo.git',
            'ssh://user%3Apassword@example.com/repo.git', 'file://user:password@localhost/repo.git',
            'https://example.com:invalid/repo.git',
        ):
            with self.assertRaisesRegex(ProjectError, '^invalid Git destination$'):
                validate_accepted_url(value)

    def test_instance_home_never_selects_project(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MAVEN_HOME': directory}):
                self.assertEqual(default_environment(), Path(directory) / '.env')
            values = project_values({'MAVEN_PROJECT_ID': 'old', 'MAVEN_GIT_ACCEPTED_URL': 'old'},
                                    {'project_id': 'selected', 'git_enabled': False})
            self.assertEqual(values, {'MAVEN_PROJECT_ID': 'selected'})

    def test_fresh_project_creation_and_rerun_preserve_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.gitignore').write_text('existing-pattern')
            args = argparse.Namespace(root=root, check=[], project_id=str(uuid4()), no_git=True,
                                      remote=None, accepted_url=None, branch=None, create=True,
                                      name='Example project', source_origin='Owner request',
                                      source_excerpt='Create this project', request_key=str(uuid4()),
                                      env=root / 'instance.env')
            client = Mock()
            client.invoke.return_value = {'status': 'ready', 'data': {
                'record_id': args.project_id, 'kind': 'entity', 'data_scope': 'owner',
                'project_id': None, 'data': {'entity_type': 'project'}}}
            config = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4())}
            with patch('tools.memory.environment.load_environment', return_value={}), \
                    patch('tools.memory.environment.memory_config', return_value=config), \
                    patch('tools.memory.reconcile_cloud.status') as signed_status, \
                    patch('tools.memory.environment.configured_client', return_value=client) as factory:
                result = _initialize(args)
                self.assertEqual(result['status'], 'initialized')
                self.assertEqual(client.invoke.call_args_list[0].args[1]['request_key'], args.request_key)
                signed_status.assert_called_once_with(client)
                client.close.assert_called_once()
                before = (root / '.maven/project.json').read_bytes()
                self.assertEqual(_initialize(args)['status'], 'unchanged')
                factory.assert_called_once_with(args.env, values={})
                self.assertEqual((root / '.maven/project.json').read_bytes(), before)
                self.assertEqual((root / '.gitignore').read_text(), 'existing-pattern\n/.maven/\n')
                args.project_id = str(uuid4())
                with self.assertRaises(ProjectError):
                    _initialize(args)
                self.assertEqual((root / '.maven/project.json').read_bytes(), before)
            project = load_project(root)
            self.assertEqual(project['root'], str(root))
            self.assertEqual((root / '.maven/project.json').stat().st_mode & 0o777, 0o600)

    def test_initializer_requires_exact_active_universal_project_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = argparse.Namespace(root=root, check=[], project_id=str(uuid4()),
                                      remote=None, accepted_url=None, branch=None, create=False,
                                      name=None, source_origin=None, source_excerpt=None, request_key=None,
                                      env=root / 'instance.env')
            record = {'record_id': args.project_id, 'kind': 'entity', 'data_scope': 'owner',
                      'project_id': None, 'tags': [], 'data': {'entity_type': 'project'}}
            client = Mock()
            config = {'instance_id': str(uuid4()), 'recovery_epoch': str(uuid4())}
            with patch('tools.memory.environment.load_environment', return_value={}), \
                    patch('tools.memory.environment.memory_config', return_value=config), \
                    patch('tools.memory.reconcile_cloud.status'), \
                    patch('tools.memory.environment.configured_client', return_value=client):
                for changed in (
                        {**record, 'record_id': str(uuid4())},
                        {**record, 'record_id': None}, {**record, 'project_id': str(uuid4())},
                        {**record, 'data_scope': 'public'}, {**record, 'tags': ['archived']},
                        {**record, 'data': {'entity_type': 'project', 'state': 'archived'}}):
                    client.invoke.return_value = {'status': 'ready', 'data': changed}
                    with self.subTest(record=changed), self.assertRaises(ProjectError):
                        _initialize(args)
                    self.assertFalse((root / '.maven/project.json').exists())
                client.invoke.return_value = {'status': 'ready', 'data': record}
                self.assertEqual(_initialize(args)['status'], 'initialized')

    def test_project_rejects_symlink_and_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = root / '.maven'
            private.mkdir(mode=0o700)
            other = root / 'other.json'
            other.write_text('{}')
            target = private / 'project.json'
            target.symlink_to(other)
            with self.assertRaises(ProjectError):
                load_project(root)
            target.unlink()
            target.write_text('{}')
            target.chmod(0o644)
            with self.assertRaises(ProjectError):
                load_project(root)

    def test_skill_conflicts_are_preserved(self):
        from tools.harness import skill_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'maven/SKILL.md'
            target.parent.mkdir()
            target.write_text('existing owner instructions')
            with self.assertRaises(ValueError):
                skill_plan([root])
            self.assertEqual(target.read_text(), 'existing owner instructions')


if __name__ == '__main__':
    unittest.main()
