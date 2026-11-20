"""Offline consumer contracts for independent publication and CLI diagnostics."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import git_workflow as git
from tools.memory import cli
from tools.uninstall.common import read_private, save_private
from tools.memory.client import MemoryClientError

INSTANCE = '22222222-2222-4222-8222-222222222222'
PROJECT = '11111111-1111-4111-8111-111111111111'
EPOCH = '33333333-3333-4333-8333-333333333333'
SOURCE = {'origin': 'owner', 'excerpt': 'Publish exactly this reviewed commit to the shown destination.'}


class GitPublication(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name).resolve()
        self.root, self.remote, self.home = parent / 'checkout', parent / 'bare', parent / 'home'
        self.home.mkdir(mode=0o700)
        environment = self.home / '.env'
        environment.write_text(f'MAVEN_MEMORY_INSTANCE_ID={INSTANCE}\nMAVEN_MEMORY_RECOVERY_EPOCH={EPOCH}\n')
        environment.chmod(0o600)
        self.enterContext(patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                                                  'MAVEN_HOME': str(self.home)}))
        self.enterContext(patch('tools.memory.environment.configured_client',
                                side_effect=AssertionError('Git must not connect to cloud memory')))
        self.cmd(parent, 'init', '--bare', str(self.remote))
        self.cmd(parent, 'init', '-b', 'main', str(self.root))
        self.cmd(self.root, 'config', 'user.name', 'Publication fixture')
        self.cmd(self.root, 'config', 'user.email', 'fixture@example.invalid')
        self.cmd(self.root, 'remote', 'add', 'origin', str(self.remote))
        (self.root / '.gitignore').write_text('.maven/\n')
        self.cmd(self.root, 'add', '.gitignore')
        self.cmd(self.root, 'commit', '-m', 'Base')
        self.cmd(self.root, 'push', 'origin', 'main')
        self.base = self.cmd(self.root, 'rev-parse', 'HEAD')
        self.project = {'schema': 2, 'root': str(self.root), 'project_id': PROJECT,
                        'instance_id': INSTANCE, 'recovery_epoch': EPOCH, 'git_enabled': True,
                        'remote': 'origin', 'branch': 'main', 'accepted_url': str(self.remote), 'checks': []}
        self.config = self.root / '.maven/project.json'
        save_private(self.config, self.project)
        self.file = self.root / 'candidate.txt'
        self.file.write_text('Reviewed content.\n')

    @staticmethod
    def cmd(root, *arguments):
        return subprocess.run(['git', *arguments], cwd=root, check=True, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True).stdout.strip()

    def action(self, action, **kwargs):
        return git.publication(self.root, action, home=self.home, **kwargs)

    def review(self):
        return self.action('inspect', message='Publish reviewed content', writers_quiescent=True)

    @staticmethod
    def approval(review):
        return {'approved': True, 'source': SOURCE, 'candidate': review['candidate']}

    def test_review_publish_and_verify_exact_commit_without_memory(self):
        review = self.review()
        self.assertEqual(review['status'], 'approval_required')
        self.assertEqual(review['proposed']['paths'], ['candidate.txt'])
        self.assertEqual(review['proposed']['expected_remote_sha'], self.base)
        self.assertEqual(self.cmd(self.remote, 'rev-parse', 'main'), self.base)
        result = self.action('apply', approval=self.approval(review), writers_quiescent=True)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['remote_sha'], self.cmd(self.remote, 'rev-parse', 'main'))
        self.assertEqual(self.cmd(self.remote, 'rev-parse', 'main^'), self.base)
        self.assertEqual(self.cmd(self.remote, 'show', 'main:candidate.txt'), 'Reviewed content.')
        self.assertEqual(self.action('status')['publication']['approval']['source'], SOURCE)
        self.assertFalse((self.root / '.maven/runtime/sessions').exists())
        self.assertFalse((self.home / 'runtime/sessions.json').exists())

    def test_stale_or_unsourced_approval_cannot_create_commit(self):
        review = self.review()
        approval = self.approval(review)
        for bad in (None, dict(approval, approved=False), dict(approval, candidate='b' * 64),
                    dict(approval, source={'origin': 'owner', 'excerpt': ' '})):
            with self.subTest(bad=bad), self.assertRaises(git.WorkflowFailure):
                self.action('apply', approval=bad, writers_quiescent=True)
        with self.assertRaises(git.WorkflowFailure):
            self.action('apply', approval=approval)
        self.file.chmod(0o755)
        with self.assertRaisesRegex(git.WorkflowFailure, 'bytes or modes'):
            self.action('apply', approval=approval, writers_quiescent=True)
        self.assertEqual(self.cmd(self.root, 'rev-parse', 'HEAD'), self.base)
        self.action('cancel', candidate=review['candidate'], writers_quiescent=True)
        self.file.chmod(0o644)
        self.assertNotEqual(self.review()['candidate'], review['candidate'])

    def test_rejects_staging_secrets_and_changed_remote(self):
        self.cmd(self.root, 'add', 'candidate.txt')
        self.assertIn('index must be unmodified', ' '.join(self.review()['issues']))
        self.cmd(self.root, 'restore', '--staged', '--', 'candidate.txt')
        sensitive = self.root / 'credentials.json'
        sensitive.write_text('{}')
        self.assertIn('secret-bearing filename', ' '.join(self.review()['issues']))
        sensitive.unlink()
        review = self.review()
        self.cmd(self.root, 'remote', 'set-url', '--push', 'origin', str(self.root))
        with self.assertRaisesRegex(git.WorkflowFailure, 'push destination'):
            self.action('apply', approval=self.approval(review), writers_quiescent=True)
        self.cmd(self.root, 'remote', 'set-url', '--push', 'origin', str(self.remote))
        self.cmd(self.remote, 'update-ref', '-d', 'refs/heads/main')
        with self.assertRaisesRegex(git.WorkflowFailure, 'remote advanced'):
            self.action('apply', approval=self.approval(review), writers_quiescent=True)
        self.assertEqual(self.cmd(self.root, 'rev-parse', 'HEAD'), self.base)

    def test_failing_checks_must_be_reviewed_and_repeat_exactly(self):
        flag = self.home / 'check'
        save_private(self.config, {**self.project, 'checks': [[sys.executable, '-c',
                     'import pathlib,sys;sys.exit(int(not pathlib.Path(sys.argv[1]).exists()))', str(flag)]]})
        review = self.review()
        self.assertFalse(review['proposed']['integration_ready'])
        self.assertEqual(review['failed_checks'][0]['returncode'], 1)
        flag.touch()
        with self.assertRaisesRegex(git.WorkflowFailure, 'check results changed'):
            self.action('apply', approval=self.approval(review), writers_quiescent=True)
        self.assertEqual(self.cmd(self.root, 'rev-parse', 'HEAD'), self.base)

    def test_lost_push_reply_recovers_verified_commit_without_resubmission(self):
        approval = self.approval(self.review())
        original = git.git
        pushes = []

        def lose_reply(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if 'push' in args:
                pushes.append(args)
                raise git.WorkflowFailure('lost push reply')
            return result

        with patch.object(git, 'git', side_effect=lose_reply):
            self.assertEqual(self.action('apply', writers_quiescent=True, approval=approval)['status'], 'uncertain')
            observed = self.action('recover')['publication']
            self.assertEqual(observed['status'], 'unverified')
            with self.assertRaises(git.WorkflowFailure):
                self.action('cancel', writers_quiescent=True, candidate=approval['candidate'])
            recovered = self.action('recover', writers_quiescent=True, approval=approval)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(recovered['status'], 'published')
        self.assertEqual(recovered['remote_sha'], observed['commit_sha'])

    def test_interrupted_ref_installation_recovers_only_exact_index_and_object(self):
        approval = self.approval(self.review())
        original = os.replace

        def interrupt(source, target):
            if Path(target) == self.root / '.git/index':
                raise OSError('index installation interrupted')
            return original(source, target)

        with patch.object(os, 'replace', side_effect=interrupt):
            self.assertEqual(self.action('apply', writers_quiescent=True, approval=approval)['status'], 'uncertain')
        self.assertEqual(self.action('status')['publication']['status'], 'installing')
        self.assertEqual(self.cmd(self.remote, 'rev-parse', 'main'), self.base)
        self.assertEqual(self.action('recover', writers_quiescent=True, approval=approval)['status'], 'published')

    def test_mcp_configuration_needs_bound_cwd_and_exact_owner_evidence(self):
        import asyncio
        from tools.mcp.server import MavenApplication, create_server
        app = MavenApplication(home=self.home)
        self.addCleanup(app.close)
        server = create_server(app)

        def call(**kwargs):
            result = asyncio.run(server.call_tool('maven_git', kwargs))
            return result.structured_content

        save_private(self.config, {**self.project, 'git_enabled': False, 'branch': None,
                                   'accepted_url': None, 'remote': None})
        selected = {'branch': 'main', 'accepted_url': str(self.remote), 'remote': 'origin'}
        nested = self.root / 'subdir'
        nested.mkdir()
        review = call(cwd=str(nested), action='configure', configuration=selected)
        self.assertEqual(review['status'], 'approval_required')
        approval = self.approval(review)
        self.assertIn(call(cwd=str(nested), action='configure', approval=dict(approval, candidate='0' * 64))['status'],
                      ('error', 'blocked'))
        self.assertFalse(read_private(self.config)['git_enabled'])
        self.assertEqual(call(cwd=str(nested), action='configure', approval=approval)['status'], 'configured')
        review = call(cwd=str(nested), action='inspect', writers_quiescent=True, message='Explicit MCP publication')
        self.assertEqual(review['status'], 'approval_required')
        self.assertEqual(call(cwd=str(nested), action='apply', writers_quiescent=True,
                              approval=self.approval(review))['status'], 'published')


class SecurityCLIDiagnostics(unittest.TestCase):
    def test_safe_error_detail_reaches_cli(self):
        output = io.StringIO()
        with patch.object(cli, '_skill_action', side_effect=MemoryClientError('invalid_request', detail='body_markdown required')), \
                redirect_stdout(output):
            self.assertEqual(cli.main(['skill', 'list']), 2)
        self.assertEqual(json.loads(output.getvalue())['detail'], 'body_markdown required')

    def test_cli_only_auth_failures_show_selected_sign_in_handoff(self):
        handoff = {'type': 'aws_login', 'command': ['aws', '--profile', 'selected', 'login'],
                   'environment': {'AWS_LOGIN_CACHE_DIRECTORY': '/selected/cache'}}
        for code in ('authentication_required', 'not_authorized'):
            output = io.StringIO()
            with patch.object(cli, '_skill_action', side_effect=MemoryClientError(code)), \
                    patch.object(cli, 'load_environment', return_value={'selected': True}), \
                    patch('tools.memory.environment.authentication_handoff', return_value=handoff) as helper, \
                    redirect_stdout(output):
                self.assertEqual(cli.main(['skill', 'list']), 2)
            result = json.loads(output.getvalue())
            if code == 'authentication_required':
                self.assertEqual(result['authentication'], handoff)
                self.assertEqual(helper.call_args.args[1], {'selected': True})
            else:
                helper.assert_not_called()
                self.assertNotIn('authentication', result)

    def test_skill_catalog_repeated_cursor_is_rejected(self):
        client = Mock()
        client.invoke.return_value = {'status': 'ready', 'data': {
            'authority_generation': 1, 'pages': [{'slug': 'a', 'page_kind': 'skill'}], 'next_after_slug': 'a'}}
        with self.assertRaises(MemoryClientError):
            cli._skill_catalog(client)
        self.assertEqual(client.invoke.call_count, 2)
