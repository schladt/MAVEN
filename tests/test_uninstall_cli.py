"""Minimal local approval boundary; no cloud clients, installs, or environments."""
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import uuid

from tools.uninstall import cli
from tools.uninstall.common import UninstallError, digest, save_private


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

    def _plan_projects(self, base, count=1):
        projects = []
        for index in range(count):
            project = base / f'project-{index}'
            project.mkdir()
            config = project / '.maven/project.json'
            save_private(config, {'schema': 1, 'root': str(project),
                                 'project_id': str(uuid.uuid4()), 'git_enabled': False,
                                 'remote': None, 'accepted_url': None, 'branch': None,
                                 'checks': []})
            projects.append(config)
        arguments = ['plan', '--home', str(base / 'removed-home'), '--offline',
                     '--output', str(base / 'operation')]
        for config in projects:
            arguments.extend(['--project', str(config.parent.parent)])
        return cli.build_plan(cli.parser().parse_args(arguments)), projects

    def _approval(self, result, *extra):
        return cli.parser().parse_args([
            'resume', '--plan', result['plan_path'], '--confirm', result['plan']['plan_digest'],
            '--writers-quiescent', '--harnesses-stopped', *extra])

    def test_pause_between_real_detach_actions_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, configs = self._plan_projects(Path(temporary), count=2)
            pause = False
            events = []

            def progress(state):
                nonlocal pause
                events.append(state)
                pause = any(entry['kind'] == 'local_project_detach' and entry['state'] == 'completed'
                            for entry in state['actions'].values())

            state = cli.apply(self._approval(result), reexec=False,
                              progress=progress, should_pause=lambda: pause)
            self.assertEqual(state['status'], 'paused')
            self.assertEqual(cli._exit_status(state), 3)
            self.assertFalse(configs[0].exists())
            self.assertTrue(configs[1].exists())
            self.assertEqual(events[-1]['status'], 'paused')
            self.assertEqual(cli.status(result['plan_path'])['status'], 'paused')
            resumed = cli.apply(self._approval(result), reexec=False)
            self.assertEqual(resumed['status'], 'completed')
            self.assertFalse(configs[1].exists())

    def test_tui_worker_rejects_current_source_even_with_correct_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, configs = self._plan_projects(Path(temporary))
            with self.assertRaisesRegex(UninstallError, 'exact snapshotted'):
                cli.apply(self._approval(result, '--tui-worker'), reexec=False)
            self.assertTrue(configs[0].exists())

    def test_pinned_worker_pauses_on_control_line_or_terminal_eof(self):
        for control in ('pause\n', ''):
            with self.subTest(control=control), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                result, configs = self._plan_projects(base)
                child = subprocess.run([
                    sys.executable, '-B', str(base / 'operation/runner/run.py'),
                    'resume', '--plan', result['plan_path'], '--confirm', result['plan']['plan_digest'],
                    '--writers-quiescent', '--harnesses-stopped', '--tui-worker'],
                    input=control, text=True, capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 3, child.stderr)
                events = [json.loads(line) for line in child.stdout.splitlines()]
                self.assertEqual(events[-1]['event'], 'result')
                self.assertEqual(events[-1]['data']['status'], 'paused')
                self.assertTrue(all(event['event'] in ('progress', 'result') for event in events))
                self.assertTrue(configs[0].exists())

    def test_replaced_valid_plan_invalidates_tui_approvals(self):
        from tools.tui.uninstall import _approve

        with tempfile.TemporaryDirectory() as temporary:
            result, configs = self._plan_projects(Path(temporary))
            reviewed = result['plan']
            changed = dict(reviewed, warnings=[*reviewed['warnings'], 'new inventory warning'])
            changed['plan_digest'] = digest({key: value for key, value in changed.items()
                                             if key != 'plan_digest'})

            class UI:
                def stage(self, name):
                    pass

                def review(self, title, body, **kwargs):
                    save_private(result['plan_path'], changed)
                    return True

                def confirm(self, prompt):
                    return True

            with self.assertRaisesRegex(UninstallError, 'All approvals are invalid'):
                _approve(UI(), result['plan_path'], reviewed)
            self.assertTrue(configs[0].exists())


if __name__ == '__main__':
    unittest.main()
