"""Local bootstrap failure boundaries; no cloud calls or package installs."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import install
from tools.interaction import OperationPaused, using


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


if __name__ == '__main__':
    unittest.main()
