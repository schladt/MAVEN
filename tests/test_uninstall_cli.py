"""Minimal local approval boundary; no cloud clients, installs, or environments."""
from pathlib import Path
import tempfile
import unittest
import uuid

from tools.uninstall import cli
from tools.uninstall.common import UninstallError, save_private


class UninstallApprovalTests(unittest.TestCase):
    def test_wrong_approval_and_changed_recovery_code_cannot_detach_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / 'project'
            project.mkdir()
            config = project / '.maven/project.json'
            save_private(config, {'schema': 1, 'root': str(project),
                                 'project_id': str(uuid.uuid4()), 'git_enabled': False,
                                 'remote': None, 'accepted_url': None, 'branch': None,
                                 'checks': []})
            original = config.read_bytes()
            home = base / 'already-removed-installation'
            arguments = cli.parser().parse_args([
                'plan', '--project', str(project), '--home', str(home), '--offline',
                '--output', str(base / 'operation')])
            result = cli.build_plan(arguments)
            apply = cli.parser().parse_args([
                'apply', '--plan', result['plan_path'], '--confirm', '0' * 64,
                '--writers-quiescent', '--harnesses-stopped'])
            with self.assertRaisesRegex(UninstallError, 'reviewed plan digest'):
                cli.apply(apply, reexec=False)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(home.exists())
            apply.confirm = result['plan']['plan_digest']
            runner = base / 'operation/runner/tools/uninstall/local.py'
            runner.write_bytes(runner.read_bytes() + b'\n# changed after approval\n')
            with self.assertRaisesRegex(UninstallError, 'implementation changed'):
                cli.apply(apply, reexec=False)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(home.exists())


if __name__ == '__main__':
    unittest.main()
