"""Native harness lifecycle bridge; startup inspection never initializes or captures memory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

HARNESS_NAMES = ('omp', 'claude', 'codex', 'copilot', 'gemini')


def _identity(harness, payload, cwd=None, harness_session=None):
    """Only native event identity (or the extension's explicit arguments) is admissible."""
    if not isinstance(payload, dict):
        raise ValueError('Hook input must be a JSON object.')
    if payload.get('agent_id') or payload.get('agentId'):
        raise ValueError('A subagent must retain its delegated MAVEN binding, not initialize independently.')
    native = payload.get('sessionId' if harness == 'copilot' else 'session_id')
    if harness_session is not None and native is not None and native != harness_session:
        raise ValueError('Explicit and native conversation identities disagree.')
    native = harness_session if harness_session is not None else native
    if not isinstance(native, str) or not native.strip() or any(ord(char) < 32 for char in native):
        raise ValueError('Native conversation ID is missing or invalid; never infer it from cwd, PID or newest session.')
    location = str(cwd) if cwd is not None else payload.get('cwd')
    if not isinstance(location, str) or not Path(location).is_absolute():
        raise ValueError('The native hook must supply an absolute working directory.')
    return Path(location), native


def startup_instruction(home, cwd, harness, harness_session, inspection):
    arguments = {'cwd': str(cwd), 'harness': harness, 'harness_session': harness_session, 'action': 'inspect'}
    return (
        'MAVEN: checking this conversation before governed work. '
        'This native hook performed local inspection only; it did not initialize, capture memory or establish readiness. '
        f'Use the MAVEN MCP server configured for installation {json.dumps(str(home))}. '
        'Call maven_startup with these exact arguments: ' + json.dumps(arguments, ensure_ascii=False) + '. '
        'Follow its returned project/Home choices and recovery guidance; continue only the exact authorized binding. '
        'Never take over another session, auto-close a peer, or infer that an old peer is dead. '
        'Print the returned active line and report ready only after verified initialization with all current authority loaded. '
        'If unavailable, explain the blocker; do not claim ready. '
        'Before quitting, ask MAVEN to wrap up, checkpoint and finish this exact session. '
        'SessionEnd is best effort, not a memory save or an exit veto; forced exit may leave unfinished work. '
        'Local inspection: ' + json.dumps({key: inspection.get(key) for key in ('status', 'message', 'issues')}, ensure_ascii=False)
    )


def _context_output(harness, instruction):
    if harness == 'omp':
        return {'instruction': instruction}
    if harness == 'copilot':
        return {'additionalContext': instruction}
    specific = {'additionalContext': instruction}
    if harness in ('claude', 'codex'):
        specific['hookEventName'] = 'SessionStart'
    return {'hookSpecificOutput': specific}


def handle_event(home, harness, action, payload, *, cwd=None, harness_session=None):
    location, native = _identity(harness, payload, cwd, harness_session)
    if action == 'startup':
        from tools.startup import inspect_startup
        inspection = inspect_startup(home, location, harness=harness, harness_session=native)
        return _context_output(harness, startup_instruction(home, location, harness, native, inspection))
    if action == 'status':
        from tools.startup import inspect_shutdown
        return inspect_shutdown(home, location, harness=harness, harness_session=native)
    from tools.startup import record_shutdown
    record_shutdown(home, location, harness=harness, harness_session=native,
                    reason=payload.get('reason', 'exit'))
    # Native SessionEnd schemas do not accept a close/continuation decision.
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('startup', 'status', 'shutdown'))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--harness', choices=HARNESS_NAMES, required=True)
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--harness-session')
    args = parser.parse_args(argv)
    try:
        if not args.home.is_absolute():
            raise ValueError('--home must name the explicit absolute installation path.')
        # The OMP extension supplies both arguments; do not wait on its inherited stdin.
        payload = {} if args.cwd is not None and args.harness_session is not None else json.load(sys.stdin)
        result = handle_event(args.home, args.harness, args.action, payload,
                              cwd=args.cwd, harness_session=args.harness_session)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        message = f'MAVEN lifecycle check unavailable: {exc}'
        print(message, file=sys.stderr)
        if args.action == 'startup':
            result = _context_output(args.harness, message + ' Do not claim readiness or invent a session identity.')
        else:
            print(json.dumps({'status': 'unknown', 'message': message}) if args.action == 'status' else '{}')
            return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
