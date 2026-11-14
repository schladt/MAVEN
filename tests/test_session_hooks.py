"""Offline native hook contracts: emitted model context, identity and end-event safety."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import session_hooks


class SessionHookTests(unittest.TestCase):
    def test_native_startup_schemas_deliver_exact_conversation_instruction(self):
        for harness in session_hooks.HARNESS_NAMES:
            with self.subTest(harness=harness), patch('tools.startup.inspect_startup', return_value={
                    'status': 'choose', 'message': 'Choose memory mode', 'issues': []}) as inspect:
                key = 'sessionId' if harness == 'copilot' else 'session_id'
                result = session_hooks.handle_event(Path('/private/instance'), harness, 'startup',
                    {key: 'native-conversation-7', 'cwd': '/work/project'})
                if harness == 'omp':
                    context = result['instruction']
                    self.assertEqual(set(result), {'instruction'})
                elif harness == 'copilot':
                    context = result['additionalContext']
                    self.assertEqual(set(result), {'additionalContext'})
                else:
                    context = result['hookSpecificOutput']['additionalContext']
                    self.assertEqual(set(result), {'hookSpecificOutput'})
                    if harness != 'gemini':
                        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'SessionStart')
                self.assertIn('maven_startup', context)
                self.assertIn('"harness_session": "native-conversation-7"', context)
                self.assertIn('"action": "inspect"', context)
                self.assertIn('did not initialize, capture memory or establish readiness', context)
                self.assertIn('Never take over another session', context)
                inspect.assert_called_once_with(Path('/private/instance'), Path('/work/project'),
                    harness=harness, harness_session='native-conversation-7')

    def test_missing_or_ambiguous_identity_never_uses_newest_cwd_or_environment(self):
        with patch('tools.startup.inspect_startup') as inspect:
            for payload in ({'cwd': '/work', 'pid': 123, 'thread_id': 'not-a-native-hook-field'},
                            {'cwd': '/work', 'session_id': ''},
                            {'cwd': '/work', 'session_id': 'id\nforged'},
                            {'cwd': 'relative', 'session_id': 'native'},
                            {'cwd': '/work', 'session_id': 'parent', 'agent_id': 'child'}, []):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    session_hooks.handle_event(Path('/home'), 'claude', 'startup', payload)
            with self.assertRaises(ValueError):
                session_hooks.handle_event(Path('/home'), 'omp', 'startup',
                    {'cwd': '/work', 'session_id': 'one'}, harness_session='another')
            inspect.assert_not_called()

    def test_startup_failure_is_visible_in_provider_context_not_ready(self):
        for harness in session_hooks.HARNESS_NAMES:
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(harness=harness), patch('sys.stdin', io.StringIO('{"cwd":"/work"}')), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = session_hooks.main(['startup', '--home', '/home', '--harness', harness])
            self.assertEqual(code, 0)
            self.assertIn('Do not claim readiness', json.dumps(json.loads(stdout.getvalue())))
            self.assertIn('Native conversation ID', stderr.getvalue())

    def test_explicit_omp_arguments_do_not_wait_on_stdin(self):
        class Unreadable:
            def read(self, *args):
                raise AssertionError('An extension invocation must not read inherited stdin')
        stdout = io.StringIO()
        with patch('sys.stdin', Unreadable()), redirect_stdout(stdout), \
                patch('tools.startup.inspect_shutdown', return_value={'status': 'unknown'}) as inspect:
            code = session_hooks.main(['status', '--home', '/home', '--harness', 'omp',
                                       '--cwd', '/work', '--harness-session', 'native'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {'status': 'unknown'})
        inspect.assert_called_once_with(Path('/home'), Path('/work'), harness='omp', harness_session='native')

    def test_end_events_only_record_native_observation_and_never_decide_exit(self):
        for harness in session_hooks.HARNESS_NAMES:
            key = 'sessionId' if harness == 'copilot' else 'session_id'
            with self.subTest(harness=harness), patch('tools.startup.record_shutdown') as record:
                result = session_hooks.handle_event(Path('/home'), harness, 'shutdown',
                    {key: 'native', 'cwd': '/work', 'reason': 'user_exit'})
                self.assertEqual(result, {})
                record.assert_called_once_with(Path('/home'), Path('/work'), harness=harness,
                                               harness_session='native', reason='user_exit')

    def test_unenabled_real_startup_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home, cwd = Path(directory) / 'home', Path(directory) / 'project'
            home.mkdir(mode=0o700)
            cwd.mkdir(mode=0o700)
            before = sorted(Path(directory).rglob('*'))
            output = session_hooks.handle_event(home, 'codex', 'startup',
                {'session_id': 'native-codex-session', 'cwd': str(cwd)})
            self.assertIn('maven_startup', output['hookSpecificOutput']['additionalContext'])
            self.assertEqual(sorted(Path(directory).rglob('*')), before)


if __name__ == '__main__':
    unittest.main()
