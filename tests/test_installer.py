"""Local bootstrap failure boundaries; no cloud calls or package installs."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import tomllib
from unittest.mock import Mock, patch

import install
from tools.interaction import OperationPaused, using
from tools.harness import (
    HARNESS_PATHS, apply_integration, integration_plan, removal_content, remove_integration, retired_skill_plan,
    _read, launcher_permission_plan, repair_launcher_permissions,
)


class InstallerTests(unittest.TestCase):
    def test_package_source_excludes_stale_build_outputs_and_preserves_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in ('pyproject.toml', 'README.md', 'LICENSE'):
                (source / name).write_text('fixture')
            for package in ('tools', 'maven_resources'):
                (source / package).mkdir()
                (source / package / '__init__.py').write_text('')
            stale = source / 'build/lib/maven_resources/templates/skills/retired/SKILL.md'
            stale.parent.mkdir(parents=True)
            stale.write_text('retired instructions must not return')
            metadata = source / 'maven_cloud_memory.egg-info'
            metadata.mkdir()
            (metadata / 'SOURCES.txt').write_text(str(stale.relative_to(source)))
            current = source / 'maven_resources/templates/skills/current/SKILL.md'
            current.parent.mkdir(parents=True)
            current.write_text('current instructions')
            with install._package_source(source) as first:
                self.assertTrue((first / current.relative_to(source)).is_file())
                self.assertFalse((first / 'build').exists())
                self.assertFalse((first / metadata.name).exists())
                self.assertFalse(any(first.rglob('retired')))
            self.assertFalse(first.exists())
            current.unlink()
            with install._package_source(source) as second:
                self.assertFalse((second / current.relative_to(source)).exists())
            self.assertTrue(stale.is_file())
            self.assertTrue((metadata / 'SOURCES.txt').is_file())

    def test_unrelated_venv_directory_and_existing_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            environment = home / '.env'
            environment.write_text('# existing private configuration\n')
            environment.chmod(0o600)
            (home / 'venv').mkdir()
            existing = home / 'venv' / 'owner-file'
            existing.write_text('keep this data')
            with patch.object(install, '_prerequisites'), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = install.main(['--local-only', '--home', str(home)])
            self.assertEqual(result, 1)
            self.assertEqual(existing.read_text(), 'keep this data')
            self.assertEqual(environment.read_text(), '# existing private configuration\n')
            self.assertFalse((home / 'activate').exists())

    def test_invalid_ca_cannot_change_trust_or_create_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / 'instance'
            ca = root / 'invalid.pem'
            ca.write_text('not a certificate')
            keys = ('AWS_CA_BUNDLE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE')
            before = {key: os.environ.get(key) for key in keys}
            with patch.object(install, '_prerequisites'), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = install.main(['--local-only', '--home', str(home), '--ca-bundle', str(ca)])
            self.assertEqual(result, 1)
            self.assertEqual({key: os.environ.get(key) for key in keys}, before)
            self.assertFalse(home.exists())

    def test_live_harness_stop_is_confirmed_in_the_only_local_change_review(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            home = user / 'not-created'
            args = argparse.Namespace(home=home, google=False, harness=['omp'], skill_dir=[],
                                      ca_bundle=None, local_only=True, harness_stopped=False)
            ui = Mock()
            ui.review.return_value = False
            ui.confirm.side_effect = AssertionError('redundant confirmation')
            with using(ui), patch.object(Path, 'home', return_value=user), \
                    patch.dict(os.environ, {}, clear=True), patch.object(install, '_prerequisites'):
                with self.assertRaises(OperationPaused):
                    install._install(args)
            ui.review.assert_called_once()
            self.assertFalse(home.exists())
            self.assertFalse((user / '.omp').exists())

    def test_declined_local_review_creates_no_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'not-created'
            args = argparse.Namespace(home=home, google=False, harness=[], skill_dir=[],
                                      ca_bundle=None, local_only=True, harness_stopped=False)
            ui = Mock()
            ui.review.return_value = False
            with using(ui), patch.object(install, '_prerequisites'), \
                    patch.object(install, '_install_command') as command:
                with self.assertRaises(OperationPaused):
                    install._install(args)
            command.assert_not_called()
            self.assertFalse(home.exists())

    def test_back_from_local_review_preserves_uncreated_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'not-created'
            args = argparse.Namespace(home=home, google=False, harness=[], skill_dir=[],
                                      ca_bundle=None, local_only=True, harness_stopped=False)
            ui = Mock()
            ui.review.return_value = None
            with using(ui), patch.object(install, '_prerequisites'), \
                    patch.object(install, '_install_command') as command:
                with self.assertRaises(install.BackToOptions):
                    install._install(args)
            command.assert_not_called()
            self.assertFalse(home.exists())

    def test_switching_home_selects_its_exact_unfinished_operation(self):
        from tools.uninstall.common import save_private
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as directory:
            homes = [Path(directory) / name for name in ('first', 'second')]
            identifiers = [str(uuid4()), str(uuid4())]
            for home, identifier in zip(homes, identifiers):
                save_private(home / 'runtime/setup/state.json', {'administration_operation': identifier})
                save_private(home / 'runtime/administration' / (identifier + '.json'), {
                    'state': 'blocked', 'step': 'identity_inspect', 'receipts': [], 'parameters': {'mode': 'update'}})
                (home / '.env').write_text('MAVEN_AWS_PROFILE=owner\n')
                (home / '.env').chmod(0o600)
            args = argparse.Namespace(home=homes[0])
            ui = Mock()
            ui.choose.side_effect = ['existing', 'existing']
            ui.form.side_effect = [{'home': str(homes[1])}, {'home': str(homes[1])}]
            with using(ui):
                install._options(args)
            self.assertEqual(args.home, homes[1])
            self.assertEqual(args.setup_operation, identifiers[1])
            # Resume continues the unfinished operation in its own mode, not a connection check.
            self.assertEqual(args.setup_mode, 'update')
            self.assertTrue(args.check_existing)
            self.assertFalse(args.local_only)
            self.assertEqual((homes[0] / '.env').read_text(), 'MAVEN_AWS_PROFILE=owner\n')

    def test_separate_instance_cannot_reuse_existing_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'existing'
            home.mkdir(mode=0o700)
            environment = home / '.env'
            environment.write_text('MAVEN_AWS_PROFILE=keep\n')
            environment.chmod(0o600)
            unused = home.parent / 'unused'
            args = argparse.Namespace(home=home)
            ui = Mock()
            ui.choose.side_effect = ['separate', 'new', 'advanced', 'local']
            ui.form.side_effect = [{'home': str(home)}, {'home': str(unused)}]
            with using(ui):
                install._options(args)
            self.assertEqual(environment.read_text(), 'MAVEN_AWS_PROFILE=keep\n')
            self.assertEqual(args.home, unused)
            self.assertTrue(args.local_only)
            self.assertIsNone(args.setup_operation)
            self.assertFalse(unused.exists())

    def test_uninstall_discovery_lists_known_homes_without_scanning_unrelated_folders(self):
        from tools.tui.installations import choose_removal_home
        from tools.uninstall.common import save_private
        from tools.uninstall.local import REGISTRY
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            default, environment, registered, removed, unrelated = [
                user / name for name in ('.maven', 'environment', 'custom', 'removed', 'unrelated')]
            for home in (default, environment, registered, removed, unrelated):
                home.mkdir(mode=0o700)
            save_private(removed / 'installation.json', {'active': False})
            save_private(user / REGISTRY, {'schema': 1, 'homes': [str(registered), str(removed)]})
            keep = unrelated / 'owner-file'
            keep.write_text('preserve')
            ui = Mock()
            def choose(title, choices, **kwargs):
                detected = {label: value for label, value in choices if value.isdigit()}
                self.assertEqual(set(detected), {str(default), str(environment), str(registered)})
                return detected[str(registered)]
            ui.choose.side_effect = choose
            ui.review.return_value = True
            with patch.object(Path, 'home', return_value=user), \
                    patch.dict(os.environ, {'MAVEN_HOME': str(environment)}):
                selected = choose_removal_home(ui)
            self.assertEqual(selected, registered)
            self.assertEqual(keep.read_text(), 'preserve')
            self.assertTrue(all(home.is_dir() for home in (default, environment, registered, removed)))

    def test_advanced_uninstall_browser_rejects_root_files_and_symlink_paths(self):
        from tools.tui.installations import choose_removal_home
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            home = user / 'custom'
            home.mkdir(mode=0o700)
            keep = home / 'owner-file'
            keep.write_text('preserve')
            link = user / 'link'
            link.symlink_to(home, target_is_directory=True)
            ui = Mock()
            ui.choose.side_effect = ['advanced', 'path', 'path', 'path', 'path', 'select']
            ui.form.side_effect = [{'path': '/'}, {'path': str(keep)}, {'path': str(link)},
                                   {'path': str(home)}]
            ui.review.return_value = True
            with patch.object(Path, 'home', return_value=user), \
                    patch.dict(os.environ, {'MAVEN_HOME': str(user / 'absent')}):
                selected = choose_removal_home(ui)
            self.assertEqual(selected, home)
            self.assertTrue(link.is_symlink())
            self.assertEqual(keep.read_text(), 'preserve')

    def test_failed_package_command_cannot_leak_its_output_into_terminal(self):
        script = """
import sys
import install
from tools.interaction import using
with using(object()):
    try:
        install._install_command([sys.executable, '-c',
            "import sys; print('private-output'); print('private-error', file=sys.stderr); sys.exit(1)"])
    except ValueError as error:
        print(error)
    else:
        raise SystemExit('failed command was accepted')
"""
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                cwd=Path(install.__file__).parent,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('private-output', result.stdout + result.stderr)
        self.assertNotIn('private-error', result.stdout + result.stderr)

    def test_staged_install_records_safe_launcher_and_skills_under_group_writable_umask(self):
        from tools.uninstall.common import read_private
        from tools.uninstall.local import inventory, MANIFEST
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            home, staged = user / 'instance', user / 'inactive'
            def create_runtime(path):
                (path / 'bin').mkdir(parents=True)
                (path / 'pyvenv.cfg').write_text('test runtime; never executed\n')
                (path / 'bin/python').write_text('test interpreter; never executed\n')
            def install_package(command):
                for name in ('maven-mcp', 'maven-session-hook'):
                    launcher = home / 'venv/bin' / name
                    launcher.write_text('#!/test-only/python\n')
                    launcher.chmod(0o775)
            mask = os.umask(0o002)
            try:
                with patch.object(Path, 'home', return_value=user), patch.dict(os.environ, {}, clear=True), \
                        patch.object(install, '_prerequisites'), \
                        patch('venv.EnvBuilder.create', side_effect=create_runtime), \
                        patch.object(install.subprocess, 'run', return_value=Mock(returncode=0)), \
                        patch.object(install, '_install_command', side_effect=install_package), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = install.main(['--local-only', '--home', str(home), '--harness', 'omp',
                                           '--stage-harnesses', str(staged)])
            finally:
                os.umask(mask)
            self.assertEqual(result, 0)
            launcher = home / 'venv/bin/maven-mcp'
            self.assertEqual(launcher.stat().st_mode & 0o7777, 0o755)
            self.assertIsNotNone(_read(launcher))
            hook_launcher = home / 'venv/bin/maven-session-hook'
            self.assertEqual(hook_launcher.stat().st_mode & 0o7777, 0o755)
            self.assertIsNotNone(_read(hook_launcher))
            receipt = read_private(home / MANIFEST)
            self.assertEqual(receipt['owned'][str(home / 'venv')], inventory(home / 'venv'))
            self.assertFalse((user / '.omp').exists())
            config = staged / HARNESS_PATHS['omp'][0]
            self.assertEqual(json.loads(_read(config))['mcpServers']['maven']['command'], str(launcher))
            for name in ('maven', 'maven-maintenance'):
                skill = staged / HARNESS_PATHS['omp'][2] / name / 'SKILL.md'
                self.assertEqual(skill.stat().st_mode & 0o777, 0o600)
                self.assertIsNotNone(_read(skill))
                self.assertEqual(receipt['owned'][str(skill)], inventory(skill))
            for item in receipt['integrations']:
                remove_integration(item)
                self.assertFalse(Path(item['path']).exists())

    def test_harness_install_and_selective_removal_preserve_unrelated_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / '.gemini/settings.json'
            config.parent.mkdir()
            original = {'theme': 'owner', 'mcpServers': {'other': {'command': '/owner/tool'}}}
            config.write_text(json.dumps(original))
            config.chmod(0o600)
            context = root / '.gemini/GEMINI.md'
            context.write_text('Owner instructions without trailing newline')
            context.chmod(0o600)
            changes = integration_plan(root / 'instance', ['gemini'], user_home=root)
            receipts = [apply_integration(change) for change in changes]
            value = json.loads(config.read_text())
            value['theme'] = 'changed by owner after install'
            config.write_text(json.dumps(value))
            for receipt in receipts:
                remove_integration(receipt)
            expected = dict(original, theme='changed by owner after install')
            self.assertEqual(json.loads(config.read_text()), expected)
            self.assertEqual(context.read_text(), 'Owner instructions without trailing newline')

    def test_changed_entry_and_review_races_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            change = integration_plan(root / 'instance', ['omp'], user_home=root)[0]
            receipt = apply_integration(change)
            value = json.loads(change.path.read_text())
            value['mcpServers']['maven']['command'] = '/owner/replacement'
            change.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                removal_content(receipt)
            self.assertEqual(json.loads(change.path.read_text()), value)
            with self.assertRaises(ValueError):
                apply_integration(change)

    def test_stdio_formats_round_trip_without_adopting_existing_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'never-copy', 'MODEL_API_KEY': 'never-copy'}):
                changes = integration_plan(root / 'instance with spaces', HARNESS_PATHS, user_home=root)
            for change in changes:
                apply_integration(change)
            for name, (relative, _, _) in HARNESS_PATHS.items():
                raw = (root / relative).read_text()
                document = tomllib.loads(raw) if name == 'codex' else json.loads(raw)
                server = document['mcp_servers' if name == 'codex' else 'mcpServers']['maven']
                self.assertEqual(server['command'], str(root / 'instance with spaces/venv/bin/maven-mcp'))
                self.assertEqual(server['args'], [])
                self.assertNotIn('never-copy', raw)
                self.assertEqual(server['env']['MAVEN_HOME'], str(root / 'instance with spaces'))
            again = integration_plan(root / 'instance with spaces', HARNESS_PATHS, user_home=root)
            self.assertTrue(all(change.receipt['owned'] is False for change in again))
            self.assertTrue(all(change.before == change.after for change in again))
            with self.assertRaises(ValueError):
                remove_integration(again[0].receipt)
            for change in changes:
                remove_integration(change.receipt)
                self.assertFalse(change.path.exists())

    def test_codex_unrelated_tables_survive_and_changed_owned_table_blocks_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '.codex/config.toml'
            path.parent.mkdir()
            original = '# owner comment\n[features]\nmulti_agent = true\n'
            path.write_text(original)
            path.chmod(0o600)
            change = integration_plan(root / 'instance', ['codex'], user_home=root)[0]
            receipt = apply_integration(change)
            path.write_text(path.read_text() + 'timeout = 123\n')
            with self.assertRaises(ValueError):
                removal_content(receipt)
            path.write_bytes(change.after)
            remove_integration(receipt)
            self.assertEqual(path.read_text(), original)

    def test_integration_symlinks_and_conflicts_never_overwrite_owner_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / '.claude.json'
            owner = root / 'owner'
            owner.write_text('{}')
            target.symlink_to(owner)
            with self.assertRaises(ValueError):
                integration_plan(root / 'instance', ['claude'], user_home=root)
            target.unlink()
            target.write_text('{"mcpServers":{"maven":{"command":"owner"}}}')
            with self.assertRaises(ValueError):
                integration_plan(root / 'instance', ['claude'], user_home=root)
            self.assertEqual(json.loads(target.read_text())['mcpServers']['maven']['command'], 'owner')

    def test_claude_statusline_is_optional_owned_and_selectively_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / '.claude/settings.json'
            plain = integration_plan(root / 'instance', ['claude'], user_home=root)
            self.assertNotIn(settings, [change.path for change in plain])
            settings.parent.mkdir()
            original = {'theme': 'dark', 'permissions': {'allow': ['Bash(ls)']}}
            settings.write_text(json.dumps(original))
            settings.chmod(0o600)
            changes = integration_plan(root / 'instance', ['claude'], user_home=root, statusline=True)
            change = next(change for change in changes if change.path == settings)
            receipts = [apply_integration(item) for item in changes]
            self.assertEqual(json.loads(settings.read_text()), dict(original, statusLine={
                'type': 'command', 'command': str(root / 'instance/venv/bin/maven-statusline')}))
            self.assertIsNot(change.receipt.get('owned'), False)
            again = integration_plan(root / 'instance', ['claude'], user_home=root, statusline=True)
            repeat = next(item for item in again if item.path == settings)
            self.assertEqual((repeat.before, repeat.receipt['owned']), (repeat.after, False))
            self.assertNotIn('conflict', repeat.receipt)
            value = json.loads(settings.read_text())
            value['theme'] = 'changed by owner after install'
            settings.write_text(json.dumps(value))
            for receipt in receipts:
                remove_integration(receipt)
            self.assertEqual(json.loads(settings.read_text()), dict(original, theme='changed by owner after install'))

    def test_claude_statusline_created_settings_file_is_removed_entirely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / '.claude/settings.json'
            changes = integration_plan(root / 'instance', ['claude'], user_home=root, statusline=True)
            change = next(change for change in changes if change.path == settings)
            self.assertIsNone(change.before)
            receipt = apply_integration(change)
            self.assertEqual(json.loads(settings.read_text())['statusLine']['type'], 'command')
            remove_integration(receipt)
            self.assertFalse(settings.exists())

    def test_claude_statusline_conflicts_and_owner_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / '.claude/settings.json'
            settings.parent.mkdir()
            owner = {'statusLine': {'type': 'command', 'command': '/owner/status.sh'}, 'model': 'opus'}
            settings.write_text(json.dumps(owner))
            settings.chmod(0o600)
            changes = integration_plan(root / 'instance', ['claude'], user_home=root, statusline=True)
            change = next(change for change in changes if change.path == settings)
            self.assertEqual(change.before, change.after)
            self.assertIs(change.receipt['owned'], False)
            self.assertIn('conflict', change.receipt)
            for item in changes:
                apply_integration(item)
            self.assertEqual(json.loads(settings.read_text()), owner)
            with self.assertRaises(ValueError):
                removal_content(change.receipt)
            self.assertEqual(json.loads(settings.read_text()), owner)

            settings.write_text(json.dumps({'model': 'opus'}))
            change = next(change for change in integration_plan(
                root / 'instance', ['claude'], user_home=root, statusline=True) if change.path == settings)
            receipt = apply_integration(change)
            edited = json.loads(settings.read_text())
            edited['statusLine']['padding'] = 2
            settings.write_text(json.dumps(edited))
            with self.assertRaises(ValueError):
                remove_integration(receipt)
            self.assertEqual(json.loads(settings.read_text()), edited)

    def test_claude_statusline_requires_the_default_claude_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                integration_plan(root / 'instance', ['omp'], user_home=root, statusline=True)
            custom = {'claude': (root / 'custom/claude.json', root / 'custom/CLAUDE.md')}
            with self.assertRaises(ValueError):
                integration_plan(root / 'instance', ['claude'], user_home=root, paths=custom, statusline=True)
            self.assertFalse((root / '.claude/settings.json').exists())

    def test_live_user_home_cannot_bypass_shutdown_as_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(install, '_prerequisites'), patch.object(Path, 'home', return_value=root), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = install.main(['--local-only', '--home', str(root / 'instance'),
                                       '--harness', 'omp', '--stage-harnesses', str(root)])
            self.assertEqual(result, 1)
            self.assertFalse((root / 'instance').exists())

    def test_legacy_skill_cutover_requires_exact_unshared_ownership(self):
        from tools.uninstall.local import inventory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, skills = root / 'instance', root / 'skills'
            home.mkdir()
            (home / 'installation.json').write_text('{}')
            target = skills / 'maven-connect/SKILL.md'
            target.parent.mkdir(parents=True)
            target.write_text('old installed skill')
            target.chmod(0o600)
            manifest = {'schema': 1, 'home': str(home),
                        'owned': {str(target): inventory(target)}, 'skill_references': [str(target)]}
            with patch('tools.uninstall.common.read_private', return_value=manifest), \
                    patch('tools.uninstall.local._shared_skills', return_value=(set(), False)):
                plan = retired_skill_plan(home, [skills])
                self.assertEqual([path for path, _ in plan], [target])
                target.write_text('owner modified guidance')
                with self.assertRaises(ValueError):
                    retired_skill_plan(home, [skills])
                self.assertEqual(target.read_text(), 'owner modified guidance')

    def test_startup_targets_follow_native_context_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            override = root / '.codex/AGENTS.override.md'
            override.parent.mkdir()
            override.write_text('Owner override')
            override.chmod(0o600)
            changes = integration_plan(root / 'instance', ['codex'], user_home=root)
            self.assertEqual(changes[1].path, override)
            self.assertTrue(changes[1].after.startswith(b'Owner override\n'))
            settings = root / '.gemini/settings.json'
            settings.parent.mkdir()
            settings.write_text('{"context":{"fileName":"CUSTOM.md"}}')
            settings.chmod(0o600)
            with self.assertRaises(ValueError):
                integration_plan(root / 'instance', ['gemini'], user_home=root)
            selected = integration_plan(root / 'instance', ['gemini'], user_home=root,
                                        paths={'gemini': (settings, settings.parent / 'CUSTOM.md')})
            self.assertEqual(selected[1].path.name, 'CUSTOM.md')


class HarnessFinishTests(unittest.TestCase):
    def setUp(self):
        from tools.uninstall.local import record_installation
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user = Path(self.temporary.name)
        self.home = self.user / 'instance'
        self.home.mkdir(mode=0o700)
        self.launcher = self.home / 'venv/bin/maven-mcp'
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text('#!/test-only/python\nprint("launcher")\n')
        self.launcher.chmod(0o775)
        self.user_patch = patch.object(Path, 'home', return_value=self.user)
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        record_installation(self.home, Path(install.__file__).parent, [self.home / 'venv'], [])

    def ui(self):
        ui = Mock()
        ui.form.return_value = {name: name == 'omp' for name in HARNESS_PATHS}
        ui.review.return_value = True
        ui.choose.return_value = 'skip'
        return ui

    def test_exact_launcher_repair_preserves_bytes_and_other_files(self):
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.common import read_private
        from tools.uninstall.local import inventory, MANIFEST
        before = self.launcher.read_bytes()
        unrelated = self.home / 'venv/owner-added'
        unrelated.write_text('not adopted')
        unrelated.chmod(0o664)
        ui = self.ui()
        result = configure_harnesses(ui, self.home)
        self.assertEqual(result['status'], 'written')
        self.assertEqual(self.launcher.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(self.launcher.read_bytes(), before)
        self.assertEqual(unrelated.stat().st_mode & 0o777, 0o664)
        receipt = read_private(self.home / MANIFEST)
        owned = receipt['owned'][str(self.home / 'venv')]
        self.assertEqual(owned['bin/maven-mcp'], inventory(self.launcher)[''])
        self.assertNotIn('owner-added', owned)
        ui.review.assert_called_once()
        ui.choose.assert_not_called()
        self.assertIn('0775 → 0755', ui.review.call_args.args[1])
        original_integrations = receipt['integrations']
        retry_ui = self.ui()
        retry = configure_harnesses(retry_ui, self.home)
        retry_ui.review.assert_not_called()
        self.assertEqual(retry['status'], 'unchanged')
        self.assertEqual(read_private(self.home / MANIFEST)['integrations'], original_integrations)
        for item in original_integrations:
            remove_integration(item)
            self.assertFalse(Path(item['path']).exists())

    def test_manually_repaired_launcher_refreshes_only_exact_stale_receipt(self):
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.common import read_private
        from tools.uninstall.local import inventory, MANIFEST
        self.launcher.chmod(0o755)
        before = self.launcher.stat()
        ui = self.ui()
        with patch('tools.tui.harness.repair_launcher_permissions') as chmod:
            result = configure_harnesses(ui, self.home)
        chmod.assert_not_called()
        self.assertEqual(result['status'], 'written')
        self.assertEqual(self.launcher.stat().st_ctime_ns, before.st_ctime_ns)
        self.assertIn('Launcher already 0755', ui.review.call_args.args[1])
        receipt = read_private(self.home / MANIFEST)
        self.assertEqual(receipt['owned'][str(self.home / 'venv')]['bin/maven-mcp'],
                         inventory(self.launcher)[''])

    def test_declined_repair_does_not_change_launcher_receipt_or_harness(self):
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.local import MANIFEST
        receipt = (self.home / MANIFEST).read_bytes()
        ui = self.ui()
        ui.review.return_value = False
        result = configure_harnesses(ui, self.home)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o775)
        self.assertEqual((self.home / MANIFEST).read_bytes(), receipt)
        self.assertFalse((self.user / '.omp').exists())

    def test_interrupted_repair_journal_retries_without_adopting_runtime(self):
        from tools.tui.harness import configure_harnesses
        from tools.uninstall.common import read_private
        from tools.uninstall.local import MANIFEST
        with patch('tools.tui.harness.repair_launcher_permissions', side_effect=OSError('interrupted')):
            result = configure_harnesses(self.ui(), self.home)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o775)
        self.assertEqual(read_private(self.home / MANIFEST)['owned'][str(self.home / 'venv')]['bin/maven-mcp']['mode'], 0o755)
        result = configure_harnesses(self.ui(), self.home)
        self.assertEqual(result['status'], 'written')
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)

    def test_launcher_symlink_hardlink_wrong_owner_and_other_modes_are_preserved(self):
        original = self.launcher.read_bytes()
        owner = self.launcher.with_name('owner')
        self.launcher.rename(owner)
        self.launcher.symlink_to(owner)
        with self.assertRaises(ValueError):
            launcher_permission_plan(self.home)
        self.launcher.unlink()
        os.link(owner, self.launcher)
        with self.assertRaises(ValueError):
            launcher_permission_plan(self.home)
        self.launcher.unlink()
        owner.rename(self.launcher)
        with patch('tools.harness.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                launcher_permission_plan(self.home)
        for mode in (0o777, 0o6755, 0o644):
            self.launcher.chmod(mode)
            with self.assertRaises(ValueError):
                launcher_permission_plan(self.home)
            self.assertEqual(self.launcher.stat().st_mode & 0o7777, mode)
        self.assertEqual(self.launcher.read_bytes(), original)

    def test_changed_launcher_refuses_repair_and_unowned_launcher_is_not_adopted(self):
        from tools.tui.harness import configure_harnesses
        expected = launcher_permission_plan(self.home)
        self.launcher.write_text('# owner changed the launcher\n')
        with self.assertRaises(ValueError):
            repair_launcher_permissions(self.home, expected)
        result = configure_harnesses(self.ui(), self.home)
        self.assertEqual(result['status'], 'conflict')
        self.assertIn('ownership receipt', result['reason'])
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o775)
        self.assertFalse((self.user / '.omp').exists())

    def test_conflict_retry_stays_in_local_harness_workflow(self):
        from tools.tui.harness import configure_harnesses
        ui = self.ui()
        ui.choose.return_value = 'retry'
        from tools.harness import integration_plan as real_plan
        calls = []
        def planning(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise ValueError('local conflict corrected before retry')
            return real_plan(*args, **kwargs)
        with patch('tools.tui.harness.integration_plan', side_effect=planning):
            result = configure_harnesses(ui, self.home)
        self.assertEqual(result['status'], 'written')
        ui.choose.assert_called_once()
        ui.review.assert_called_once()

    def completed(self, *, legacy=False):
        from tools.uninstall.common import save_private
        from uuid import uuid4
        values = {'MAVEN_MEMORY_INSTANCE_ID': str(uuid4()), 'MAVEN_ACCOUNT_ID': '123456789012',
                  'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_AWS_PROFILE': 'owner',
                  'MAVEN_MEMORY_ENDPOINT': 'https://original.example',
                  'MAVEN_MEMORY_RECOVERY_EPOCH': str(uuid4()),
                  'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/owner'}
        environment = self.home / '.env'
        environment.write_text(''.join(key + '=' + json.dumps(value) + '\n' for key, value in values.items()))
        environment.chmod(0o600)
        identifier = str(uuid4())
        outcome = {'home': str(self.home), 'instance_ready': True,
                   'instance_id': values['MAVEN_MEMORY_INSTANCE_ID'], 'account': values['MAVEN_ACCOUNT_ID'],
                   'region': values['MAVEN_AWS_REGION'], 'profile': values['MAVEN_AWS_PROFILE'],
                   'authority_generation': 3, 'project_configured': False}
        previous = {'kind': 'setup', 'state': 'completed', 'operation_id': identifier,
                    'step': 'verify', 'pending_interaction': None, 'outcome': outcome,
                    'receipts': [{'step': 'verify', 'result': {'instance_ready': True}}]}
        if legacy:
            previous['starter_binding'] = values
        else:
            outcome['binding'] = values
        save_private(self.home / 'runtime/setup/state.json', {'administration_operation': identifier})
        save_private(self.home / 'runtime/administration' / (identifier + '.json'), previous)
        return previous

    def test_completed_default_and_explicit_resume_reuse_bound_readiness_offline(self):
        from tools.setup import _present
        for legacy in (False, True):
            previous = self.completed(legacy=legacy)
            for explicit in (False, True):
                with self.subTest(legacy=legacy, explicit=explicit):
                    ui, controller = Mock(), Mock()
                    controller.status.return_value = previous
                    args = argparse.Namespace(operation=previous['operation_id'] if explicit else None,
                                              fresh_operation=False, mode=None)
                    receipt_path = self.home / 'runtime/administration' / (previous['operation_id'] + '.json')
                    before = receipt_path.read_bytes()
                    with patch('tools.administration.AdminOperations', return_value=controller), \
                            patch('tools.tui.administration.present') as present, \
                            patch('tools.tui.harness.finish_setup', return_value={'status': 'skipped'}) as finish, \
                            patch('tools.memory.environment.selected_aws_session', side_effect=AssertionError('AWS forbidden')):
                        self.assertEqual(_present(ui, args, self.home), {'status': 'skipped'})
                    present.assert_not_called()
                    controller.begin.assert_not_called()
                    controller.advance.assert_not_called()
                    ui.review.assert_not_called()
                    finish.assert_called_once()
                    self.assertEqual(finish.call_args.args[2]['binding'],
                                     previous.get('starter_binding') or previous['outcome']['binding'])
                    self.assertEqual(receipt_path.read_bytes(), before)

    def test_changed_targets_or_missing_binding_cannot_reuse_completed_readiness(self):
        from tools.setup import _present
        from tools.setup_common import SetupPending
        from tools.tui.harness import completed_setup_outcome
        previous = self.completed()
        for key in ('MAVEN_MEMORY_ENDPOINT', 'MAVEN_MEMORY_RECOVERY_EPOCH',
                    'MAVEN_MEMORY_INSTANCE_ID', 'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL'):
            with self.subTest(key=key):
                changed = dict(previous['outcome']['binding'], **{key: 'changed'})
                (self.home / '.env').write_text(''.join(k + '=' + json.dumps(v) + '\n' for k, v in changed.items()))
                controller = Mock()
                controller.status.return_value = previous
                with patch('tools.administration.AdminOperations', return_value=controller), \
                        patch('tools.tui.administration.present') as present, \
                        patch('tools.tui.harness.finish_setup') as finish:
                    with self.assertRaises(SetupPending):
                        _present(Mock(), argparse.Namespace(operation=previous['operation_id'],
                                 fresh_operation=False, mode=None), self.home)
                present.assert_not_called()
                finish.assert_not_called()
        previous = self.completed()
        previous['outcome'].pop('binding')
        with self.assertRaises(ValueError):
            completed_setup_outcome(self.home, previous)

    def test_ready_installer_menu_routes_harness_and_explicit_fresh_check_separately(self):
        self.completed()
        args, ui = argparse.Namespace(home=self.home), Mock()
        ui.choose.return_value = 'harness'
        with using(ui):
            install._options(args)
        self.assertTrue(args.check_existing)
        self.assertFalse(args.setup_fresh)
        self.assertFalse(args.local_only)
        ui.form.assert_not_called()
        self.assertIn('harness', [value for _, value in ui.choose.call_args.args[1]])
        ui.choose.return_value = 'existing'
        ui.form.return_value = {'home': str(self.home)}
        with using(ui):
            install._options(args)
        self.assertTrue(args.setup_fresh)
        self.assertIsNone(args.setup_operation)

    def test_menu_home_entry_dispatches_home_operation_and_offers_turn_off_when_set(self):
        self.completed()
        args, ui = argparse.Namespace(home=self.home), Mock()
        ui.choose.return_value = 'home'
        with using(ui):
            install._options(args)
        self.assertIn('home', [value for _, value in ui.choose.call_args.args[1]])
        self.assertEqual(args.home_action, 'enable')
        self.assertFalse(args.uninstall)
        command = install._handoff_command({'home': self.home, 'home_action': args.home_action})
        self.assertEqual(command[1:], ['-m', 'tools.tui.administration', '--home', str(self.home),
                                       '--action', 'enable'])
        (self.home / 'home.json').write_text('{}')
        ui.choose.side_effect = ['home', 'disable']
        with using(ui):
            install._options(args)
        self.assertEqual(args.home_action, 'disable')
        self.assertEqual(install._handoff_command({'home': self.home, 'home_action': 'disable'})[-1], 'disable')

    def test_home_offer_appears_only_without_home_and_skip_writes_nothing(self):
        from tools.tui import harness
        outcome = {'status': 'skipped', 'selected': [], 'written': []}
        before = sorted(str(path) for path in self.home.rglob('*'))
        with patch.object(harness, '_verified', return_value=True), \
                patch.object(harness, 'configure_harnesses', return_value=outcome), \
                patch.object(harness, 'show_completion', return_value=outcome), \
                patch.object(harness, '_cloud_context', return_value=''), \
                patch('tools.tui.administration.present') as present:
            ui = Mock()
            ui.choose.return_value = 'skip'
            self.assertEqual(harness.finish_setup(ui, self.home, {}), outcome)
            self.assertEqual(ui.choose.call_args.args[0], 'Set up MAVEN Home (optional)')
            present.assert_not_called()
            self.assertEqual(sorted(str(path) for path in self.home.rglob('*')), before)
            ui = Mock()
            ui.choose.return_value = 'home'
            present.return_value = 0
            self.assertEqual(harness.finish_setup(ui, self.home, {}), outcome)
            self.assertEqual(present.call_args.kwargs['kind'], 'home')
            present.reset_mock()
            present.side_effect = OperationPaused('paused')
            self.assertEqual(harness.finish_setup(ui, self.home, {}), outcome)
            present.reset_mock()
            with patch('tools.home.load_home', return_value={'root': str(self.user)}):
                ui = Mock()
                self.assertEqual(harness.finish_setup(ui, self.home, {}), outcome)
                ui.choose.assert_not_called()
                present.assert_not_called()

    def test_claude_statusline_opt_in_is_planned_and_existing_line_is_reviewed_as_conflict(self):
        from tools.tui import harness
        settings = self.user / '.claude/settings.json'
        settings.parent.mkdir(parents=True)
        owner = {'statusLine': {'type': 'command', 'command': 'owner-line'}}
        settings.write_text(json.dumps(owner))
        settings.chmod(0o600)
        for claude, expected in ((False, False), (True, True)):
            with self.subTest(claude=claude):
                ui = self.ui()
                ui.form.return_value = {**{name: name == ('claude' if claude else 'omp') for name in HARNESS_PATHS},
                                        'statusline': True}
                ui.review.return_value = False
                with patch.object(harness, 'integration_plan', wraps=integration_plan) as plan:
                    harness.configure_harnesses(ui, self.home)
                self.assertIs(plan.call_args.kwargs['statusline'], expected)
                if claude:
                    body = ui.review.call_args.args[1]
                    self.assertIn('Conflict, not changed', body)
                    self.assertNotIn(f'Preserve identical content: {settings}', body)
        self.assertEqual(json.loads(settings.read_text()), owner)


if __name__ == '__main__':
    unittest.main()
