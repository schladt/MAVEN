"""Local bootstrap failure boundaries; no cloud calls or package installs."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install


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


if __name__ == '__main__':
    unittest.main()
