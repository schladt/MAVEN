"""Reviewed lifecycle installation/update/removal contracts; no live harness or cloud."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import install
from maven_resources import resource_path
from tools.harness import (
    HARNESS_PATHS, LIFECYCLE_PATHS, _hook_entries, apply_integration, grouped_removal_content,
    integration_plan, removal_content, remove_integrations, validate_default_profiles,
)
from tools.uninstall.common import read_private
from tools.uninstall.local import MANIFEST, record_installation


class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.user = Path(self.directory.name)
        self.home = self.user / 'instance with spaces'
        self.home.mkdir(mode=0o700)
        self.source = Path(__file__).resolve().parents[1]
        self.home_patch = patch.object(Path, 'home', return_value=self.user)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def apply(self, harnesses, **options):
        changes = integration_plan(self.home, harnesses, user_home=self.user, lifecycle=True, **options)
        for change in changes:
            record_installation(self.home, self.source, [], [], integrations=[change.receipt])
            apply_integration(change)
        return changes

    def test_all_native_schemas_emit_model_visible_startup_via_real_cli(self):
        self.apply(HARNESS_PATHS)
        for harness in HARNESS_PATHS:
            path = self.user / LIFECYCLE_PATHS[harness]
            self.assertTrue(path.is_file())
            if harness == 'omp':
                text = path.read_text()
                self.assertIn(json.dumps(str(self.home)), text)
                self.assertNotIn('__MAVEN_', text)
                continue
            document = json.loads(path.read_text())
            event = 'sessionStart' if harness == 'copilot' else 'SessionStart'
            group = document['hooks'][event][0]
            handler = group if harness == 'copilot' else group['hooks'][0]
            command = shlex.split(handler['bash' if harness == 'copilot' else 'command'])
            self.assertEqual(command[0], str(self.home / 'venv/bin/maven-session-hook'))
            key = 'sessionId' if harness == 'copilot' else 'session_id'
            # Execute the shipped CLI behind the installed entry point, without pip or AWS.
            result = subprocess.run([sys.executable, '-m', 'tools.session_hooks', *command[1:]],
                cwd=self.source, input=json.dumps({key: 'native-7', 'cwd': str(self.user)}),
                text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            context = output['additionalContext'] if harness == 'copilot' else output['hookSpecificOutput']['additionalContext']
            self.assertIn('"harness_session": "native-7"', context)
            self.assertIn('maven_startup', context)
            if harness == 'copilot':
                self.assertEqual(document['version'], 1)
            if harness == 'gemini':
                self.assertEqual(handler['timeout'], 5000)
            else:
                self.assertEqual(handler['timeoutSec' if harness == 'copilot' else 'timeout'], 5)

    def test_shared_json_removal_preserves_owner_settings_and_hooks(self):
        for harness, statusline in (('gemini', False), ('claude', True)):
            with self.subTest(harness=harness):
                path = self.user / LIFECYCLE_PATHS[harness]
                path.parent.mkdir(parents=True, exist_ok=True)
                original = {'theme': 'owner', 'hooks': {'SessionStart': [
                    {'hooks': [{'type': 'command', 'command': 'owner-command'}]}]}}
                path.write_text(json.dumps(original))
                path.chmod(0o600)
                self.apply([harness], statusline=statusline)
                receipts = [item for item in read_private(self.home / MANIFEST)['integrations']
                            if item['path'] == str(path) and item.get('owned', True)]
                self.assertEqual(len(receipts), 2)
                value = json.loads(path.read_text())
                value['owner-added-after-install'] = True
                path.write_text(json.dumps(value))
                expected = dict(original, **{'owner-added-after-install': True})
                self.assertEqual(json.loads(grouped_removal_content(receipts)), expected)
                remove_integrations(receipts)
                self.assertEqual(json.loads(path.read_text()), expected)

    def test_created_shared_settings_file_removed_as_one_reviewed_action(self):
        from tools.uninstall.local import _fragment, _plan_integrations, _remove_integration
        self.apply(['gemini'])
        manifest = read_private(self.home / MANIFEST)
        fragment = _fragment()
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)):
            _plan_integrations(fragment, self.home, manifest, [])
        target = self.user / LIFECYCLE_PATHS['gemini']
        actions = [item for item in fragment['actions'] if item['target'] == str(target)]
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(actions[0]['details']['receipts']), 2)
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)), \
                patch('tools.uninstall.local._sharing_locks', return_value=nullcontext()):
            result = _remove_integration(actions[0], {}, lambda: None)
            self.assertTrue(result['removed'])
            self.assertFalse(target.exists())
            resumed = _remove_integration(actions[0], {}, lambda: None)
            self.assertTrue(resumed['already_at_reviewed_result'])

    def test_repeat_install_preserves_ownership_and_updates_only_exact_owned_assets(self):
        self.apply(['omp', 'codex'])
        original = read_private(self.home / MANIFEST)
        again = self.apply(['omp', 'codex'])
        self.assertTrue(all(change.before == change.after for change in again))
        current = read_private(self.home / MANIFEST)
        self.assertEqual(current['integrations'], original['integrations'])
        extension = resource_path('templates/harness/maven-lifecycle.js').read_text()
        replacement = Mock()
        replacement.read_text.return_value = extension + '\n// Updated native adapter revision\n'
        actual = resource_path
        with patch('tools.harness.resource_path', side_effect=lambda name: replacement if name.endswith('.js') else actual(name)):
            changes = self.apply(['omp'])
        changed = [change for change in changes if change.before != change.after]
        self.assertEqual([change.path for change in changed], [self.user / LIFECYCLE_PATHS['omp']])
        receipt = next(item for item in read_private(self.home / MANIFEST)['integrations'] if item['format'] == 'file')
        self.assertEqual(removal_content(receipt), b'')

    def test_owned_hook_schema_update_preserves_unrelated_entries(self):
        self.apply(['codex'])
        target = self.user / LIFECYCLE_PATHS['codex']
        value = json.loads(target.read_text())
        owner = {'hooks': [{'type': 'command', 'command': 'owner-command'}]}
        value['hooks']['SessionStart'].append(owner)
        target.write_text(json.dumps(value))
        expected = _hook_entries(self.home, 'codex')
        expected['SessionStart'][0]['hooks'][0]['timeout'] = 6
        with patch('tools.harness._hook_entries', return_value=expected):
            self.apply(['codex'])
        current = json.loads(target.read_text())
        self.assertIn(owner, current['hooks']['SessionStart'])
        self.assertIn(expected['SessionStart'][0], current['hooks']['SessionStart'])
        self.assertEqual(len(current['hooks']['SessionStart']), 2)

    def test_modified_hook_or_extension_never_overwritten_or_removed(self):
        self.apply(['omp', 'codex'])
        for harness in ('omp', 'codex'):
            target = self.user / LIFECYCLE_PATHS[harness]
            if harness == 'omp':
                target.write_text(target.read_text() + '\n// owner edit\n')
            else:
                value = json.loads(target.read_text())
                value['hooks']['SessionStart'][0]['hooks'][0]['timeout'] = 99
                target.write_text(json.dumps(value))
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                integration_plan(self.home, [harness], user_home=self.user, lifecycle=True)
            receipt = next(item for item in read_private(self.home / MANIFEST)['integrations'] if item['path'] == str(target))
            with self.assertRaises(ValueError):
                removal_content(receipt)
            self.assertEqual(target.read_bytes(), before)

    def test_custom_profile_refusal_does_not_touch_defaults(self):
        for harness in HARNESS_PATHS:
            with self.subTest(harness=harness), self.assertRaisesRegex(ValueError, 'default or staged'):
                integration_plan(self.home, [harness], user_home=self.user, lifecycle=True,
                    paths={harness: (self.user / 'custom/config', self.user / 'custom/context')})
        self.assertFalse((self.user / '.claude').exists())
        for harness, variable in (('omp', 'OMP_PROFILE'), ('claude', 'CLAUDE_CONFIG_DIR'),
                                  ('codex', 'CODEX_HOME'), ('copilot', 'COPILOT_HOME'), ('gemini', 'GEMINI_CLI_HOME')):
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: 'custom'}), self.assertRaises(ValueError):
                validate_default_profiles([harness])

    def test_staged_installer_reviews_and_records_all_lifecycle_assets(self):
        staged = self.user / 'inactive'
        def create_runtime(path):
            (path / 'bin').mkdir(parents=True)
            (path / 'pyvenv.cfg').write_text('fixture runtime\n')
            (path / 'bin/python').write_text('fixture interpreter\n')
        def install_package(_command):
            for name in ('maven-mcp', 'maven-session-hook'):
                launcher = self.home / 'venv/bin' / name
                launcher.write_text('#!/fixture/python\n')
                launcher.chmod(0o755)
        arguments = ['--local-only', '--home', str(self.home), '--stage-harnesses', str(staged)]
        for harness in HARNESS_PATHS:
            arguments.extend(['--harness', harness])
        with patch.dict(os.environ, {}, clear=True), patch.object(install, '_prerequisites'), \
                patch('venv.EnvBuilder.create', side_effect=create_runtime), \
                patch.object(install.subprocess, 'run', return_value=Mock(returncode=0)), \
                patch.object(install, '_install_command', side_effect=install_package), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(install.main(arguments), 0)
        for relative in LIFECYCLE_PATHS.values():
            self.assertTrue((staged / relative).exists())
            self.assertFalse((self.user / relative).exists())
        manifest = read_private(self.home / MANIFEST)
        for harness, relative in LIFECYCLE_PATHS.items():
            self.assertTrue(any(item['path'] == str(staged / relative)
                                and item['format'] == ('file' if harness == 'omp' else 'json-hooks')
                                for item in manifest['integrations']))

    def test_installer_option_is_explicit_and_disable_does_not_install_hooks(self):
        for option, expected in (('--lifecycle-hooks', True), ('--no-lifecycle-hooks', False)):
            with patch.object(install, '_install', return_value=0) as installer:
                self.assertEqual(install.main(['--local-only', option]), 0)
            self.assertEqual(installer.call_args.args[0].lifecycle_hooks, expected)
        changes = integration_plan(self.home, HARNESS_PATHS, user_home=self.user, lifecycle=False)
        self.assertFalse(any(change.receipt['format'] in ('file', 'json-hooks') for change in changes))

    def test_tui_review_covers_hooks_and_statusline_in_one_settings_file(self):
        from tools.tui.harness import configure_harnesses
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        hook = launcher.with_name('maven-session-hook')
        hook.write_text('#!/fixture/python\n')
        hook.chmod(0o755)
        ui = Mock()
        ui.form.return_value = {**{name: name == 'claude' for name in HARNESS_PATHS},
                                'lifecycle': True, 'statusline': True}
        ui.review.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
        self.assertEqual(result['status'], 'written', result)
        self.assertIn('maven-session-hook', ui.review.call_args.kwargs['details'])
        settings = json.loads((self.user / LIFECYCLE_PATHS['claude']).read_text())
        self.assertIn('statusLine', settings)
        self.assertIn('SessionStart', settings['hooks'])
        self.assertIn('SessionEnd', settings['hooks'])

    def test_unowned_differing_hooks_and_shared_asset_upgrades_are_refused(self):
        target = self.user / LIFECYCLE_PATHS['codex']
        target.parent.mkdir(parents=True)
        entries = _hook_entries(self.home, 'codex')
        entries['SessionStart'][0]['hooks'][0]['timeout'] = 99
        target.write_text(json.dumps({'hooks': entries}))
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            integration_plan(self.home, ['codex'], user_home=self.user, lifecycle=True)
        self.apply(['omp'])
        resource = resource_path
        replacement = Mock()
        replacement.read_text.return_value = resource('templates/harness/maven-lifecycle.js').read_text() + '\n// update\n'
        with patch('tools.harness.resource_path', side_effect=lambda name: replacement if name.endswith('.js') else resource(name)), \
                patch('tools.uninstall.local._integration_references', return_value=(
                    {str(self.user / LIFECYCLE_PATHS['omp'])}, False)), \
                self.assertRaisesRegex(ValueError, 'Shared lifecycle'):
            integration_plan(self.home, ['omp'], user_home=self.user, lifecycle=True)

    def test_grouped_uninstall_refuses_post_review_owner_edits(self):
        from tools.uninstall.local import _fragment, _plan_integrations, _remove_integration
        self.apply(['claude'], statusline=True)
        fragment = _fragment()
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)):
            _plan_integrations(fragment, self.home, read_private(self.home / MANIFEST), [])
        target = self.user / LIFECYCLE_PATHS['claude']
        action = next(item for item in fragment['actions'] if item['target'] == str(target))
        value = json.loads(target.read_text())
        value['owner-edit-after-review'] = True
        target.write_text(json.dumps(value))
        before = target.read_bytes()
        with patch('tools.uninstall.local._integration_references', return_value=(set(), False)), \
                patch('tools.uninstall.local._sharing_locks', return_value=nullcontext()), \
                self.assertRaisesRegex(ValueError, 'changed after review'):
            _remove_integration(action, {}, lambda: None)
        self.assertEqual(target.read_bytes(), before)

    def test_copilot_removal_keeps_schema_required_by_owner_added_hooks(self):
        self.apply(['copilot'])
        path = self.user / LIFECYCLE_PATHS['copilot']
        value = json.loads(path.read_text())
        owner = {'type': 'command', 'bash': 'owner-command'}
        value['hooks']['sessionStart'].append(owner)
        path.write_text(json.dumps(value))
        receipt = next(item for item in read_private(self.home / MANIFEST)['integrations']
                       if item['path'] == str(path))
        self.assertEqual(json.loads(removal_content(receipt)), {
            'version': 1, 'hooks': {'sessionStart': [owner]}})

    def test_tui_refuses_lifecycle_without_installed_launcher_before_review(self):
        from tools.tui.harness import configure_harnesses
        launcher = self.home / 'venv/bin/maven-mcp'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/fixture/python\n')
        launcher.chmod(0o755)
        ui = Mock()
        ui.form.return_value = {**{name: name == 'omp' for name in HARNESS_PATHS}, 'lifecycle': True}
        ui.choose.return_value = 'skip'
        with patch.dict(os.environ, {}, clear=True):
            result = configure_harnesses(ui, self.home)
        self.assertEqual(result['status'], 'conflict')
        self.assertIn('maven-session-hook', result['reason'])
        ui.review.assert_not_called()
        self.assertFalse((self.user / LIFECYCLE_PATHS['omp']).exists())


if __name__ == '__main__':
    unittest.main()
