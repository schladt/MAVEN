"""Temporary-directory local uninstall consumers; no cloud or package installs."""
import copy
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.project import _ensure_ignore, _record_integration, load_project, ProjectError
from tools.uninstall import local
from tools.uninstall.common import UninstallError, read_private, save_private


class LocalUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / 'instance'
        self.home.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {'HOME': str(self.base), 'MAVEN_HOME': str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = self.base / 'checkout'
        self.source.mkdir()
        (self.source / '.git').mkdir()
        (self.source / 'source.py').write_text('untouched source\n')

    def runtime(self):
        runtime = self.home / 'venv'
        runtime.mkdir()
        (runtime / 'bin').mkdir()
        external = self.base / 'system-python'
        external.write_text('external interpreter must survive')
        (runtime / 'bin/python').symlink_to(external)
        (runtime / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        local.record_installation(self.home, self.source, [runtime], [])
        return runtime, external

    def project(self, name='project'):
        root = self.base / name
        root.mkdir()
        (root / '.maven').mkdir(mode=0o700)
        save_private(root / '.maven/project.json', {
            'schema': 1, 'project_id': 'd9a12c41-67cf-4d8a-a70f-29bbf25f99e6',
            'root': str(root), 'git_enabled': False, 'remote': None,
            'accepted_url': None, 'branch': None, 'checks': []})
        ignore = _ensure_ignore(root)
        _record_integration(root, (root / '.maven/project.json').read_bytes(), ignore)
        pending = root / '.maven/pending-memory.json'
        save_private(pending, {'unresolved': True})
        return root, pending

    def apply(self, fragment):
        return [local.execute(action, {}, lambda: None)
                for action in sorted(fragment['actions'], key=lambda value: value['stage'])]

    def test_machine_removes_owned_symlink_tree_not_source_or_settings(self):
        runtime, external = self.runtime()
        environment = self.home / '.env'
        environment.write_text('PRIVATE=value\n')
        environment.chmod(0o600)
        self.assertIn(str(self.home), local.discover_homes())
        self.apply(local.plan_machine(self.home, offline=True))
        self.assertFalse(runtime.exists())
        self.assertEqual(external.read_text(), 'external interpreter must survive')
        self.assertEqual(environment.read_text(), 'PRIVATE=value\n')
        self.assertTrue((self.source / 'source.py').exists())
        self.assertTrue(read_private(self.home / 'runtime/uninstall.json')['whole_home'])
        self.assertNotIn(str(self.home), local.discover_homes())
        self.assertIn(str(self.home), local.discover_homes([self.home]))
        with local._lock(self.home / 'runtime/registry.lock'):
            completed = local.reinstall_marker(self.home)
            self.runtime()
            local.finish_reinstallation(self.home, completed)
        self.assertFalse((self.home / 'runtime/uninstall.json').exists())
        self.assertIn(str(self.home), local.discover_homes())

    def test_inventory_change_invalidates_approval_and_interrupted_removal_resumes(self):
        runtime, _ = self.runtime()
        fragment = local.plan_machine(self.home, offline=True)
        barrier = next(item for item in fragment['actions'] if item['kind'] == 'local_barrier')
        removal = next(item for item in fragment['actions'] if item['kind'] == 'local_remove')
        local.execute(barrier, {}, lambda: None)
        extra = runtime / 'unexpected'
        extra.write_text('preserve')
        with self.assertRaises(UninstallError):
            local.execute(removal, {}, lambda: None)
        self.assertTrue(extra.exists())
        extra.unlink()
        progress, durable = {}, {}

        def interrupt_after_effect():
            if progress.get('cursor'):
                raise RuntimeError('simulated lost journal acknowledgement')
            durable.clear()
            durable.update(copy.deepcopy(progress))

        with self.assertRaises(RuntimeError):
            local.execute(removal, progress, interrupt_after_effect)
        local.execute(removal, durable, lambda: None)
        self.assertFalse(runtime.exists())

    def test_project_detach_preserves_receipts_ignore_and_other_home(self):
        root, pending = self.project()
        before_ignore = (root / '.gitignore').read_bytes()
        project = load_project(root)
        absent_home = self.base / 'already-removed-home'
        fragment = local.plan_project(root, absent_home, offline=True)
        self.apply(fragment)
        with self.assertRaises(ProjectError):
            load_project(root)
        detached = list((root / '.maven').glob('detached-project-*.json'))
        self.assertEqual(len(detached), 1)
        self.assertEqual(read_private(detached[0])['project_id'], project['project_id'])
        self.assertEqual((root / '.gitignore').read_bytes(), before_ignore)
        self.assertTrue(pending.exists())
        self.assertEqual(read_private(absent_home / 'runtime/uninstall.json')['project_roots'], [])
        self.assertFalse((self.home / 'runtime').exists())
        self.assertTrue(read_private(root / '.maven/integration.json')['integrations'])

    def test_export_is_private_verified_copy_and_partial_write_resumes(self):
        root, pending = self.project()
        original = (root / '.maven/project.json').read_bytes()
        destination = self.base / 'recovery'
        fragment = local.plan_project(root, self.home, offline=True, export=destination)
        actions = sorted(fragment['actions'], key=lambda value: value['stage'])
        local.execute(actions[0], {}, lambda: None)
        export = actions[1]
        destination.mkdir(mode=0o700)
        output = Path(export['details']['destination'])
        partial = output.with_suffix('.partial')
        partial.write_bytes(b'interrupted ZIP header')
        partial.chmod(0o600)
        progress = {'intent': 'export'}
        result = local.execute(export, progress, lambda: None)
        self.assertTrue(result['originals_retained'])
        self.assertTrue((root / '.maven/project.json').exists())
        self.assertTrue(pending.exists())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('data/content'), original)
        self.assertEqual(local.execute(export, progress, lambda: None)['sha256'], result['sha256'])
        for action in actions[2:]:
            local.execute(action, {}, lambda: None)
        self.assertFalse((root / '.maven/project.json').exists())
        self.assertEqual(len(list((root / '.maven').glob('detached-project-*.json'))), 1)

    def test_shared_skills_and_active_sessions_are_protected(self):
        skill = self.base / 'skills/maven-connect/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('installer skill')
        other = self.base / 'other-instance'
        other.mkdir(mode=0o700)
        local.record_installation(self.home, self.source, [skill], [skill])
        local.record_installation(other, self.source, [], [skill])
        fragment = local.plan_machine(self.home, offline=True)
        self.apply(fragment)
        self.assertTrue(skill.exists())
        self.assertTrue(any(item['path'] == str(skill) for item in fragment['retained']))
        (other / 'runtime').mkdir(mode=0o700)
        save_private(other / 'runtime/sessions.json', {'schema': 1, 'sessions': {
            'session': {'root': str(self.base / 'project'), 'state': 'paused', 'writers': []}},
            'reservations': {}})
        with self.assertRaises(UninstallError):
            local.plan_machine(other)

    def test_combined_offline_export_locks_and_multiple_roots_preserve_evidence(self):
        runtime, _ = self.runtime()
        root, pending = self.project()
        second, resolved = self.project('second-project')
        resolved.unlink()
        sensitive = second / '.maven/sensitive'
        sensitive.mkdir(mode=0o700)
        original = sensitive / 'original.bin'
        original.write_bytes(b'owner original')
        original.chmod(0o600)
        environment = self.home / '.env'
        environment.write_text('PRIVATE=value\n')
        environment.chmod(0o600)
        sessions = {}
        for index, checkout in enumerate((root, second), 1):
            identifier = f'00000000-0000-4000-8000-{index:012d}'
            sessions[identifier] = {'root': str(checkout), 'state': 'active',
                                    'writers': ['exact-in-flight-token'] if index == 1 else [], 'caches': []}
            save_private(checkout / '.maven/runtime/sessions' / identifier / 'command.lock', {})
        save_private(self.home / 'runtime/sessions.json',
                     {'schema': 1, 'sessions': sessions, 'reservations': {}})
        destination = self.base / 'recovery'
        combined = local.plan_project(root, self.home, offline=True, export=destination)
        combined['actions'].extend(local.plan_project(second, self.home, offline=True,
                                                      export=destination)['actions'])
        combined['actions'].extend(local.plan_machine(self.home, offline=True,
            selected_homes=[self.home], export=destination)['actions'])
        first = next(item for item in combined['actions'] if item['kind'] == 'local_barrier')
        identifier = next(iter(sessions))
        with local._lock(root / '.maven/runtime/sessions' / identifier / 'command.lock'):
            with self.assertRaises(UninstallError):
                local.execute(first, {}, lambda: None)
        self.apply(combined)
        self.assertFalse(runtime.exists())
        self.assertFalse(environment.exists())
        self.assertFalse(sensitive.exists())
        self.assertTrue(pending.exists())
        self.assertEqual(read_private(self.home / 'runtime/sessions.json')['sessions'], {})
        receipts = list((self.home / 'runtime').glob('uninstall-sessions-*.json'))
        archived = [item for path in receipts for item in read_private(path)['sessions'].values()]
        self.assertEqual(len(archived), 2)
        self.assertTrue(all(item['state'] == 'detached' and item['access_revoked'] for item in archived))
        self.assertTrue(read_private(self.home / 'runtime/uninstall.json')['whole_home'])
        sensitive_export = next(item for item in combined['actions'] if item['target'] == str(sensitive))
        with zipfile.ZipFile(sensitive_export['details']['destination']) as archive:
            self.assertEqual(archive.read('data/original.bin'), b'owner original')

    def test_explicit_legacy_adoption_validates_real_metadata_and_entrypoints(self):
        runtime = self.home / 'venv'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'pyvenv.cfg').write_text('home = /usr/bin\ninclude-system-site-packages = false\n')
        metadata = runtime / 'lib/python3.12/site-packages/maven_cloud_memory-0.1.0.dist-info'
        metadata.mkdir(parents=True)
        (metadata / 'METADATA').write_text('Name: maven-cloud-memory\nVersion: 0.1.0\n')
        bindings = {'maven-memory': 'tools.memory.cli:main', 'maven-project': 'tools.project:main',
                    'maven-workflow': 'tools.workflow:main'}
        (metadata / 'entry_points.txt').write_text('[console_scripts]\n' + ''.join(
            f'{name} = {binding}\n' for name, binding in bindings.items()))
        for name, binding in bindings.items():
            module, function = binding.split(':')
            (runtime / 'bin' / name).write_text(
                f'#!{runtime}/bin/python\nfrom {module} import {function}\n')
        ordinary = local.plan_machine(self.home, offline=True)
        self.assertFalse(any(item['kind'] == 'local_remove' for item in ordinary['actions']))
        self.apply(local.plan_machine(self.home, offline=True, adopt_legacy=True))
        self.assertFalse(runtime.exists())
        self.assertTrue((self.source / 'source.py').exists())

    def test_owned_cache_is_removed_only_without_other_installation_references(self):
        (self.home / 'runtime').mkdir(mode=0o700)
        cache = self.home / 'runtime/aws-login-cache'
        cache.mkdir(mode=0o700)
        local.record_installation(self.home, self.source, [], [], cache_roots=[cache])
        save_private(cache / 'token.json', {'private_credential': 'never put this value in a plan'})
        other = self.base / 'other-instance'
        other.mkdir(mode=0o700)
        local.record_installation(other, self.source, [], [])
        environment = other / '.env'
        environment.write_text('MAVEN_AWS_LOGIN_CACHE=' + str(cache) + '\n')
        environment.chmod(0o600)
        shared = local.plan_machine(self.home, offline=True)
        self.assertFalse(any(item['target'] == str(cache) for item in shared['actions']))
        self.assertTrue(any(item['path'] == str(cache) for item in shared['retained']))
        environment.write_text('# no shared cache\n')
        exclusive = local.plan_machine(self.home, offline=True)
        self.assertNotIn('never put this value', str(exclusive))
        results = self.apply(exclusive)
        self.assertFalse(cache.exists())
        self.assertTrue(other.exists())
        self.assertTrue(all(not item.get('token_revocation_claimed', False) for item in results))


if __name__ == '__main__':
    unittest.main()
