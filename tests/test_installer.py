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
    HARNESS_PATHS, apply_integration, integration_plan, removal_content, remove_integration,
)


class InstallerTests(unittest.TestCase):
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
            manifest = {'schema': 1, 'home': str(home),
                        'owned': {str(target): inventory(target)}, 'skill_references': [str(target)]}
            with patch('tools.uninstall.common.read_private', return_value=manifest), \
                    patch('tools.uninstall.local._shared_skills', return_value=(set(), False)):
                plan = install._retired_skill_plan(home, [skills])
                self.assertEqual([path for path, _ in plan], [target])
                target.write_text('owner modified guidance')
                with self.assertRaises(ValueError):
                    install._retired_skill_plan(home, [skills])
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


if __name__ == '__main__':
    unittest.main()
