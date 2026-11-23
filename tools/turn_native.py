"""Native *local* command-hook contracts (no transcript, history or network reads).

Claude: https://code.claude.com/docs/en/hooks
Codex: https://developers.openai.com/codex/hooks/
Copilot: https://docs.github.com/en/copilot/reference/hooks-reference (camelCase events)
Gemini: https://geminicli.com/docs/hooks/reference/

Only prompt events are considered for opt-out. Native post-tool payloads stay
in this process; the observer gets booleans, not args, output, or filenames.
Stop feedback can request another response; a UI message is not an edit to an
already-emitted assistant message. Keep installer imports dependency-free.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


NATIVE_EVENTS = {
    'claude': ('UserPromptSubmit', 'PostToolUse', 'Stop'),
    'codex': ('UserPromptSubmit', 'PostToolUse', 'Stop'),
    'copilot': ('userPromptSubmitted', 'postToolUse', 'agentStop'),
    'gemini': ('BeforeAgent', 'AfterTool', 'AfterAgent'),
}
_WRITES = {
    'claude': ('Write', 'Edit', 'NotebookEdit'),
    'codex': ('apply_patch',),
    'copilot': ('create', 'edit', 'str_replace_editor', 'apply_patch'),
    'gemini': ('write_file', 'replace'),
}
_OPAQUE = {
    'claude': ('Bash', 'Agent', 'Task'),
    'codex': ('Bash', 'spawn_agent'),
    'copilot': ('bash', 'powershell', 'task'),
    'gemini': ('run_shell_command', 'generalist', 'codebase_investigator', 'cli_help', 'browser_agent'),
}
POST_MATCHERS = {name: '^(' + '|'.join(_WRITES[name] + _OPAQUE[name]) + ')$'
                 for name in NATIVE_EVENTS}
_PHASES = {'begin': 0, 'observe': 1, 'end': 2}
_COPILOT_READ = frozenset(('view', 'glob', 'grep', 'rg', 'ask_user',
                           'maven/maven_orient', 'maven-maven_orient',
                           'maven/maven_local_read', 'maven-maven_local_read'))
# Copilot exposes MCP names as server/tool and flattened server-tool. Match
# the specific installed server, never a suffix supplied by another server.
_COPILOT_TURN = frozenset(('maven/maven_turn', 'maven-maven_turn'))
# Codex's own apply_patch renderer. Never apply this to arbitrary shell/MCP text:
# https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/tests/suite/hooks.rs
_CODEX_PATCH = re.compile(r'Exit code: 0\nWall time: [0-9]+(?:\.[0-9]+)? seconds\n'
                          r'Output:\nSuccess\. Updated the following files:\n')


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and value.isprintable()


def _copilot_args(event):
    # Some releases wrap arguments in a JSON string. Only decode this for a
    # selected built-in writer or the one named local receipt tool.
    args = event.get('toolArgs')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            return {}
    return args if isinstance(args, dict) else {}


def _target(home, cwd, value):
    """Return write evidence for an identified target, never persist the path."""
    if not isinstance(value, str) or not value or '\x00' in value:
        return 'opaque'
    try:
        target = Path(cwd) / value
        install = Path(home).resolve()
        for path in (target, target.resolve()):
            if path.is_relative_to(install) or any(
                    part == '.maven' or part.startswith('.maven-preserved-') for part in path.parts):
                return None
    except (OSError, RuntimeError, ValueError):
        return 'opaque'
    return 'write'


def _patch_observation(home, cwd, response):
    if not isinstance(response, str):
        return 'opaque'
    header = _CODEX_PATCH.match(response)
    if not header:
        # No exit signal on many PostToolUse paths; never interpret free-form
        # success/failure-looking output as proof of a write.
        return 'opaque'
    rows = response[header.end():].splitlines()
    if not rows or not all(len(row) > 2 and row[:2] in ('A ', 'M ', 'D ') for row in rows):
        return 'opaque'
    outcomes = {_target(home, cwd, row[2:]) for row in rows}
    return 'write' if 'write' in outcomes else 'opaque' if 'opaque' in outcomes else None


def _observation(harness, event, home, cwd):
    name = event.get('toolName' if harness == 'copilot' else 'tool_name')
    if not isinstance(name, str) or (name not in _WRITES[harness] and name not in _OPAQUE[harness]):
        return None
    if harness == 'copilot':
        result = event.get('toolResult')
        if not isinstance(result, dict) or result.get('resultType') != 'success':
            return None
        if name in ('create', 'edit'):
            return _target(home, cwd, _copilot_args(event).get('path'))
        if name == 'str_replace_editor':
            args = _copilot_args(event)
            if args.get('command') == 'view':
                return None
            if args.get('command') in ('str_replace', 'create', 'insert', 'undo_edit'):
                return _target(home, cwd, args.get('path'))
        return 'opaque'  # compound patch, shell or delegated effects need classification

    if ('tool_response' not in event or event.get('mcp_context') is not None
            or event.get('mcp_server') is not None):
        return None
    result = event['tool_response']
    if harness == 'codex':
        if name == 'apply_patch':
            return _patch_observation(home, cwd, result)
        return 'opaque'  # Bash PostToolUse includes non-zero exits
    if not isinstance(result, dict) or result.get('error') is not None or result.get('isError') is True:
        return None
    if name in _OPAQUE[harness]:
        return 'opaque'
    if harness == 'gemini':
        if 'llmContent' not in result or 'returnDisplay' not in result:
            return None
        # AfterTool also fires for errors. In contrast to MCP data, this is
        # the built-in ToolResult envelope, with optional error checked above.
        args = event.get('tool_input')
        path = args.get('file_path') if isinstance(args, dict) else None
        return _target(home, cwd, path)
    if name == 'NotebookEdit':
        if 'updated_file' not in result:
            return None
        return _target(home, cwd, result.get('notebook_path'))
    if name == 'Write' and result.get('type') not in ('create', 'update'):
        return None
    if name == 'Edit' and not isinstance(result.get('structuredPatch'), list):
        return None
    return _target(home, cwd, result.get('filePath'))


def _guard(runtime, home, conversation, cwd, event):
    name = event.get('toolName')
    if not isinstance(name, str) or not name or 'toolResult' in event:
        return None
    if name in _COPILOT_READ:
        return None
    ready = runtime.copilot_guard(home, conversation, cwd)
    if name in _COPILOT_TURN:
        if ready.get('off'):
            return {'permissionDecision': 'deny', 'permissionDecisionReason':
                    'MAVEN is off for this conversation. Do not use turn capture.'}
        args = _copilot_args(event)
        bound = (ready.get('turn_key') and args.get('turn_key') == ready['turn_key']
                 and args.get('cwd') == cwd)
        if bound and (args.get('action') == 'refresh' or not ready['deny']):
            return None
        return {'permissionDecision': 'deny', 'permissionDecisionReason':
                ready.get('message') or 'Use the current turn key and working directory.'}
    if ready['deny']:
        return {'permissionDecision': 'deny', 'permissionDecisionReason': ready['message']}
    return None  # never grant permission; native approval/sandbox policy still applies


def dispatch(harness, phase, home, event):
    """Process exactly one native event; return a structured response or silence."""
    from tools import turn_runtime as runtime

    if harness not in NATIVE_EVENTS or not isinstance(event, dict):
        return None
    guard = harness == 'copilot' and phase == 'guard'
    if not guard and phase not in _PHASES:
        return None
    expected = 'preToolUse' if guard else NATIVE_EVENTS[harness][_PHASES[phase]]
    # Copilot's camelCase protocol normally has no event tag; registration
    # selects the phase. Do not mix it with the PascalCase VS Code wire shape.
    if event.get('hook_event_name', expected if harness == 'copilot' else None) != expected:
        return None
    if event.get('agentId' if harness == 'copilot' else 'agent_id'):
        # The parent's delegate result remains opaque. A child cannot claim
        # the owner's receipt, even when normal tool use is permitted.
        if guard and isinstance(event.get('toolName'), str) and event['toolName'] in _COPILOT_TURN:
            return {'permissionDecision': 'deny', 'permissionDecisionReason':
                    'The parent agent owns this conversation’s MAVEN turn receipt.'}
        return None
    conversation = event.get('sessionId' if harness == 'copilot' else 'session_id')
    cwd = event.get('cwd')
    if (not _identifier(conversation) or not isinstance(cwd, str) or '\x00' in cwd
            or not Path(cwd).is_absolute()):
        return None  # no cwd-only lookup or fallback to an ambient profile
    if guard:
        return _guard(runtime, home, conversation, cwd, event)

    native_id = event.get('turn_id') if harness == 'codex' else (
        event.get('prompt_id') if harness == 'claude' else None)
    if (native_id is not None or harness == 'codex') and not _identifier(native_id):
        return None
    identity = {'turn_id': native_id} if native_id is not None else {}
    if phase == 'begin':
        prompt = event.get('prompt')
        if not isinstance(prompt, str):
            return None
        disable, enable = runtime.control(prompt)
        # Claude also runs UserPromptSubmit for scheduled and cross-session
        # messages, without a trustworthy author field. Allow less capture,
        # never allow such a message to undo the owner's privacy choice.
        result = runtime.begin(home, harness, conversation, cwd, disable=disable,
                               enable=enable and harness != 'claude', **identity)
        if harness == 'claude' and enable and result['status'] == 'off':
            return {'systemMessage': 'MAVEN remains off. Start a new Claude conversation to re-enable it.'}
        if harness == 'copilot' or not result.get('text'):
            # Config-file userPromptSubmitted command output is dropped. Do
            # not rewrite/echo the transformed (potentially secret) prompt.
            return None
        return {'hookSpecificOutput': {'hookEventName': expected, 'additionalContext': result['text']}}
    if phase == 'observe':
        effect = _observation(harness, event, home, cwd)
        if effect:
            runtime.observe(home, harness, conversation, cwd, write=effect == 'write',
                            opaque=effect == 'opaque', **identity)
        return None
    if harness == 'copilot' and event.get('stopReason') != 'end_turn':
        return None
    result = runtime.end(home, harness, conversation, cwd, **identity)
    if result['status'] == 'off':
        return None
    if result['request_capture'] and event.get('stop_hook_active', False) is False:
        return {'decision': 'deny' if harness == 'gemini' else 'block', 'reason': result['message']}
    footer = result.get('footer')
    if not footer:
        return None
    # Current Claude/Codex/Gemini include the actual final assistant text.
    # Copilot agentStop does not; never read transcriptPath as a substitute.
    if harness != 'copilot':
        last = event.get('prompt_response' if harness == 'gemini' else 'last_assistant_message')
        if isinstance(last, str):
            end = len(last)
            while end and last[end - 1] in '\r\n':
                end -= 1
            start = last.rfind('\n', 0, end) + 1
            if end - start == len(footer) and last.startswith(footer, start):
                return None
    # Copilot consumes single-line progress objects in the timeline, not as a
    # decision object. The other clients expose systemMessage to the UI.
    return {'type': 'progress', 'message': footer} if harness == 'copilot' else {'systemMessage': footer}
