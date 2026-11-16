"""Offline consumer contracts for independent publication and CLI diagnostics."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import unittest
from unittest.mock import Mock, patch

from tools import git_workflow, workflow
from tools.memory import cli
from tools.memory.boundary import BoundaryError, MemoryBoundary
from tools.memory.client import MemoryClientError

SESSION = '22222222-2222-4222-8222-222222222222'
PROJECT = '11111111-1111-4111-8111-111111111111'
RESERVATION = '33333333-3333-4333-8333-333333333333'
CHECKPOINT = '44444444-4444-4444-8444-444444444444'
NOTE = '55555555-5555-4555-8555-555555555555'


class WorkspaceGitCLI(unittest.TestCase):
    def test_failed_check_review_generates_publication_parser_command(self):
        root = Path('/disposable-checkout')
        memory = Mock(session_id=SESSION, project_id=PROJECT)
        memory.freshness_issues.return_value = []
        memory.prepare_checkpoint.return_value = {'record_id': CHECKPOINT, 'revision': 1}
        registry = Mock()
        registry.reserve.return_value = {'reservation_id': RESERVATION}
        checks = [{'command': ['configured-check'], 'passed': False, 'returncode': 1}]
        policy = {'git_enabled': True, 'checkpoint': {'max_commit_message_chars': 100}}
        replacements = {
            'ensure_no_pending': None, 'maybe_receipt': None,
            'inspect_git': {'changed_paths': ['code.py'], 'head': 'abc', 'remote_sha': 'abc',
                            'branch': 'main', 'remote': 'origin', 'remote_url': 'reviewed-url'},
            'is_control_plane': False, 'checkpoint_issues': [], 'candidate_digest': 'digest',
            'run_project_checks': checks, 'candidate_paths': ['code.py'], 'head': 'abc',
            'index_paths': [], 'workflow_digest': 'workflow', 'receipt': None,
        }
        with ExitStack() as stack:
            for name, result in replacements.items():
                stack.enter_context(patch.object(git_workflow, name, return_value=result))
            report = git_workflow.inspect_checkpoint(root, policy, memory, registry, action='checkpoint')
        self.assertEqual(report['status'], 'approval_required')
        self.assertEqual(report['failed_checks'], checks)
        self.assertFalse(report['proposed']['integration_ready'])
        self.assertIn('INCLUDING FAILED CHECKS', report['next_action'])
        command = shlex.split(report['next_action'].split(', then ', 1)[1])
        self.assertEqual(command.pop(0), 'maven-git')
        parsed = git_workflow.parser().parse_args(command)
        self.assertEqual(parsed.apply, report['proposed']['approval_code'])
        self.assertEqual(parsed.action, 'checkpoint')
        self.assertTrue(parsed.writers_quiescent)

    def test_rerun_requires_exact_reviewed_checks_even_when_failure_was_approved(self):
        failed = [{'command': ['configured-check'], 'passed': False, 'returncode': 1}]
        proposal = {'checks': failed}
        for rerun, message in ((failed, 'validated exact failing candidate'),
                               ([dict(failed[0], passed=True, returncode=0)], 'results changed')):
            with self.subTest(rerun=rerun), patch.object(git_workflow, 'run_project_checks', return_value=rerun), \
                    patch.object(git_workflow, 'validate_approval', side_effect=BoundaryError('validated exact failing candidate')):
                with self.assertRaisesRegex((BoundaryError, git_workflow.WorkflowFailure), message):
                    git_workflow.create_checkpoint(Path('/unused'), {}, Mock(), Mock(), {}, proposal)

    def test_memory_closure_or_reference_edit_invalidates_unsubmitted_git_candidate(self):
        memory = MemoryBoundary.__new__(MemoryBoundary)
        memory.session_id, memory.project_id = SESSION, PROJECT
        authority = {'generation': 1, 'pages': []}
        original = {'record_id': CHECKPOINT, 'revision': 1, 'kind': 'checkpoint', 'data': {
            'state': 'prepared', 'session_id': SESSION, 'candidate_digest': 'digest', 'git_base': 'base',
            'memory_refs': [{'record_id': SESSION, 'revision': 1}, {'record_id': NOTE, 'revision': 1}],
            'summary': json.dumps({'authority': authority, 'reservation_id': RESERVATION})}}
        proposal = {'session_id': SESSION, 'project_id': PROJECT, 'memory': {'record_id': CHECKPOINT, 'revision': 1},
                    'candidate_digest': 'digest', 'base_head': 'base', 'reservation_id': RESERVATION}
        memory.read = Mock(return_value=original)
        memory.context = Mock(return_value={'authority_generation': 1, 'instructions': []})
        for revisions in ({SESSION: 2, NOTE: 1}, {SESSION: 1, NOTE: 2}):
            with self.subTest(revisions=revisions):
                memory.revisions = Mock(return_value=revisions)
                with self.assertRaisesRegex(BoundaryError, 'changed after approval'):
                    memory.verify_checkpoint(proposal)

    def test_git_auth_failure_never_revokes_shared_memory_credentials(self):
        registry = Mock()
        registry.caches.return_value = []
        registry.inspect.return_value = {'session': {'project_id': PROJECT, 'caches': []}}
        project = {'project_id': PROJECT, 'git_enabled': True}
        with patch('tools.project.load_project', return_value=project), \
                patch('tools.project.project_values', return_value={}), \
                patch('tools.memory.environment.load_environment', return_value={}), \
                patch('tools.memory.environment.configured_client', side_effect=MemoryClientError('authentication_required')), \
                patch('tools.coordination.SessionRegistry', return_value=registry), \
                patch('tools.coordination.session_lock', return_value=nullcontext()), \
                patch.object(git_workflow, 'load_policy', return_value={}), \
                patch.object(git_workflow, 'maybe_receipt', return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(git_workflow.main(['refresh-context', '--root', '/unused', '--session', SESSION]), 2)
        registry.revoke.assert_not_called()
        registry.close.assert_not_called()

    def test_git_writer_recovery_reconciles_original_request_first(self):
        memory, registry = Mock(session_id=SESSION), Mock()
        order = []
        memory.recover.side_effect = lambda: order.append('reconcile')
        registry.recover_writers.side_effect = lambda *args, **kwargs: order.append('writers')
        with patch.object(git_workflow, 'maybe_receipt', return_value=None):
            git_workflow.recover_work(Path('/unused'), {}, memory, registry, writer_tokens=['exact-token'])
        self.assertEqual(order, ['reconcile', 'writers'])
        registry.recover_writers.assert_called_once_with(Path('/unused'), SESSION, ['exact-token'], workers_stopped=True)

    def test_workflow_forwards_drain_and_exact_writer_tokens(self):
        service, maintenance = Mock(), Mock()
        maintenance.pause.return_value = {'status': 'paused'}
        service.recover.return_value = {'status': 'recovered'}
        maintenance.recover_checkpoint.return_value = {'status': 'recovered'}
        with patch('tools.memory.lifecycle.SessionService', return_value=service), \
                patch('tools.memory.maintenance.Maintenance', return_value=maintenance), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['pause', '--root', '/unused', '--session', SESSION, '--drain']), 0)
            self.assertEqual(workflow.main(['recover', '--root', '/unused', '--session', SESSION,
                                           '--worker-stopped', '--writer-token', 'exact-token']), 0)
            self.assertEqual(workflow.main(['recover', '--root', '/unused', '--session', SESSION,
                                           '--checkpoint-id', CHECKPOINT, '--worker-stopped', '--writer-token', 'exact-token']), 0)
        maintenance.pause.assert_called_once_with(anchor=None, drain=True)
        service.recover.assert_called_once_with(workers_stopped=True, writer_tokens=['exact-token'], observe_only=False)
        maintenance.recover_checkpoint.assert_called_once_with(CHECKPOINT, worker_stopped=True, writer_tokens=['exact-token'])

    def test_ranked_search_offset_and_safe_error_detail_reach_cli(self):
        client = Mock()
        with patch.object(cli, '_connection', return_value=({}, {})), \
                patch.object(cli, 'selected_session'), patch.object(cli, 'MemoryClient', return_value=client):
            for offset in (0, 20):
                args = cli.parser().parse_args(['record', 'list', '--query', 'ranked term', '--offset', str(offset)])
                cli._memory_action(args)
                self.assertEqual(client.invoke.call_args.args[1]['offset'], offset)
        output = io.StringIO()
        with patch.object(cli, '_memory_action', side_effect=MemoryClientError('invalid_request', detail='body_markdown required')), \
                redirect_stdout(output):
            self.assertEqual(cli.main(['record', 'list']), 2)
        self.assertEqual(json.loads(output.getvalue())['detail'], 'body_markdown required')

    def test_workflow_forwards_read_only_receipt_observation(self):
        service = Mock()
        service.recover.return_value = {'status': 'ready'}
        with patch('tools.memory.lifecycle.SessionService', return_value=service), \
                patch('tools.memory.maintenance.Maintenance'), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(['recover', '--root', '/unused', '--session', SESSION, '--observe-only']), 0)
        service.recover.assert_called_once_with(workers_stopped=False, writer_tokens=None, observe_only=True)

    def test_cli_only_auth_failures_show_selected_sign_in_handoff(self):
        handoff = {'type': 'aws_login', 'command': ['aws', '--profile', 'selected', 'login'],
                   'environment': {'AWS_LOGIN_CACHE_DIRECTORY': '/selected/cache'}}
        for code in ('authentication_required', 'not_authorized'):
            output = io.StringIO()
            with patch.object(cli, '_memory_action', side_effect=MemoryClientError(code)), \
                    patch.object(cli, 'load_environment', return_value={'selected': True}), \
                    patch('tools.memory.environment.authentication_handoff', return_value=handoff) as helper, \
                    redirect_stdout(output):
                self.assertEqual(cli.main(['record', 'list']), 2)
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
