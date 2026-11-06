"""Local configuration and installation boundaries; no live cloud operations."""
import argparse
import os
from pathlib import Path
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
        for value in ('../pyproject.toml', '/etc/passwd', ''):
            with self.assertRaises(ValueError):
                resource_path(value)

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
            client.invoke.return_value = {'data': {'kind': 'entity', 'data_scope': 'owner',
                                                   'data': {'entity_type': 'project'}}}
            with patch('tools.memory.environment.configured_client', return_value=client) as factory:
                result = _initialize(args)
                self.assertEqual(result['status'], 'initialized')
                self.assertEqual(client.invoke.call_args_list[0].args[1]['request_key'], args.request_key)
                client.close.assert_called_once()
                before = (root / '.maven/project.json').read_bytes()
                self.assertEqual(_initialize(args)['status'], 'unchanged')
                factory.assert_called_once_with(args.env)
                self.assertEqual((root / '.maven/project.json').read_bytes(), before)
                self.assertEqual((root / '.gitignore').read_text(), 'existing-pattern\n/.maven/\n')
                args.project_id = str(uuid4())
                with self.assertRaises(ProjectError):
                    _initialize(args)
                self.assertEqual((root / '.maven/project.json').read_bytes(), before)
            project = load_project(root)
            self.assertEqual(project['root'], str(root))
            self.assertEqual((root / '.maven/project.json').stat().st_mode & 0o777, 0o600)

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
        import install
        source = Path(install.__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'maven-setup/SKILL.md'
            target.parent.mkdir()
            target.write_text('existing owner instructions')
            with self.assertRaises(ValueError):
                install._skill_plan(source, [root])
            self.assertEqual(target.read_text(), 'existing owner instructions')


if __name__ == '__main__':
    unittest.main()
