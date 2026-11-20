"""maven-statusline: local private state only; never fails the harness status bar."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import statusline


class StatusLineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name).resolve()
        self.home = base / 'maven-home'
        self.home.mkdir(mode=0o700)
        self.outside = base / 'scratch'
        self.outside.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def run_line(self, payload, *, raw=None):
        stdin = io.TextIOWrapper(io.BytesIO(raw if raw is not None else json.dumps(payload).encode()))
        output = io.StringIO()
        with patch.dict(os.environ, {'MAVEN_HOME': str(self.home)}), \
                patch.object(sys, 'stdin', stdin), redirect_stdout(output):
            code = statusline.main([])
        self.assertEqual(code, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, output.getvalue())
        return lines[0]

    def test_uses_actual_folder_never_conversation_identity(self):
        for mode in ('home', 'project'):
            with self.subTest(mode=mode), patch.object(statusline, 'select_project', return_value={
                    'mode': mode, 'label': 'Test'}) as select:
                self.assertEqual(self.run_line({'session_id': 'ignored',
                                 'workspace': {'current_dir': str(self.outside)}}), f'MAVEN: {mode} Test (local)')
                select.assert_called_once_with(str(self.outside), self.home)
        with patch.object(statusline, 'select_project', return_value=None):
            self.assertEqual(self.run_line({'cwd': str(self.outside)}), statusline.OFF)
        self.assertEqual(self.run_line({'session_id': 'ignored'}), statusline.OFF)

    def test_scope_line_is_one_sanitized_line(self):
        with patch.object(statusline, 'select_project', return_value={'mode': 'project', 'label': '\x1b[31mA\nB'}):
            line = self.run_line({'cwd': str(self.outside)})
        self.assertNotIn('\x1b', line)
        self.assertTrue(line.startswith('MAVEN: project'))

    def test_bad_input_and_internal_errors_print_a_safe_line(self):
        for raw in (b'', b'not json', b'[1, 2]', b'{"session_id": 5, "workspace": "x", "cwd": []}', b'\xff\xfe'):
            self.assertTrue(self.run_line(None, raw=raw).startswith('MAVEN:'))
        with patch.object(statusline, 'status_line', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_line({'session_id': 's'}), statusline.UNAVAILABLE)
        with patch.object(statusline, 'select_project', side_effect=ValueError('legacy selection')):
            self.assertEqual(self.run_line({'cwd': str(self.outside)}), statusline.UNAVAILABLE)

    def test_command_runs_without_cloud_client_imports(self):
        script = ('import sys; from tools.statusline import main; main(); '
                  'sys.stderr.write(" ".join(m for m in ("boto3", "botocore", "requests") if m in sys.modules))')
        result = subprocess.run([sys.executable, '-c', script], input=json.dumps({'cwd': str(self.outside)}),
                                capture_output=True, text=True, timeout=30,
                                cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, 'MAVEN_HOME': str(self.home)})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, statusline.OFF + '\n')
        self.assertEqual(result.stderr, '')


    def test_generated_status_command_binds_quoted_home_against_ambient_selection(self):
        from tools.harness import _statusline_change
        custom = self.home / "space and 'quote"
        launcher = custom / 'venv/bin/maven-statusline'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!' + sys.executable + '\nimport os\nprint(os.environ["MAVEN_HOME"])\n')
        launcher.chmod(0o700)
        command = _statusline_change(custom, self.home / 'settings.json').receipt['value']['command']
        for ambient in (None, str(self.home)):
            environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
            environment.pop('MAVEN_HOME', None)
            if ambient:
                environment['MAVEN_HOME'] = ambient
            result = subprocess.run(command, shell=True, env=environment, text=True,
                                    input=json.dumps({'session_id': 's'}), capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(custom))



if __name__ == '__main__':
    unittest.main()
