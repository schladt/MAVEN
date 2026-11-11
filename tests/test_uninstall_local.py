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

    def apply_before_purge(self, fragment):
        return self.apply({'actions': [item for item in fragment['actions'] if item['kind'] != 'local_home_purge']})

    def purge(self, fragment):
        action = next(item for item in fragment['actions'] if item['kind'] == 'local_home_purge')
        return local.execute(action, {}, lambda: None)

    def test_machine_deletes_installation_folder_but_not_source_or_external_targets(self):
        runtime, external = self.runtime()
        environment = self.home / '.env'
        environment.write_text('PRIVATE=value\n')
        environment.chmod(0o600)
        self.assertIn(str(self.home), local.discover_homes())
        fragment = local.plan_machine(self.home, offline=True)
        self.assertTrue(any('No export directory' in warning for warning in fragment['warnings']))
        self.assertFalse(any(item['path'] == str(environment) for item in fragment['retained']))
        self.apply(fragment)
        self.assertFalse(self.home.exists())
        self.assertEqual(external.read_text(), 'external interpreter must survive')
        self.assertTrue((self.source / 'source.py').exists())
        self.assertNotIn(str(self.home), local.discover_homes())
        # A later installation into the same path starts without a removal barrier.
        self.home.mkdir(mode=0o700)
        with local._lock(self.home / 'runtime/registry.lock'):
            self.assertIsNone(local.reinstall_marker(self.home))

    def test_installation_folder_purge_refuses_changes_and_resumes(self):
        self.runtime()
        (self.home / '.env').write_text('PRIVATE=value\n')
        fragment = local.plan_machine(self.home, offline=True)
        self.apply_before_purge(fragment)
        added = self.home / 'runtime/added-after-review'
        added.write_text('keep')
        with self.assertRaisesRegex(UninstallError, 'changed since review'):
            self.purge(fragment)
        self.assertTrue((self.home / '.env').exists())
        added.unlink()
        (self.home / '.env').unlink()  # Interrupted purge: some reviewed entries already gone.
        self.purge(fragment)
        self.assertFalse(self.home.exists())
        self.purge(fragment)

    def test_installation_folder_without_receipt_is_kept(self):
        (self.home / '.env').write_text('PRIVATE=value\n')
        fragment = local.plan_machine(self.home, offline=True)
        self.assertFalse(any(item['kind'] == 'local_home_purge' for item in fragment['actions']))
        self.assertTrue(any('is kept because no installer ownership receipt' in warning
                            for warning in fragment['warnings']))
        self.apply(fragment)
        self.assertTrue((self.home / '.env').exists())

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
        identifier = '00000000-0000-4000-8000-000000000009'
        session = {'root': str(self.base / 'project'), 'state': 'paused', 'writers': []}
        save_private(other / 'runtime/sessions.json', {'schema': 1, 'sessions': {identifier: session},
                                                       'reservations': {}})
        idle = local.plan_machine(other)
        barrier = next(item for item in idle['actions'] if item['kind'] == 'local_barrier')
        self.assertTrue(barrier['details']['detach_sessions'])
        self.assertTrue(any('will be detached' in warning for warning in idle['warnings']))
        save_private(other / 'runtime/sessions.json', {'schema': 1, 'sessions': {
            identifier: {**session, 'writers': ['in-flight']}}, 'reservations': {}})
        with self.assertRaisesRegex(UninstallError, 'in-flight writers'):
            local.plan_machine(other)
        # A writer registered after review invalidates the idle detachment.
        with self.assertRaisesRegex(UninstallError, 'in-flight writers'):
            local.execute(barrier, {}, lambda: None)
        save_private(other / 'runtime/sessions.json', {'schema': 1, 'sessions': {identifier: session},
                                                       'reservations': {}})
        self.apply_before_purge(idle)
        self.assertEqual(read_private(other / 'runtime/sessions.json')['sessions'], {})
        evidence = read_private(next((other / 'runtime').glob('uninstall-sessions-*.json')))
        self.assertEqual(evidence['sessions'][identifier]['state'], 'detached')
        self.purge(idle)
        self.assertFalse(other.exists())

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
        self.apply_before_purge(combined)
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
        self.purge(combined)
        self.assertFalse(self.home.exists())
        self.assertTrue(Path(sensitive_export['details']['destination']).exists())

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

    def integration(self):
        import json
        target = self.base / 'harness.json'
        value = {'command': str(self.home / 'venv/bin/maven-mcp')}
        target.write_text(json.dumps({'mcpServers': {'maven': value, 'other': {'command': 'keep'}}}))
        target.chmod(0o600)
        receipt = {'path': str(target), 'format': 'json-entry', 'key': 'maven',
                   'value': value, 'created': False, 'created_container': False}
        local.record_installation(self.home, self.source, [], [], integrations=[receipt])
        return target, receipt

    def test_integration_removal_preserves_unrelated_configuration_and_resumes(self):
        import json
        target, receipt = self.integration()
        fragment = local.plan_machine(self.home, offline=True)
        barrier = next(item for item in fragment['actions'] if item['kind'] == 'local_barrier')
        action = next(item for item in fragment['actions'] if item['kind'] == 'local_integration')
        local.execute(barrier, {}, lambda: None)
        progress = {}
        result = local.execute(action, progress, lambda: None)
        self.assertEqual(result['status'], 'removed')
        self.assertEqual(json.loads(target.read_text()), {'mcpServers': {'other': {'command': 'keep'}}})
        self.assertEqual(local.execute(action, progress, lambda: None)['status'], 'removed')
        self.assertEqual(read_private(self.home / local.MANIFEST)['integrations'], [receipt])

    def test_integration_changed_after_review_is_not_removed(self):
        import json
        target, _ = self.integration()
        fragment = local.plan_machine(self.home, offline=True)
        barrier = next(item for item in fragment['actions'] if item['kind'] == 'local_barrier')
        action = next(item for item in fragment['actions'] if item['kind'] == 'local_integration')
        local.execute(barrier, {}, lambda: None)
        document = json.loads(target.read_text())
        document['new_owner_setting'] = True
        target.write_text(json.dumps(document))
        with self.assertRaisesRegex(UninstallError, 'changed after review'):
            local.execute(action, {}, lambda: None)
        self.assertEqual(json.loads(target.read_text()), document)

    def test_shared_integration_is_retained_for_unselected_installation(self):
        target, receipt = self.integration()
        other = self.base / 'other-home'
        other.mkdir(mode=0o700)
        local.record_installation(other, self.source, [], [], integrations=[receipt])
        fragment = local.plan_machine(self.home, offline=True)
        self.assertFalse(any(item['kind'] == 'local_integration' for item in fragment['actions']))
        self.assertTrue(any(item['path'] == str(target) for item in fragment['retained']))

    def test_identical_reference_receipt_does_not_adopt_or_lose_existing_ownership(self):
        target, receipt = self.integration()
        reference = {**receipt, 'owned': False}
        local.record_installation(self.home, self.source, [], [], integrations=[reference])
        self.assertEqual(read_private(self.home / local.MANIFEST)['integrations'], [receipt])
        other = self.base / 'reference-only'
        other.mkdir(mode=0o700)
        local.record_installation(other, self.source, [], [], integrations=[reference])
        fragment = local.plan_machine(other, offline=True)
        self.assertFalse(any(item['kind'] == 'local_integration' for item in fragment['actions']))
        self.assertTrue(target.exists())


if __name__ == '__main__':
    unittest.main()
