"""Owner-key AWS sign-in boundaries, without a real terminal app, credentials, or AWS calls."""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import pty
import select
import shlex
import socket
import tempfile
import termios
import threading
import unittest
from unittest.mock import ANY, Mock, patch

from tools import login
from tools.memory.client import MemoryClientError


ACCOUNT = '123456789012'
PRINCIPAL = 'arn:aws:iam::' + ACCOUNT + ':user/owner'
HANDOFF = {'type': 'aws_login', 'command': ['aws', 'login', '--profile', 'owner', '--region', 'us-east-1'],
           'environment': {'AWS_LOGIN_CACHE_DIRECTORY': '/private/cache with spaces'},
           'purpose': 'MAVEN reconcile', 'profile': 'owner', 'account': ACCOUNT,
           'expected_principal': PRINCIPAL, 'deploy_profile': False}


class TerminalTests(unittest.TestCase):
    def test_key_is_read_only_in_foreground_prompt_and_terminal_flags_restored(self):
        for key, expected in ((b'x', True), (b'\x1b', False), (b'\x03', False)):
            with self.subTest(key=key):
                master, slave = pty.openpty()
                try:
                    # No controlling process group on this test PTY. See Ctrl-C as a byte, not a signal.
                    flags = termios.tcgetattr(slave)
                    flags[3] &= ~termios.ISIG
                    termios.tcsetattr(slave, termios.TCSANOW, flags)
                    result = []
                    worker = threading.Thread(target=lambda: result.append(login._confirm(slave, HANDOFF)), daemon=True)
                    worker.start()
                    prompt = b''
                    while b'Press any key' not in prompt:
                        self.assertTrue(select.select([master], [], [], 3)[0])
                        prompt += os.read(master, 1024)
                    self.assertIn(b'Profile: owner', prompt)
                    self.assertEqual(result, [])
                    os.write(master, key)
                    worker.join(3)
                    self.assertEqual(result, [expected])
                    self.assertEqual(termios.tcgetattr(slave), flags)
                finally:
                    os.close(master)
                    os.close(slave)

    def test_foreground_requires_tty_and_never_captures_aws_output(self):
        with patch('tools.login.sys.stdin.isatty', return_value=False), patch('tools.login.subprocess.run') as launch:
            self.assertEqual(login._foreground(HANDOFF), b'U')
            launch.assert_not_called()
        terminal = Mock()
        with patch('tools.login.sys.stdin.isatty', return_value=True), \
                patch('tools.login.sys.stdout.isatty', return_value=True), \
                patch('tools.login.sys.stderr.isatty', return_value=True), \
                patch('builtins.open') as opened, \
                patch('tools.login.os.tcgetpgrp', return_value=os.getpgrp()), \
                patch('tools.login.private_directory') as private, \
                patch('tools.login._confirm', side_effect=[False, True, True]) as confirm, \
                patch('tools.login.subprocess.run', return_value=Mock(returncode=0)) as launch, \
                patch('tools.login.inspect_login_identity', side_effect=[MemoryClientError('unexpected_login_identity'), {}]) as verify:
            opened.return_value.__enter__.return_value = terminal
            self.assertEqual(login._foreground(HANDOFF), b'C')
            launch.assert_not_called()
            private.assert_not_called()
            self.assertEqual(login._foreground(HANDOFF), b'I')
            self.assertEqual(login._foreground(HANDOFF), b'V')
            self.assertEqual(confirm.call_count, 3)
            private.assert_called_with(Path(HANDOFF['environment']['AWS_LOGIN_CACHE_DIRECTORY']))
            self.assertEqual(launch.call_args.args, (HANDOFF['command'],))
            self.assertEqual(launch.call_args.kwargs, {'env': {**os.environ, **HANDOFF['environment']},
                             'stdin': terminal, 'stdout': terminal, 'stderr': terminal, 'check': False})
            source = verify.call_args.args[0]
            self.assertEqual(source['MAVEN_AWS_EXPECTED_PRINCIPAL'], PRINCIPAL)
            self.assertEqual(source['MAVEN_ACCOUNT_ID'], ACCOUNT)
            self.assertFalse(verify.call_args.kwargs['deploy'])

    def test_bridge_opens_private_command_file_and_accepts_only_status_byte(self):
        # Exercise actual Unix socket and cleanup; substitute only the user's macOS Terminal.app.
        args = ['--env', "/private/config with spaces and 'quotes/.env"]
        paths = []
        def open_terminal(command, **options):
            self.assertEqual(command[:3], ['/usr/bin/open', '-b', 'com.apple.Terminal'])
            self.assertEqual(options['stdout'], login.subprocess.DEVNULL)
            self.assertEqual(options['stderr'], login.subprocess.DEVNULL)
            script = Path(command[3])
            self.assertEqual(script.stat().st_mode & 0o777, 0o700)
            self.assertEqual(script.parent.stat().st_mode & 0o777, 0o700)
            words = shlex.split(script.read_text().splitlines()[1])
            self.assertEqual(words[:-2], ['exec', login.sys.executable, '-I', '-m', 'tools.login', *args])
            self.assertEqual(words[-2], '--notify')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as child:
                child.connect(words[-1])
                child.sendall(b'V')
            paths.append(script)
            return Mock(returncode=0)
        # Patch the console-owner check only, not stat() on temporary artifacts.
        real_stat = os.stat
        def stat(path, *args, **kwargs):
            return Mock(st_uid=os.getuid()) if path == '/dev/console' else real_stat(path, *args, **kwargs)
        with patch('tools.login.sys.platform', 'darwin'), patch.dict(os.environ, {'SSH_CONNECTION': '', 'SSH_TTY': ''}), \
                patch('tools.login.os.stat', side_effect=stat), patch('tools.login.subprocess.run', side_effect=open_terminal):
            self.assertEqual(login._desktop(args), 'verified')
        self.assertFalse(paths[0].parent.exists())

    def test_manual_and_headless_keep_exact_command_never_launch_aws(self):
        with patch('tools.login.sys.platform', 'linux'), patch('tools.login.subprocess.run') as launch:
            for interactive, status in ((False, 'manual'), (True, 'terminal_unavailable')):
                result = login.offer(HANDOFF, ['--env', '/private/selected/.env'], interactive=interactive)
                self.assertEqual(result['login_status'], status)
                self.assertEqual(shlex.split(result['sign_in_command']),
                    ['AWS_LOGIN_CACHE_DIRECTORY=/private/cache with spaces', *HANDOFF['command']])
                self.assertIn('--binding', shlex.split(result['terminal_command']))
            launch.assert_not_called()
            for altered in (dict(HANDOFF, command=['sh', '-c', 'echo unsafe']),
                            dict(HANDOFF, environment={'OTHER': 'foo'})):
                with self.assertRaises(ValueError):
                    login.offer(altered, [], interactive=True)


class OperationTests(unittest.TestCase):
    def test_admin_verifies_source_but_never_advances_mutation_or_stale_handoff(self):
        admin = Mock()
        result = {'operation_id': 'original', 'revision': 15, 'step': 'update_service_apply', 'state': 'blocked',
                  'handoff': HANDOFF, 'receipts': [{'request': 'original'}]}
        admin.status.return_value = result
        with patch('tools.login.verify_source') as verify:
            retained = login.after_admin_login(admin, result, verify=True)
            verify.assert_called_once_with(HANDOFF)
        self.assertEqual(retained['operation_id'], result['operation_id'])
        self.assertEqual(retained['receipts'], result['receipts'])
        self.assertEqual(retained['login_status'], 'verified')
        admin.advance.assert_not_called()
        admin.status.return_value = dict(result, revision=16)
        self.assertEqual(login.after_admin_login(admin, result)['login_status'], 'handoff_changed')
        admin.advance.assert_not_called()
        admin.status.return_value = dict(result, step='deploy_access_inspect')
        login.after_admin_login(admin, admin.status.return_value)
        admin.advance.assert_called_once_with('original', expected_revision=15)

    def test_reconcile_after_login_discards_old_tokens_and_choices(self):
        from tools.mcp.server import MavenApplication, create_server
        with tempfile.TemporaryDirectory() as directory:
            app = MavenApplication(home=Path(directory))
            store = Mock(root=Path('/bound/project'))
            held = []
            @contextmanager
            def local(*args, **kwargs):
                held.append(True)
                try:
                    yield store
                finally:
                    held.pop()
            def offer(*args, **kwargs):
                self.assertFalse(held, 'never hold the project lock during human sign-in')
                self.assertTrue(kwargs['interactive'])
                return {'login_status': 'verified'}
            server = create_server(app)
            with patch.object(app, 'local', side_effect=local), patch.object(app, 'unbound_client') as client, \
                    patch('tools.project.load_project', return_value={'schema': 2}), \
                    patch('tools.project.require_binding'), patch('tools.login.daily_handoff', return_value=HANDOFF), \
                    patch('tools.login.offer', side_effect=offer) as launch, \
                    patch('tools.mcp.server.apply_review', side_effect=MemoryClientError('authentication_required')) as apply, \
                    patch('tools.mcp.server.inspect_views', return_value={'status': 'review_required'}) as inspect:
                result = asyncio.run(server.call_tool('maven_reconcile', {'action': 'apply', 'cwd': '/bound/project',
                    'project_id': 'b5b1b52f-b543-4cd4-a1f1-31d54ddcd2e7', 'interactive_login': True, 'local_token': 'old-local', 'cloud_token': 'old-cloud',
                    'choices': [{'id': 'selected', 'take': 'local', 'reason': 'old review'}]})).structured_content
                self.assertEqual(result, {'status': 'review_required', 'login_status': 'verified', 'resumed_action': 'inspect'})
                inspect.assert_called_once_with(store, ANY, limit=100)
                apply.assert_called_once()
                launch.assert_called_once()
                self.assertEqual(client.call_count, 2)

    def test_mcp_admin_login_is_typed_and_revision_bound(self):
        from tools.mcp.server import MavenApplication, create_server
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(MavenApplication(home=Path(directory)))
            state = {'operation_id': 'original', 'revision': 2, 'step': 'apply', 'state': 'blocked', 'handoff': HANDOFF}
            with patch('tools.mcp.server.AdminOperations') as factory, \
                    patch('tools.login.offer', return_value={'login_status': 'verified'}) as launch:
                controller = factory.return_value
                controller.status.return_value = state
                def call(revision):
                    return asyncio.run(server.call_tool('maven_admin', {'action': 'login', 'operation_id': 'original',
                                                                        'revision': revision})).structured_content
                self.assertEqual(call(1)['status'], 'error')
                launch.assert_not_called()
                self.assertEqual(call(2)['login_status'], 'verified')
                controller.advance.assert_not_called()
                controller.respond.assert_not_called()
                launch.assert_called_once_with(HANDOFF, ['--home', str(Path(directory).resolve()), '--operation',
                                                        'original', '--revision', '2'], interactive=True)
