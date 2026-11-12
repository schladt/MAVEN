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
from tools.home import ACTIVE_DIRECTORY, active, write_active


class StatusLineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name).resolve()
        self.home = base / 'maven-home'
        self.home.mkdir(mode=0o700)
        self.outside = base / 'scratch'
        self.outside.mkdir()
        self.project = base / 'alpha-repo'
        (self.project / '.maven').mkdir(parents=True)
        (self.project / '.maven/project.json').write_text('{}')
        (self.project / 'src').mkdir()

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

    def test_session_state_wins_and_folder_state_serves_other_sessions(self):
        folder_state = active('read_only')
        session_state = active('project', label='alpha', project_id='p-1')
        write_active(self.home, folder_state, folder=self.outside)
        write_active(self.home, session_state, harness_session='claude-1')
        self.assertEqual(self.run_line({'session_id': 'claude-1', 'workspace': {'current_dir': str(self.outside)}}),
                         session_state['line'])
        self.assertEqual(self.run_line({'session_id': 'claude-2', 'workspace': {'current_dir': str(self.outside)}}),
                         folder_state['line'])
        self.assertEqual(self.run_line({'session_id': 'claude-2', 'cwd': str(self.outside)}), folder_state['line'])

    def test_state_written_for_session_and_folder_is_found_by_either(self):
        state = active('home', label='Desk', project_id='p-2')
        write_active(self.home, state, harness_session='claude-9', folder=self.outside)
        self.assertEqual(self.run_line({'session_id': 'claude-9'}), state['line'])
        self.assertEqual(self.run_line({'session_id': 'unknown', 'cwd': str(self.outside)}), state['line'])
        self.assertEqual(self.run_line({'cwd': str(self.outside) + '/.'}), state['line'])

    def test_fallbacks_without_state(self):
        outside = self.run_line({'session_id': 's', 'workspace': {'current_dir': str(self.outside)}})
        self.assertEqual(outside, statusline.NOT_STARTED)
        inside = self.run_line({'session_id': 's', 'workspace': {'current_dir': str(self.project / 'src')}})
        self.assertNotIn(inside, (statusline.NOT_STARTED, statusline.OFF, statusline.UNAVAILABLE))
        self.assertIn('alpha-repo', inside)
        self.assertEqual(self.run_line({'session_id': 's'}), statusline.OFF)
        self.assertEqual(self.run_line({'cwd': str(self.outside / 'missing')}), statusline.OFF)

    def test_unsafe_or_corrupt_state_is_ignored(self):
        write_active(self.home, active('home', label='Desk'), harness_session='s', folder=self.outside)
        payload = {'session_id': 's', 'cwd': str(self.outside)}
        files = sorted((self.home / ACTIVE_DIRECTORY).iterdir())
        for path in files:
            path.chmod(0o644)
        self.assertEqual(self.run_line(payload), statusline.NOT_STARTED)
        for path in files:
            path.unlink()
            path.symlink_to(self.home / 'elsewhere.json')
        (self.home / 'elsewhere.json').write_text(json.dumps(active('home', label='Forged')))
        (self.home / 'elsewhere.json').chmod(0o600)
        self.assertEqual(self.run_line(payload), statusline.NOT_STARTED)
        for path in files:
            path.unlink()
            path.write_text('{not json')
            path.chmod(0o600)
        self.assertEqual(self.run_line(payload), statusline.NOT_STARTED)

    def test_state_line_is_one_sanitized_line(self):
        write_active(self.home, {**active('project', label='x'), 'line': 'MAVEN:\nproject \x1b[31mx'},
                     harness_session='s')
        line = self.run_line({'session_id': 's'})
        self.assertNotIn('\x1b', line)
        self.assertTrue(line.startswith('MAVEN: project'))
        write_active(self.home, {'mode': 'read_only'}, harness_session='t')
        self.assertEqual(self.run_line({'session_id': 't'}), active('read_only')['line'])

    def test_bad_input_and_internal_errors_print_a_safe_line(self):
        for raw in (b'', b'not json', b'[1, 2]', b'{"session_id": 5, "workspace": "x", "cwd": []}', b'\xff\xfe'):
            self.assertTrue(self.run_line(None, raw=raw).startswith('MAVEN:'))
        with patch.object(statusline, 'status_line', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_line({'session_id': 's'}), statusline.UNAVAILABLE)
        with patch.object(statusline, '_active_name', side_effect=OSError('unreadable')):
            self.assertEqual(self.run_line({'session_id': 's'}), statusline.UNAVAILABLE)

    def test_command_runs_without_cloud_client_imports(self):
        state = active('project', label='alpha')
        write_active(self.home, state, harness_session='proc')
        script = ('import sys; from tools.statusline import main; main(); '
                  'sys.stderr.write(" ".join(m for m in ("boto3", "botocore", "requests") if m in sys.modules))')
        result = subprocess.run([sys.executable, '-c', script], input=json.dumps({'session_id': 'proc'}),
                                capture_output=True, text=True, timeout=30,
                                cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, 'MAVEN_HOME': str(self.home)})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, state['line'] + '\n')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
