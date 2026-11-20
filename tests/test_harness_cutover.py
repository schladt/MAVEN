"""Local-only harness bootstrap and selective retirement of previously installed lifecycle hooks."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.harness import (
    HARNESS_PATHS, LIFECYCLE_PATHS, apply_integration, apply_reviewed_integration, integration_plan,
    validate_default_profiles,
)
from tools.uninstall.common import read_private
from tools.uninstall.local import MANIFEST, record_installation


class HarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.user = Path(self.directory.name).resolve()
        self.home = self.user / 'instance with spaces'
        self.home.mkdir(mode=0o700)
        self.source = Path(__file__).resolve().parents[1]
        self.home_patch = patch.object(Path, 'home', return_value=self.user)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def apply(self, harnesses, **options):
        changes = integration_plan(self.home, harnesses, user_home=self.user, **options)
        for change in changes:
            apply_reviewed_integration(self.home, self.source, change)
        return changes

    def legacy(self, harness, *, modified=False):
        """Seed an old installation receipt, independent of today's configuration planner."""
        path = self.user / LIFECYCLE_PATHS[harness]
        path.parent.mkdir(parents=True, exist_ok=True)
        if harness == 'omp':
            value = '// old shutdown extension'
            path.write_text(value + ('\n// owner edit' if modified else ''))
            fmt = 'file'
        else:
            start, end = ('sessionStart', 'sessionEnd') if harness == 'copilot' else ('SessionStart', 'SessionEnd')
            value = {start: [{'command': 'old startup'}], end: [{'command': 'old shutdown'}]}
            document = {'version': 1, 'theme': 'owner',
                        'hooks': {start: [*value[start], {'command': 'keep'}], end: value[end]}}
            if modified:
                document['hooks'][end] = [{'command': 'owner replacement'}]
            path.write_text(json.dumps(document))
            fmt = 'json-hooks'
        path.chmod(0o600)
        record_installation(self.home, self.source, [], [], integrations=[{
            'path': str(path), 'format': fmt, 'key': 'maven-lifecycle', 'harness': harness,
            'target': 'lifecycle', 'value': value, 'created': harness == 'omp'}])
        return path

    def test_all_harnesses_use_local_orientation_without_hooks(self):
        changes = self.apply(HARNESS_PATHS)
        self.assertFalse(any(change.receipt.get('conflict') for change in changes))
        for name, (_, context, _) in HARNESS_PATHS.items():
            with self.subTest(harness=name):
                text = (self.user / context).read_text()
                self.assertIn('maven_orient', text)
                self.assertIn('maven_local_write', text)
                self.assertIn('references/reconcile.md', text)
                self.assertNotIn('maven-reconcile', text)
                self.assertNotIn('maven_startup', text)
                target = self.user / LIFECYCLE_PATHS[name]
                if name == 'gemini':
                    self.assertNotIn('hooks', json.loads(target.read_text()))
                else:
                    self.assertFalse(target.exists())

    def test_cutover_removes_exact_owned_hooks_and_keeps_owner_entries(self):
        for name in HARNESS_PATHS:
            self.legacy(name)
        changes = self.apply(HARNESS_PATHS, statusline=True)
        self.assertFalse(any(change.receipt.get('conflict') for change in changes))
        self.assertEqual(sum(bool(c.receipt.get('retired')) for c in changes), len(HARNESS_PATHS))
        for name, relative in LIFECYCLE_PATHS.items():
            path = self.user / relative
            if name == 'omp':
                self.assertFalse(path.exists())
                continue
            document = json.loads(path.read_text())
            start = 'sessionStart' if name == 'copilot' else 'SessionStart'
            self.assertEqual(document['hooks'], {start: [{'command': 'keep'}]})
            self.assertEqual(document['theme'], 'owner')
            if name == 'claude':
                self.assertIn('statusLine', document)
            if name == 'gemini':
                self.assertIn('maven', document['mcpServers'])
        self.assertNotIn('maven-lifecycle', [r['key'] for r in read_private(self.home / MANIFEST)['integrations']])
        self.assertTrue(all(change.before == change.after for change in self.apply(HARNESS_PATHS, statusline=True)))

    def test_modified_or_shared_hooks_survive_upgrade(self):
        modified = self.legacy('omp', modified=True)
        shared = self.legacy('codex')
        before = [modified.read_bytes(), shared.read_bytes()]
        with patch('tools.uninstall.local._integration_references', return_value=({str(shared)}, False)):
            changes = self.apply(['omp', 'codex'])
        self.assertEqual({c.path for c in changes if c.receipt.get('conflict')}, {modified, shared})
        self.assertEqual(before, [modified.read_bytes(), shared.read_bytes()])

    def test_retirement_rechecks_bytes_after_review(self):
        path = self.legacy('omp')
        plan = integration_plan(self.home, ['omp'], user_home=self.user)
        retirement = next(c for c in plan if c.receipt.get('retired'))
        path.write_text('owner changed this after review')
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            apply_reviewed_integration(self.home, self.source, retirement)
        self.assertEqual(path.read_text(), 'owner changed this after review')

    def test_staging_does_not_retire_live_hook(self):
        path = self.legacy('omp')
        changes = integration_plan(self.home, ['omp'], user_home=self.user / 'inactive')
        self.assertFalse(any(c.path == path for c in changes))
        self.assertTrue(path.exists())

    def test_no_owner_memory_or_session_lifecycle_entrypoints(self):
        import tomllib
        package = tomllib.loads((self.source / 'pyproject.toml').read_text())
        scripts = set(package['project']['scripts'])
        self.assertFalse(scripts & {'maven-memory', 'maven-session-hook', 'maven-workspace', 'maven-start-work',
                                    'maven-checkpoint', 'maven-stop-work', 'maven-workflow'})
        self.assertTrue({'maven-setup', 'maven-mcp', 'maven-secrets', 'maven-uninstall'} <= scripts)

    def test_owned_guidance_refresh_preserves_profile_trust_and_retires_maintenance_skill(self):
        from tools.harness import retired_skill_plan, retire_skills, skill_plan
        from tools.update import apply_owned_skills
        self.apply(['claude'], ca_bundle=self.user / 'owner-ca.pem', guidance='Previous startup')
        context = self.user / HARNESS_PATHS['claude'][1]
        context.write_text('Owner context\n' + context.read_text())
        config = (self.user / HARNESS_PATHS['claude'][0]).read_bytes()
        self.apply(['claude'])
        self.assertEqual((self.user / HARNESS_PATHS['claude'][0]).read_bytes(), config)
        self.assertTrue(context.read_text().startswith('Owner context\n'))
        self.assertIn('maven_orient', context.read_text())
        self.assertNotIn('Previous startup', context.read_text())
        root = self.user / HARNESS_PATHS['claude'][2]
        old = root / 'maven-maintenance/SKILL.md'
        old.parent.mkdir(parents=True)
        old.write_text('obsolete owned skill')
        old.chmod(0o600)
        record_installation(self.home, self.source, [old], [old])
        retiring = retired_skill_plan(self.home, [root])
        writes = skill_plan([root], self.home)
        apply_owned_skills(self.home, self.source, writes[:-1], [path for path, _, _ in writes[:-1]])
        with self.assertRaises(ValueError):
            retire_skills(self.home, retiring)
        self.assertTrue(old.exists())
        target = writes[-1][0]
        apply_owned_skills(self.home, self.source, writes[-1:], [target])
        with patch('tools.uninstall.local._shared_skills', return_value=({str(target)}, False)):
            with self.assertRaises(ValueError):
                retire_skills(self.home, retiring)
        self.assertTrue(old.exists())
        retire_skills(self.home, retiring)
        self.assertFalse(old.exists())
        receipt = read_private(self.home / MANIFEST)
        self.assertNotIn(str(old), receipt['owned'])
        self.assertNotIn(str(old), receipt['skill_references'])

    def test_custom_install_refresh_carries_recorded_paths_not_defaults(self):
        import argparse
        import install
        custom = self.user / 'inactive profile'
        self.apply(['codex'], paths={'codex': (custom / 'config.toml', custom / 'guidance.md')})
        args = argparse.Namespace(harness=[], skill_dir=[], stage_harnesses=None)
        install._update_targets(args, self.home)
        self.assertEqual(args.harness_config, ['codex=' + str(custom / 'config.toml')])
        self.assertEqual(args.harness_context, ['codex=' + str(custom / 'guidance.md')])
        self.assertFalse((self.user / '.codex').exists())

    def test_custom_profile_requires_explicit_configuration(self):
        for harness, variable in (('omp', 'OMP_PROFILE'), ('claude', 'CLAUDE_CONFIG_DIR'),
                                  ('codex', 'CODEX_HOME'), ('copilot', 'COPILOT_HOME'), ('gemini', 'GEMINI_CLI_HOME')):
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: 'custom'}), self.assertRaises(ValueError):
                validate_default_profiles([harness])

    def test_conflicting_harness_does_not_suppress_other_selected_harness(self):
        path = self.user / HARNESS_PATHS['claude'][0]
        original = json.dumps({'mcpServers': {'maven': {'command': 'owner-command'}}})
        path.write_text(original)
        path.chmod(0o600)
        changes = integration_plan(self.home, ['claude', 'omp'], user_home=self.user)
        self.assertTrue(any(change.receipt.get('conflict') for change in changes))
        for change in changes:
            apply_integration(change)
        self.assertEqual(path.read_text(), original)
        self.assertTrue((self.user / HARNESS_PATHS['omp'][0]).exists())

    def test_recorded_custom_destinations_are_exact_and_legacy_unknowns_never_default(self):
        from tools.harness import installed_targets
        from tools.uninstall.common import save_private
        config, context = self.user / 'custom/mcp.json', self.user / 'custom/policy.txt'
        changes = integration_plan(self.home, ['claude'], paths={'claude': (config, context)})
        for change in changes:
            record_installation(self.home, self.source, [], [], integrations=[change.receipt])
            apply_integration(change)
        recorded = installed_targets(self.home)
        self.assertEqual(recorded['paths'], {'claude': (config, context)})
        self.assertEqual(recorded['unresolved'], [])
        manifest = read_private(self.home / MANIFEST)
        for item in manifest['integrations']:
            item.pop('harness', None)
            item.pop('target', None)
        save_private(self.home / MANIFEST, manifest)
        recorded = installed_targets(self.home)
        self.assertEqual(recorded['paths'], {})
        self.assertEqual(set(recorded['unresolved']), {str(config), str(context)})
        self.assertFalse((self.user / '.claude.json').exists())

    def test_owned_legacy_status_command_migrates_without_adopting_owner_changes(self):
        from tools.harness import _statusline_change
        target = self.user / '.claude/settings.json'
        target.parent.mkdir()
        old = {'type': 'command', 'command': str(self.home / 'venv/bin/maven-statusline')}
        target.write_text(json.dumps({'statusLine': old, 'theme': 'owner'}))
        target.chmod(0o600)
        record_installation(self.home, self.source, [], [], integrations=[{
            'path': str(target), 'format': 'json-setting', 'key': 'statusLine',
            'value': old, 'created': False}])
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)):
            change = _statusline_change(self.home, target)
        self.assertIn('MAVEN_HOME=', change.receipt['value']['command'])
        apply_integration(change)
        self.assertEqual(json.loads(target.read_text())['theme'], 'owner')
        target.write_text(json.dumps({'statusLine': {'command': 'owner-command'}}))
        self.assertTrue(_statusline_change(self.home, target).receipt.get('conflict'))

    def test_tui_upgrades_legacy_layouts_and_second_review_is_noop(self):
        from maven_resources import resource_path
        from tools.tui.harness import configure_harnesses
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        roots = {self.user / value[2] for value in HARNESS_PATHS.values()} | {self.user / 'custom skills'}
        previous = []
        for root in roots:
            for name in ('maven', 'maven-reconcile', 'maven-update', 'maven-secrets', 'maven-maintenance'):
                target = root / name / 'SKILL.md'
                target.parent.mkdir(parents=True)
                target.write_text('installer-owned ' + name)
                target.chmod(0o600)
                previous.append(target)
            (root / 'maven-update/owner-note').write_text('not installer-owned')
        record_installation(self.home, self.source, previous, previous)
        ui = Mock()
        ui.form.return_value = dict.fromkeys(HARNESS_PATHS, True)
        ui.review.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
            self.assertEqual(result['status'], 'written', result)
            for root in roots:
                self.assertEqual(list(root.rglob('SKILL.md')), [root / 'maven/SKILL.md'])
                for leaf in ('SKILL.md', 'references/project.md', 'references/reconcile.md',
                             'references/secrets.md', 'references/update.md'):
                    self.assertEqual((root / 'maven' / leaf).read_bytes(),
                                     resource_path('templates/skills/maven/' + leaf).read_bytes())
                self.assertEqual((root / 'maven-update/owner-note').read_text(), 'not installer-owned')
            self.assertEqual(configure_harnesses(ui, self.home)['status'], 'unchanged')
            ui.review.assert_called_once()

    def test_skill_conflict_names_the_file_and_preserves_its_bytes(self):
        from tools.harness import skill_plan
        root = self.user / 'custom skills'
        target = root / 'maven/SKILL.md'
        target.parent.mkdir(parents=True)
        target.write_text('owner skill')
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            skill_plan([root], self.home)
        conflicts = []
        plan = skill_plan([root], self.home, conflicts=conflicts)
        self.assertEqual([item['target'] for item in conflicts], [str(target)])
        self.assertEqual(plan, [])
        self.assertEqual(target.read_text(), 'owner skill')

    def test_tui_reviews_and_reconciles_pending_skill_receipt_even_when_bytes_match(self):
        import base64
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.common import save_private
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        ui = Mock()
        ui.form.return_value = {name: name == 'omp' for name in HARNESS_PATHS}
        ui.review.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            configure_harnesses(ui, self.home)
        target = self.user / HARNESS_PATHS['omp'][2] / 'maven/SKILL.md'
        pending = self.home / 'runtime/skill-installations/original.json'
        save_private(pending, {'schema': 1, 'home': str(self.home), 'source': str(self.source),
                              'target': str(target), 'body': base64.b64encode(target.read_bytes()).decode(),
                              'expected': None, 'completed': False, 'skill_reference': True})
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
        self.assertEqual(ui.review.call_count, 2)
        self.assertIn('Resume exact journaled local file installation', ui.review.call_args.args[1])
        self.assertTrue(read_private(pending)['completed'])
        self.assertIn({'target': str(target), 'status': 'reconciled'}, result['targets'])



if __name__ == '__main__':
    unittest.main()
