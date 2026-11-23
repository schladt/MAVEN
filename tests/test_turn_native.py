"""Offline native wire contracts, not live client qualification or UI rendering tests."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import turn_native, turn_runtime


CWD = '/workspace/contract-fixture'
HOME = Path('/private/maven-test-home')
SECRET = 'PRIVATE_SENTINEL_NOT_FOR_RECEIPT'
PROMPT = 'Suggest a commit message only.'
FOOTER = 'MAVEN · Project: consulted/no update.'
KEY = 'a' * 64 + '.' + 'b' * 32


def claude(event, **extra):
    return dict(session_id='claude-session', prompt_id='prompt-1', cwd=CWD,
                transcript_path='/not/read/session.jsonl', permission_mode='default',
                hook_event_name=event, **extra)


def codex(event, **extra):
    return dict(session_id='codex-thread', turn_id='turn-7', cwd=CWD, transcript_path=None,
                hook_event_name=event, model='codex', permission_mode='default', **extra)


def gemini(event, **extra):
    return dict(session_id='gemini-session', cwd=CWD, transcript_path='/not/read/session.json',
                hook_event_name=event, timestamp='2026-10-01T12:00:00.000Z', **extra)


def copilot(**extra):
    # camelCase registration selects a different input schema; no hook_event_name.
    return dict(sessionId='copilot-session', cwd=CWD, timestamp=1790856000000, **extra)


def send(name, phase, event):
    return turn_native.dispatch(name, phase, HOME, event)


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        for name in ('begin', 'observe', 'end', 'copilot_guard'):
            setattr(self, name, stack.enter_context(patch.object(turn_runtime, name)))
        self.begin.return_value = {'status': 'consulted', 'text': 'Apply preferences. ' + KEY, 'turn_key': KEY}
        self.end.return_value = {'status': 'complete', 'request_capture': False,
                                 'footer': FOOTER, 'message': FOOTER}
        self.copilot_guard.return_value = {'deny': True, 'turn_key': KEY, 'message': 'Refresh ' + KEY}

    def test_claude_submit_edit_failures_and_stop(self):
        self.assertEqual(send('claude', 'begin', claude('UserPromptSubmit', prompt=PROMPT)),
                         {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                 'additionalContext': 'Apply preferences. ' + KEY}})
        common = (HOME, 'claude', 'claude-session', CWD)
        self.begin.assert_called_once_with(*common, disable=False, enable=False, turn_id='prompt-1')
        write = claude('PostToolUse', tool_name='Write', tool_use_id='toolu_A',
                       tool_input={'file_path': CWD + '/draft.txt', 'content': SECRET},
                       tool_response={'type': 'create', 'filePath': CWD + '/draft.txt'})
        self.assertIsNone(send('claude', 'observe', write))
        self.observe.assert_called_once_with(*common, write=True, opaque=False, turn_id='prompt-1')
        self.observe.reset_mock()
        for override in ({'hook_event_name': 'PreToolUse'}, {'hook_event_name': 'PostToolUseFailure'},
                         {'tool_response': SECRET}, {'tool_response': {'error': SECRET}},
                         {'tool_response': {}}, {'agent_id': 'subagent'}, {'tool_name': 'mcp__maven__maven_local_write'},
                         {'tool_response': {'type': 'update', 'filePath': CWD + '/.maven/records.db'}}):
            with self.subTest(override=override):
                send('claude', 'observe', write | override)
        self.observe.assert_not_called()
        self.assertEqual(send('claude', 'end', claude('Stop', stop_hook_active=False, last_assistant_message=SECRET)),
                         {'systemMessage': FOOTER})
        self.end.assert_called_once_with(*common, turn_id='prompt-1')

    def test_codex_patch_envelope_not_attempt_or_shell_output(self):
        common = (HOME, 'codex', 'codex-thread', CWD)
        header = 'Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess. Updated the following files:\n'
        post = codex('PostToolUse', tool_name='apply_patch', tool_use_id='call-2',
                     tool_input={'command': '*** Begin Patch\n*** Add File: x\n+' + SECRET + '\n*** End Patch'},
                     tool_response=header + 'A x\n')
        send('codex', 'observe', post)
        self.observe.assert_called_once_with(*common, write=True, opaque=False, turn_id='turn-7')
        self.observe.reset_mock()
        send('codex', 'observe', {**post, 'tool_response': header + 'A .maven/state.db\nD .maven/project.json\n'})
        send('codex', 'observe', {**post, 'turn_id': None})
        send('codex', 'observe', {**post, 'hook_event_name': 'PreToolUse'})
        self.observe.assert_not_called()
        for change in ({'tool_name': 'Bash'}, {'tool_name': 'Bash', 'tool_response': ''},
                       {'tool_response': 'error\n' + header + 'A x'}, {'tool_response': header + 'truncated…'},
                       {'tool_response': 'Verification failed: ' + SECRET}):
            send('codex', 'observe', post | change)
            self.observe.assert_called_once_with(*common, write=False, opaque=True, turn_id='turn-7')
            self.observe.reset_mock()
        self.assertEqual(send('codex', 'end', codex('Stop', stop_hook_active=False, last_assistant_message=None)),
                         {'systemMessage': FOOTER})
        self.end.assert_called_once_with(*common, turn_id='turn-7')

    def test_codex_release_output_schemas(self):
        # jsonschema is included with the project's MCP extra. Do not fetch a
        # schema at test time or substitute a locally invented output parser.
        from jsonschema import Draft7Validator
        root = Path(__file__).with_name('fixtures') / 'turn_native'
        submit = Draft7Validator(json.loads((root / 'codex-user-prompt-submit.output.schema.json').read_text()))
        stop = Draft7Validator(json.loads((root / 'codex-stop.output.schema.json').read_text()))
        intro = send('codex', 'begin', codex('UserPromptSubmit', prompt=PROMPT))
        submit.validate(intro)
        self.assertIn(KEY, intro['hookSpecificOutput']['additionalContext'])
        self.end.return_value.update(status='incomplete', request_capture=True, message='Classify the turn.')
        denied = send('codex', 'end', codex('Stop', stop_hook_active=False, last_assistant_message=SECRET))
        stop.validate(denied)
        self.assertEqual(denied, {'decision': 'block', 'reason': 'Classify the turn.'})
        stop.validate(send('codex', 'end', codex('Stop', stop_hook_active=True)))

    def test_copilot_command_prompt_output_is_not_a_context_channel(self):
        submit = copilot(prompt='TURN OFF MAVEN FOR THIS SESSION', transformedPrompt=SECRET)
        self.assertIsNone(send('copilot', 'begin', submit))
        self.begin.assert_called_once_with(HOME, 'copilot', 'copilot-session', CWD, disable=True, enable=False)
        self.begin.reset_mock()
        for event in (copilot(toolName='bash', toolArgs='{}'), {**submit, 'sessionId': ''},
                      {**submit, 'agentId': 'subagent'}, {**submit, 'cwd': 'relative'}):
            send('copilot', 'begin', event)
        self.begin.assert_not_called()
        self.assertEqual(send('copilot', 'end', copilot(transcriptPath='/not/read', stopReason='end_turn',
                                                        stop_hook_active=False)),
                         {'type': 'progress', 'message': FOOTER})
        self.end.reset_mock()
        send('copilot', 'end', copilot(stopReason='cancelled', stop_hook_active=False))
        self.end.assert_not_called()

    def test_copilot_pretool_guards_consequential_and_receipt_calls(self):
        def guard(name, args):
            return send('copilot', 'guard', copilot(toolName=name, toolArgs=args))
        deny = {'permissionDecision': 'deny', 'permissionDecisionReason': 'Refresh ' + KEY}
        self.assertIsNone(guard('view', {'path': '/not/recorded'}))
        self.assertIsNone(guard('maven-maven_local_read', {'cwd': CWD}))
        self.copilot_guard.assert_not_called()
        self.assertEqual(guard('bash', {'command': SECRET}), deny)
        self.assertEqual(guard('unknown-mcp-tool', {'password': SECRET}), deny)
        self.copilot_guard.assert_called_with(HOME, 'copilot-session', CWD)
        args = {'cwd': CWD, 'turn_key': KEY, 'action': 'refresh'}
        for tool in ('maven-maven_turn', 'maven/maven_turn'):
            self.assertIsNone(guard(tool, json.dumps(args)))
            for invalid in ({'turn_key': 'another'}, {'cwd': '/elsewhere'}, {'action': 'begin'},
                            {'action': 'capture'}, {'action': 'finish'}):
                self.assertEqual(guard(tool, args | invalid), deny)
        self.assertEqual(guard('other-maven_turn', args), deny)
        self.copilot_guard.return_value.update(deny=False)
        self.assertIsNone(guard('bash', {'command': SECRET}))
        self.assertIsNone(guard('maven/maven_turn', args | {'action': 'finish'}))
        self.assertEqual(guard('maven/maven_turn', args | {'turn_key': 'other'}), deny)
        self.copilot_guard.return_value.update(off=True, turn_key=None)
        self.assertIsNone(guard('bash', {}))
        self.assertEqual(guard('maven/maven_turn', args)['permissionDecision'], 'deny')
        self.assertEqual(send('copilot', 'guard', copilot(toolName='maven-maven_turn',
                         toolArgs=args, agentId='child'))['permissionDecision'], 'deny')

    def test_missing_native_stop_retry_flag_uses_runtime_once_only(self):
        stop = copilot(transcriptPath='/not/read/anything', stopReason='end_turn')
        self.end.return_value.update(status='incomplete', request_capture=True, message='Classify the turn.')
        self.assertEqual(send('copilot', 'end', stop), {'decision': 'block', 'reason': 'Classify the turn.'})
        self.end.return_value.update(request_capture=False)
        self.assertEqual(send('copilot', 'end', stop), {'type': 'progress', 'message': FOOTER})

    def test_skip_ui_footer_only_when_the_actual_final_line_matches(self):
        for name, event, field in (('claude', claude('Stop'), 'last_assistant_message'),
                                   ('codex', codex('Stop'), 'last_assistant_message'),
                                   ('gemini', gemini('AfterAgent'), 'prompt_response')):
            self.assertIsNone(send(name, 'end', event | {field: 'Result\n\n' + FOOTER + '\r\n'}))
            for wrong in ('Reported: ' + FOOTER, FOOTER + '\nOne more thing.', FOOTER + ' '):
                self.assertEqual(send(name, 'end', event | {field: wrong}), {'systemMessage': FOOTER})
        # Copilot has no assistant text in its agentStop input.
        self.assertEqual(send('copilot', 'end', copilot(stopReason='end_turn', last_assistant_message=FOOTER)),
                         {'type': 'progress', 'message': FOOTER})

    def test_copilot_posttool_success_marker_is_required(self):
        post = copilot(toolName='edit', toolArgs=json.dumps({'path': CWD + '/result', 'new_str': SECRET}),
                       toolResult={'resultType': 'success', 'textResultForLlm': SECRET})
        common = (HOME, 'copilot', 'copilot-session', CWD)
        send('copilot', 'observe', post)
        self.observe.assert_called_once_with(*common, write=True, opaque=False)
        self.observe.reset_mock()
        for result in (None, {}, {'resultType': 'failure', 'textResultForLlm': 'success'}, {'result_type': 'success'}):
            send('copilot', 'observe', post | {'toolResult': result})
        send('copilot', 'observe', post | {'toolArgs': {'path': str(HOME / 'runtime/turns/key.json')}})
        self.observe.assert_not_called()
        send('copilot', 'observe', post | {'toolName': 'task'})
        self.observe.assert_called_once_with(*common, write=False, opaque=True)

    def test_gemini_agent_not_model_cycle_and_error_envelope(self):
        self.assertEqual(send('gemini', 'begin', gemini('BeforeAgent', prompt=PROMPT)),
                         {'hookSpecificOutput': {'hookEventName': 'BeforeAgent',
                                                 'additionalContext': 'Apply preferences. ' + KEY}})
        common = (HOME, 'gemini', 'gemini-session', CWD)
        self.begin.assert_called_once_with(*common, disable=False, enable=False)
        self.begin.reset_mock()
        send('gemini', 'begin', gemini('BeforeModel', prompt='turn off maven for this session'))
        self.begin.assert_not_called()
        post = gemini('AfterTool', tool_name='write_file', tool_input={'file_path': 'notes.txt', 'content': SECRET},
                      tool_response={'llmContent': SECRET, 'returnDisplay': {'fileName': 'notes.txt',
                                     'fileDiff': SECRET, 'isNewFile': True}})
        send('gemini', 'observe', post)
        self.observe.assert_called_once_with(*common, write=True, opaque=False)
        self.observe.reset_mock()
        for extra in ({'tool_response': dict(post['tool_response'], error={'type': 'PATH_NOT_IN_WORKSPACE'})},
                      {'tool_response': {'llmContent': 'Success'}}, {'hook_event_name': 'BeforeTool'},
                      {'mcp_context': {'server': 'private'}}, {'tool_name': 'write_todos'}):
            send('gemini', 'observe', post | extra)
        self.observe.assert_not_called()
        send('gemini', 'observe', post | {'tool_name': 'run_shell_command'})
        self.observe.assert_called_once_with(*common, write=False, opaque=True)
        self.end.return_value.update(status='incomplete', request_capture=True, message='Finish the current receipt.')
        stop = gemini('AfterAgent', prompt=PROMPT, prompt_response=SECRET, stop_hook_active=False)
        self.assertEqual(send('gemini', 'end', stop), {'decision': 'deny', 'reason': 'Finish the current receipt.'})
        self.assertEqual(send('gemini', 'end', stop | {'stop_hook_active': True}), {'systemMessage': FOOTER})

    def test_only_prompt_phase_can_toggle_and_claude_cannot_undo_opt_out(self):
        for name, submit, end in (
                ('claude', claude('UserPromptSubmit'), claude('Stop')),
                ('codex', codex('UserPromptSubmit'), codex('Stop')),
                ('gemini', gemini('BeforeAgent'), gemini('AfterAgent')),
                ('copilot', copilot(), copilot(stopReason='end_turn'))):
            self.begin.return_value = {'status': 'off', 'turn_key': None, 'text': ''}
            self.end.return_value = {'status': 'off', 'footer': '', 'message': '', 'request_capture': False}
            self.assertIsNone(send(name, 'begin', submit | {'prompt': ' Turn OFF MAVEN for this Session! '}))
            self.assertTrue(self.begin.call_args.kwargs['disable'])
            send(name, 'begin', submit | {'prompt': 'Explain “turn off MAVEN for this session”.'})
            self.assertFalse(self.begin.call_args.kwargs['disable'])
            self.assertIsNone(send(name, 'end', end | {'stop_hook_active': False,
                                                      'last_assistant_message': 'turn on maven for this session'}))
            output = send(name, 'begin', submit | {'prompt': 'Turn on MAVEN for this session'})
            self.assertEqual(self.begin.call_args.kwargs['enable'], name != 'claude')
            if name == 'claude':
                self.assertIn('remains off', output['systemMessage'])

    def test_failed_results_visible_without_synthetic_success_or_loop(self):
        self.end.return_value = {'status': 'failed', 'request_capture': False,
                                 'message': 'not saved', 'footer': 'MAVEN · P: capture failed.'}
        for name, stop in (('claude', claude('Stop')), ('codex', codex('Stop')),
                           ('gemini', gemini('AfterAgent')), ('copilot', copilot(stopReason='end_turn'))):
            out = send(name, 'end', stop | {'stop_hook_active': False})
            self.assertIn('MAVEN · P: capture failed.', out.values())
            self.assertNotIn('decision', out)


class NativeReceiptPrivacyTests(unittest.TestCase):
    def test_disabled_native_conversations_use_private_receipts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'install'
            home.mkdir(mode=0o700)
            cwd = Path(directory).resolve() / SECRET
            cwd.mkdir(mode=0o700)
            for name, event in (('claude', claude('UserPromptSubmit')), ('codex', codex('UserPromptSubmit')),
                                ('copilot', copilot()), ('gemini', gemini('BeforeAgent'))):
                submit = event | {'cwd': str(cwd), 'prompt': 'Turn off MAVEN for this session'}
                for event in (submit, submit | {'prompt': SECRET}):
                    out = turn_native.dispatch(name, 'begin', home, event)
                    if name == 'copilot':  # Output dropped; guard enforces off on receipt calls.
                        self.assertIsNone(out)
                    else:
                        cue = out['hookSpecificOutput']['additionalContext']
                        self.assertIn('MAVEN is off', cue)
                        self.assertNotIn(SECRET, cue)
            receipts = list((home / 'runtime/turns').glob('*.json'))
            self.assertEqual(len(receipts), 4)
            for receipt in receipts:
                self.assertEqual(json.loads(receipt.read_text()), {'off': True})
                self.assertEqual(receipt.stat().st_mode & 0o077, 0)
            self.assertEqual(list(cwd.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
