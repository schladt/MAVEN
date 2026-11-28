"""Local update inspection and installer-owned skill upgrades in temporary homes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import update
from tools.harness import skill_plan, write_skill
from tools.uninstall.common import read_private, save_private
from tools.uninstall import local


def _record_hash(data):
    return 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {'HOME': str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.checkout = self.base / 'checkout'
        for relative, text in {'install.py': '', 'tools/__init__.py': '', 'tools/kept.py': 'same\n',
                               'tools/changed.py': 'new\n', 'tools/added.py': 'added\n',
                               'maven_resources/__init__.py': '',
                               'maven_resources/templates/skills/maven/SKILL.md': 'skill v2\n'}.items():
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.checkout / 'pyproject.toml').write_text(
            '[tool.setuptools.package-data]\nmaven_resources = ["templates/skills/maven/SKILL.md", '
            '"templates/skills/maven/references/*.md"]\n')
        installed = {'tools/__init__.py': b'', 'tools/kept.py': b'same\n', 'tools/changed.py': b'old\n',
                     'tools/removed.py': b'gone\n', 'maven_resources/__init__.py': b'',
                     'maven_resources/templates/skills/maven/SKILL.md': b'skill v2\n'}
        for name in ('project', 'reconcile', 'secrets', 'update'):
            relative = f'maven_resources/templates/skills/maven/references/{name}.md'
            path = self.checkout / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text(name + ' procedure\n')
            installed[relative] = path.read_bytes()
        dist = self.home / 'venv/lib/python3.14/site-packages/maven_cloud_memory-0.1.0.dist-info'
        dist.mkdir(parents=True)
        self.dist = dist
        for name, data in installed.items():
            target = dist.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b'entrypoint\n')
        (dist / 'METADATA').write_text('Name: maven-cloud-memory\nVersion: 0.1.0\n')
        (dist / 'entry_points.txt').write_text('[console_scripts]\nmaven-mcp = tools.mcp.server:main\n')
        (dist / 'RECORD').write_text(''.join(f'{name},{_record_hash(data)},{len(data)}\n'
                                             for name, data in installed.items()) +
                                     f'../../../bin/maven-mcp,{_record_hash(launcher.read_bytes())},11\n'
                                     'tools/__pycache__/x.pyc,,\n')
        update.record_package_inputs(self.home, self.checkout)

    def test_report_lists_changed_added_and_removed_software(self):
        report = update.local_report(self.home, self.checkout)
        self.assertEqual(report['software'], {'installed': True, 'changed': ['tools/changed.py'],
                                              'added': ['tools/added.py'], 'removed': ['tools/removed.py']})
        self.assertFalse(report['current'])

    def test_report_is_current_when_installed_software_matches(self):
        (self.checkout / 'tools/changed.py').write_text('old\n')
        (self.checkout / 'tools/added.py').unlink()
        (self.checkout / 'tools/removed.py').write_text('gone\n')
        self.assertTrue(update.local_report(self.home, self.checkout)['current'])

    def test_unchanged_record_does_not_hide_deleted_or_changed_installed_bytes(self):
        original_record = (self.dist / 'RECORD').read_bytes()
        target = self.dist.parent / 'tools/kept.py'
        for body in (b'corrupt\n', None):
            with self.subTest(body=body):
                if body is None:
                    target.unlink()
                else:
                    target.write_bytes(body)
                report = update.local_report(self.home, self.checkout)
                self.assertIn('tools/kept.py', report['software']['changed'])
                self.assertIn('tools/kept.py', report['integrity_failures'])
                self.assertFalse(report['current'])
                self.assertEqual((self.dist / 'RECORD').read_bytes(), original_record)

    def test_entrypoint_only_input_or_installed_metadata_change_is_not_current(self):
        pyproject = self.checkout / 'pyproject.toml'
        original = pyproject.read_text()
        pyproject.write_text(original + '\n[project.scripts]\nmaven-new = "tools.new:main"\n')
        self.assertFalse(update.local_report(self.home, self.checkout)['package_metadata_current'])
        pyproject.write_text(original)
        (self.dist / 'entry_points.txt').write_text('[console_scripts]\n')
        self.assertFalse(update.local_report(self.home, self.checkout)['package_metadata_current'])

    def test_missing_entrypoint_and_unrecorded_module_are_reported(self):
        (self.home / 'venv/bin/maven-mcp').unlink()
        (self.dist.parent / 'tools/extra.py').write_text('unexpected\n')
        report = update.local_report(self.home, self.checkout)
        self.assertIn('../../../bin/maven-mcp', report['integrity_failures'])
        self.assertIn('tools/extra.py', report['software']['removed'])

    def test_skill_write_interruption_recovers_original_ownership_before_next_upgrade(self):
        root = self.base / 'custom-skills'
        plan = skill_plan([root], self.home)
        target, body, _ = plan[0]
        with patch.object(local, 'record_installation', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                update.apply_owned_skills(self.home, self.checkout, [plan[0]], [target])
        receipts = list((self.home / 'runtime/skill-installations').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(target.read_bytes(), body)
        update.apply_owned_skills(self.home, self.checkout, [], [target])
        self.assertTrue(read_private(receipts[0])['completed'])
        self.assertIn(str(target), read_private(self.home / local.MANIFEST)['owned'])
        # A subsequent exact owned replacement remains possible.
        expected = local.inventory(target)
        update.apply_owned_skills(self.home, self.checkout, [(target, body + b'\n', expected)], [target])
        self.assertEqual(target.read_bytes(), body + b'\n')

    def test_interrupted_skill_refuses_changed_bytes_without_adoption(self):
        root = self.base / 'custom-skills'
        entry = skill_plan([root], self.home)[0]
        with patch.object(local, 'record_installation', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                update.apply_owned_skills(self.home, self.checkout, [entry], [entry[0]])
        entry[0].write_bytes(b'owner edit')
        with self.assertRaisesRegex(ValueError, 'changed'):
            update.apply_owned_skills(self.home, self.checkout, [], [entry[0]])
        self.assertEqual(entry[0].read_bytes(), b'owner edit')

    def test_interrupted_legacy_skill_intent_is_not_reinstalled(self):
        for index, leaf in enumerate(('maven-reconcile/SKILL.md', 'maven-secrets/SKILL.md',
                                      'maven-update/SKILL.md', 'maven-maintenance/SKILL.md',
                                      'maven/SKILL.md', 'maven/references/reconcile.md')):
            with self.subTest(leaf=leaf):
                target = self.base / 'custom-skills' / leaf
                intent = self.home / f'runtime/skill-installations/previous-{index}.json'
                save_private(intent, {'schema': 1, 'home': str(self.home), 'source': str(self.checkout),
                                      'target': str(target), 'body': base64.b64encode(b'old lifecycle').decode(),
                                      'expected': None, 'completed': False, 'skill_reference': True})
                self.assertIn('retire obsolete', update.pending_owned_skills(self.home)[0]['disposition'])
                update.apply_owned_skills(self.home, self.checkout, [], [])
                self.assertTrue(read_private(intent)['completed'])
                self.assertFalse(target.exists())

    def test_guided_update_uses_the_selected_home_not_ambient_profiles(self):
        from tools.harness import apply_integration, integration_plan
        custom = self.base / 'custom-profile'
        custom.mkdir()
        config, context, skills = custom / 'mcp.json', custom / 'CONTEXT.md', custom / 'skills'
        config.write_text('{"owner":{"keep":true}}\n')
        context.write_text('Owner context\n')
        config.chmod(0o600)
        context.chmod(0o600)
        environment = self.home / '.env'
        environment.write_text('MAVEN_AWS_PROFILE=original\nMAVEN_AWS_LOGIN_CACHE=/original/cache\n')
        environment.chmod(0o600)
        before = environment.read_bytes()
        changes = integration_plan(self.home, ['omp'], user_home=self.base,
                                   paths={'omp': (config, context)})
        references = [skills / 'maven/SKILL.md']
        for change in changes:
            local.record_installation(self.home, self.checkout, [], references, integrations=[change.receipt])
            apply_integration(change)
        command = update.install_command(self.home, self.checkout)
        self.assertEqual(command[1:], [str(self.checkout / 'install.py'), '--home', str(self.home)])
        self.assertNotIn(str(self.base / '.omp/agent/mcp.json'), command)
        self.assertEqual(json.loads(config.read_text())['owner'], {'keep': True})
        self.assertTrue(context.read_text().startswith('Owner context\n'))
        self.assertEqual(environment.read_bytes(), before)

    def test_preserved_skill_difference_does_not_declare_harness_current(self):
        (self.checkout / 'tools/changed.py').write_text('old\n')
        (self.checkout / 'tools/added.py').unlink()
        (self.checkout / 'tools/removed.py').write_text('gone\n')
        target = self.base / 'custom-skills/maven/SKILL.md'
        target.parent.mkdir(parents=True)
        target.write_text('Owner-customized skill\n')
        local.record_installation(self.home, self.checkout, [], [target])
        report = update.local_report(self.home, self.checkout)
        self.assertFalse(report['current'])
        self.assertFalse(report['skills_current'])
        self.assertEqual(report['skills']['stale'], [str(target)])

    def test_report_compares_recorded_nested_procedure_with_checkout_not_loaded_package(self):
        root = self.base / 'custom-skills'
        shipped = self.checkout / 'maven_resources/templates/skills'
        targets = []
        for file in shipped.rglob('*.md'):
            target = root / file.relative_to(shipped)
            write_skill(target, file.read_bytes(), None)
            targets.append(target)
        local.record_installation(self.home, self.checkout, targets, targets)
        self.assertTrue(update.local_report(self.home, self.checkout)['skills_current'])
        relative = 'maven/references/project.md'
        (shipped / relative).write_text('checkout revision\n')
        report = update.local_report(self.home, self.checkout)
        self.assertFalse(report['skills_current'])
        self.assertEqual(report['skills']['stale'], [str(root / relative)])

    def _bundle_with_device_drift(self):
        root = self.base / 'custom-skills'
        shipped = self.checkout / 'maven_resources/templates/skills'
        resources = patch('tools.harness.resource_path', return_value=shipped)
        resources.start()
        self.addCleanup(resources.stop)
        entries = skill_plan([root], self.home)
        outdated = entries[-1][0]
        targets = []
        for target, body, _ in entries:
            write_skill(target, b'old revision\n' if target == outdated else body, None)
            targets.append(target)
        local.record_installation(self.home, self.checkout, targets, targets)
        path = self.home / local.MANIFEST
        manifest = read_private(path)
        for target in targets:
            manifest['owned'][str(target)]['']['dev'] += 1
        save_private(path, manifest)
        return root, targets, outdated

    def test_bundle_and_resume_report_accept_only_device_drift(self):
        root, targets, outdated = self._bundle_with_device_drift()
        # The administration resume check also requires the software to match.
        (self.checkout / 'tools/changed.py').write_text('old\n')
        (self.checkout / 'tools/added.py').unlink()
        (self.checkout / 'tools/removed.py').write_text('gone\n')
        report = update.local_report(self.home, self.checkout)
        self.assertEqual(report['preserved'], [])
        self.assertEqual(report['skills']['stale'], [str(outdated)])
        unchanged = {path: local.inventory(path) for path in targets if path != outdated}
        writes = skill_plan([root], self.home)
        self.assertEqual([path for path, _, _ in writes], [outdated])
        update.install_skill_bundle(self.home, self.checkout, [root], writes, [])
        self.assertEqual(outdated.read_bytes(), writes[0][1])
        self.assertEqual({path: local.inventory(path) for path in unchanged}, unchanged)
        self.assertTrue(update.local_report(self.home, self.checkout)['current'])

    def test_device_drift_does_not_hide_hash_inode_or_type_conflicts(self):
        root, targets, outdated = self._bundle_with_device_drift()
        edited, replaced, changed_type = targets[:3]
        body = edited.read_bytes()
        edited.write_bytes(bytes([body[0] ^ 1]) + body[1:])
        temporary = replaced.with_suffix('.replacement')
        temporary.write_bytes(replaced.read_bytes())
        temporary.chmod(replaced.stat().st_mode & 0o7777)
        temporary.replace(replaced)
        changed_type.unlink()
        changed_type.mkdir()
        before = {path: local.inventory(path) for path in targets}
        conflicts = []
        writes = skill_plan([root], self.home, conflicts=conflicts)
        self.assertEqual(writes, [])
        self.assertEqual({item['target'] for item in conflicts}, {str(path) for path in targets[:3]})
        report = update.local_report(self.home, self.checkout)
        self.assertFalse(report['skills_current'])
        self.assertEqual({item['target'] for item in report['preserved']}, {str(path) for path in targets[:3]})
        self.assertEqual({path: local.inventory(path) for path in targets}, before)
        self.assertEqual(outdated.read_bytes(), b'old revision\n')

    def test_unpublished_skill_intent_rechecks_old_file_after_device_renumber(self):
        root, _, outdated = self._bundle_with_device_drift()
        writes = skill_plan([root], self.home)
        with patch('tools.harness.write_skill', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                update.install_skill_bundle(self.home, self.checkout, [root], writes, [])
        pending, = update.pending_owned_skills(self.home)
        journal = Path(pending['path'])
        intent = read_private(journal)
        intent['expected']['']['dev'] += 1
        save_private(journal, intent)
        update.apply_owned_skills(self.home, self.checkout, [], [outdated])
        self.assertEqual(outdated.read_bytes(), writes[0][1])
        self.assertTrue(read_private(journal)['completed'])

    def test_published_skill_intent_recovers_after_device_renumber(self):
        root, _, outdated = self._bundle_with_device_drift()
        writes = skill_plan([root], self.home)
        with patch.object(local, 'record_installation', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                update.install_skill_bundle(self.home, self.checkout, [root], writes, [])
        pending, = update.pending_owned_skills(self.home)
        journal = Path(pending['path'])
        intent = read_private(journal)
        for field in ('published', 'expected'):
            intent[field]['']['dev'] += 1
        save_private(journal, intent)
        published = local.inventory(outdated)
        resume = skill_plan([root], self.home)
        update.install_skill_bundle(self.home, self.checkout, [root], resume, [])
        self.assertEqual(local.inventory(outdated), published)
        self.assertEqual(read_private(self.home / local.MANIFEST)['owned'][str(outdated)], published)
        self.assertTrue(read_private(journal)['completed'])

    def test_owned_unmodified_skill_is_upgraded_and_modified_skill_is_preserved(self):
        roots = [self.base / '.omp/agent/skills']
        fresh = skill_plan(roots, self.home)
        for target, body, expected in fresh:
            self.assertIsNone(expected)
            write_skill(target, b'old ' + body, None)
        references = [target for target, _, _ in fresh]
        local.record_installation(self.home, self.checkout, references, references)
        plan = skill_plan(roots, self.home)
        self.assertEqual({target for target, _, _ in plan}, set(references))
        self.assertTrue(all(expected is not None for _, _, expected in plan))
        target, body, expected = plan[0]
        write_skill(target, body, expected)
        self.assertEqual(target.read_bytes(), body)
        modified = plan[1][0]
        modified.write_bytes(b'owner edit')
        with self.assertRaisesRegex(ValueError, 'preserved'):
            skill_plan(roots, self.home)
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            write_skill(modified, plan[1][1], plan[1][2])
        self.assertEqual(modified.read_bytes(), b'owner edit')

    def test_owned_guidance_block_is_upgraded_and_edited_block_is_preserved(self):
        from tools.harness import _block, _text_change
        path = self.base / 'AGENTS.md'
        old = '\n' + _block('old guidance')
        path.write_text('# Mine\n' + old)
        path.chmod(0o600)
        prior = {'path': str(path), 'format': 'text-block', 'key': 'maven', 'value': old, 'created': False}
        change = _text_change(path, 'new guidance', prior=prior)
        self.assertEqual(change.after.decode(), '# Mine\n\n' + _block('new guidance'))
        self.assertEqual(change.receipt['value'], '\n' + _block('new guidance'))
        path.write_text('# Mine\n' + old.replace('old guidance', 'owner edit'))
        with self.assertRaisesRegex(ValueError, 'differs'):
            _text_change(path, 'new guidance', prior=prior)


class CloudInspectionTests(unittest.TestCase):
    def test_only_data_resource_removal_or_replacement_is_a_risk(self):
        changes = [
            {'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction', 'ResourceType': 'AWS::Lambda::Function',
             'Replacement': 'True'},
            {'Action': 'Modify', 'LogicalResourceId': 'DatabaseCluster', 'ResourceType': 'AWS::RDS::DBCluster',
             'Replacement': 'False'},
            {'Action': 'Modify', 'LogicalResourceId': 'RecoveryBucket', 'ResourceType': 'AWS::S3::Bucket',
             'Replacement': 'Conditional'},
            {'Action': 'Remove', 'LogicalResourceId': 'AdminSecret', 'ResourceType': 'AWS::SecretsManager::Secret'},
        ]
        document = {'review': {'change_set': {'Changes': [{'ResourceChange': change} for change in changes]}}}
        self.assertEqual([row['logical_id'] for row in update.stateful_risks(document)],
                         ['RecoveryBucket', 'AdminSecret'])

