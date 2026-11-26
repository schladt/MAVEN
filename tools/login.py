"""Owner-terminal AWS sign-in. MCP never reads terminal keys or AWS command output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import termios
import tty

from tools.memory.client import MemoryClientError
from tools.memory.environment import fresh_sign_in, load_environment, sign_in
from tools.setup_common import SetupPending, expected_account, inspect_login_identity, private_directory


# The entire terminal-to-MCP protocol: one bounded outcome, never AWS output.
_OUTCOMES = {b'V': 'verified', b'C': 'cancelled', b'F': 'login_failed',
             b'I': 'identity_not_verified', b'U': 'terminal_unavailable', b'S': 'handoff_changed'}
_INSPECTIONS = {'identity_inspect', 'deploy_identity_inspect', 'deploy_access_inspect'}
_NEXT_ACTION = {
    'manual': 'Run the offered terminal command in your own foreground terminal.',
    'cancelled': 'Leave sign-in paused. When ready, request a fresh sign-in handoff from MAVEN.',
    'login_failed': 'Check the AWS login diagnostic in your private terminal and the selected profile before signing in again.',
    'identity_not_verified': 'Compare the AWS identity with the account and principal in the handoff before continuing.',
    'terminal_unavailable': 'Request the current terminal command from MAVEN and run it in your own foreground terminal.',
    'handoff_changed': 'Ask MAVEN for the current sign-in handoff; do not reuse the old command.',
    'login_timed_out': 'Check the open sign-in terminal and ask MAVEN to verify the selected identity before starting another login.',
}


def daily_handoff(env):
    values = load_environment(env)
    selected = sign_in(values)
    return {'type': 'aws_login', 'command': selected['argv'], 'environment': selected['environment'],
            'purpose': 'MAVEN reconcile', 'profile': selected['profile'], 'account': expected_account(values),
            'expected_principal': values['MAVEN_AWS_EXPECTED_PRINCIPAL'], 'deploy_profile': False}


def _selection(handoff):
    """Reconstruct only an allowlisted aws login; persisted state is not a shell program."""
    argv, environment = handoff['command'], handoff['environment']
    fresh = handoff.get('fresh_mfa') is True
    if (handoff.get('type') != 'aws_login' or not isinstance(argv, list) or len(argv) not in (6, 7)
            or not isinstance(environment, dict)
            or set(environment) - ({'BROWSER'} if fresh else set()) != {'AWS_LOGIN_CACHE_DIRECTORY'}):
        raise ValueError('Invalid AWS sign-in handoff')
    # Old profile-mode checkpoints predate the explicit source marker.
    deploy = handoff.get('deploy_profile', handoff.get('purpose') == 'Maintenance access')
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    values = {prefix + 'PROFILE': argv[3], prefix + 'LOGIN_CACHE': environment['AWS_LOGIN_CACHE_DIRECTORY'],
              'MAVEN_AWS_REGION': argv[5]}
    expected = handoff.get('expected_principal', 'Unknown')
    if expected != 'Unknown':
        values[prefix + 'EXPECTED_PRINCIPAL'] = expected
    if handoff.get('account', '').isdigit():
        values['MAVEN_ACCOUNT_ID'] = handoff['account']
    selected = sign_in(values, deploy=deploy)
    if fresh:
        # Role elevation: only an allowlisted private-window launcher or the plain-URL flow.
        selected = fresh_sign_in(selected, environment.get('BROWSER'))
    if argv != selected['argv'] or environment != selected['environment']:
        raise ValueError('Invalid AWS sign-in handoff')
    return selected, values, deploy


def confirmed_command(handoff):
    """Validate an already confirmed TUI/terminal launch and its private cache."""
    selected, _, _ = _selection(handoff)
    private_directory(Path(selected['environment']['AWS_LOGIN_CACHE_DIRECTORY']))
    return selected


def verify_source(handoff):
    _, values, deploy = _selection(handoff)
    # An unbound setup profile is inspected, never approved or saved by this helper.
    return inspect_login_identity(values, deploy=deploy)


def _digest(handoff):
    return hashlib.sha256(json.dumps(handoff, sort_keys=True).encode()).hexdigest()


def _confirm(fd, handoff):
    def line(text):
        os.write(fd, (text + '\n').encode())
    selected, _, _ = _selection(handoff)
    # Local state/path strings should never be interpreted as terminal escapes.
    detail = ' | '.join(str(item) for item in (handoff.get('purpose', 'MAVEN'),
        'Profile: ' + selected['profile'], 'Account: ' + handoff.get('account', 'Unknown'),
        'Expected principal: ' + handoff.get('expected_principal', 'Unknown')))
    line('AWS sign-in for ' + ''.join(c if c.isprintable() else '?' for c in detail))
    if selected.get('note'):
        line(selected['note'])
    if handoff.get('reason') == 'deployment_elevation_denied':
        line('Elevation denied. Fresh MFA usually resolves it; AWS may also deny the role trust or permissions.')
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSAFLUSH)
        line('Press any key to launch AWS login. Esc or Ctrl-C cancels. This does not approve a change.')
        return os.read(fd, 1) not in (b'', b'\x1b', b'\x03')
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)


def _foreground(handoff):
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return b'U'
    try:
        with open('/dev/tty', 'r+b', buffering=0) as terminal:
            fd = terminal.fileno()
            if os.tcgetpgrp(fd) != os.getpgrp():
                return b'U'
            if not _confirm(fd, handoff):
                return b'C'
            selected = confirmed_command(handoff)
            result = subprocess.run(selected['argv'], env={**os.environ, **selected['environment']},
                                    stdin=terminal, stdout=terminal, stderr=terminal, check=False)
            if result.returncode != 0:
                return b'F'
            try:
                verify_source(handoff)
            except (SetupPending, MemoryClientError):
                return b'I'
            return b'V'
    except KeyboardInterrupt:
        return b'C'
    except (OSError, termios.error):
        return b'U'


def _desktop(args):
    """Open a new Terminal.app window for the logged-in console owner. No Apple events/keystrokes."""
    if (sys.platform != 'darwin' or os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY')
            or os.stat('/dev/console').st_uid != os.getuid()):
        return 'terminal_unavailable'
    # AF_UNIX paths on macOS are limited to 103 bytes; user TMPDIR may itself be longer.
    with tempfile.TemporaryDirectory(prefix='maven-login-', dir='/tmp') as temporary, \
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        directory = Path(temporary)
        address = str(directory / 'status')
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(20)
        program = directory / 'AWS sign-in.command'
        command = [sys.executable, '-I', '-m', 'tools.login', *args, '--notify', address]
        program.write_text('#!/bin/sh\nexec ' + shlex.join(command) + '\n', encoding='utf-8')
        program.chmod(0o700)
        opened = subprocess.run(['/usr/bin/open', '-b', 'com.apple.Terminal', str(program)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        if opened.returncode:
            return 'terminal_unavailable'
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(600)
            try:
                return _OUTCOMES.get(connection.recv(1), 'cancelled')
            except TimeoutError:
                return 'login_timed_out'


def offer(handoff, args, *, interactive):
    """Return manual alternatives on every handoff; open a real terminal only when requested."""
    exact, _, _ = _selection(handoff)
    args = [*args, '--binding', _digest(handoff)]
    status = 'manual'
    if interactive:
        try:
            status = _desktop(args)
        except (OSError, subprocess.TimeoutExpired):
            status = 'terminal_unavailable'
    executable = Path(sys.executable).with_name('maven-login')
    helper = [str(executable)] if executable.is_file() else [sys.executable, '-I', '-m', 'tools.login']
    result = {'login_status': status, 'terminal_command': shlex.join([*helper, *args]),
              'sign_in_command': exact['command']}
    if status != 'verified':
        result['next_action'] = _NEXT_ACTION[status]
    return result


def current_admin_handoff(result, revision):
    if (result['revision'] != revision or result['state'] != 'blocked' or result.get('pending_interaction')
            or result.get('handoff', {}).get('type') != 'aws_login'):
        raise ValueError('No current AWS login handoff for that revision')
    return result['handoff']


def after_admin_login(controller, result, *, verify=False):
    """Advance only a read-only identity inspection. Never dispatch a stopped effect."""
    identifier = result['operation_id']
    current = controller.status(identifier)
    if current.get('revision') != result.get('revision') or current.get('handoff') != result.get('handoff'):
        return {**current, 'login_status': 'handoff_changed', 'next_action': _NEXT_ACTION['handoff_changed']}
    if current['step'] in _INSPECTIONS:
        return controller.advance(identifier, expected_revision=current['revision'])
    if verify:
        verify_source(current['handoff'])
    return {**current, 'login_status': 'verified', 'next_action': 'Inspect the retained operation and its receipts before continuing. No stopped mutation was replayed.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--env', type=Path, help='Selected daily-use environment file')
    source.add_argument('--home', type=Path, help='Instance home of a blocked administration operation')
    parser.add_argument('--operation')
    parser.add_argument('--revision', type=int)
    parser.add_argument('--binding', help='Expected digest of the exact sign-in handoff')
    parser.add_argument('--notify', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    channel = None
    outcome = b'S'
    try:
        if args.notify:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.connect(args.notify)
        if args.env:
            handoff = daily_handoff(args.env)
        else:
            from tools.administration import AdminOperations
            handoff = current_admin_handoff(AdminOperations(args.home).status(args.operation), args.revision)
        outcome = (b'S' if args.binding and _digest(handoff) != args.binding else _foreground(handoff))
    except KeyboardInterrupt:
        outcome = b'C'
    except Exception:
        # The private terminal protocol only reports a bounded outcome, never provider details.
        outcome = b'S'
    finally:
        if channel is not None:
            try:
                channel.sendall(outcome)
            except OSError:
                pass
            channel.close()
    print('MAVEN sign-in: ' + _OUTCOMES[outcome] + '. No administration or reconciliation change was submitted.')
    if outcome != b'V':
        print('Next action: ' + _NEXT_ACTION[_OUTCOMES[outcome]], file=sys.stderr)
    return 0 if outcome == b'V' else 130 if outcome == b'C' else 1


if __name__ == '__main__':
    sys.exit(main())
