"""Deterministic local boundaries; no cloud calls, AWS logout, or Git publication."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tools.coordination import CoordinationError, SessionRegistry
from tools.memory.boundary import BoundaryError, MemoryBoundary, save_private
from tools.memory.client import MemoryClientError
from tools.workflow import WorkflowFailure, approval_code, candidate_digest, proposal_from_receipt


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / 'project'
        self.root.mkdir()
        self.registry = SessionRegistry(self.home / 'instance')
        self.project = str(uuid.uuid4())
        self.first, self.second = str(uuid.uuid4()), str(uuid.uuid4())
        self.values = {'MAVEN_AWS_PROFILE': 'test-only', 'MAVEN_AWS_LOGIN_CACHE': str(self.home / 'cache')}
        for identifier in (self.first, self.second):
            self.registry.register(self.root, self.project, identifier, self.values)
            self.registry.activate(self.root, identifier)

    def test_peer_acknowledgment_is_exact_and_blocks_new_writers(self):
        reservation = self.registry.reserve(self.root, self.first)['reservation_id']
        with self.assertRaises(CoordinationError):
            self.registry.quiescent(self.root, self.first, reservation)
        with self.assertRaises(CoordinationError):
            self.registry.pause(self.root, self.second, str(uuid.uuid4()))
        with self.assertRaises(CoordinationError):
            self.registry.register(self.root, self.project, str(uuid.uuid4()), self.values)
        with self.assertRaises(CoordinationError):
            with self.registry.writer(self.root, self.second):
                self.fail('reserved checkout accepted a writer')
        self.registry.pause(self.root, self.second, reservation)
        self.registry.quiescent(self.root, self.first, reservation)
        self.registry.release(self.root, self.first, activate=True)
        self.assertEqual(self.registry.inspect(self.root, self.second)['session']['state'], 'paused')
        self.registry.activate(self.root, self.second)

    def test_in_flight_writer_prevents_pause(self):
        with self.registry.writer(self.root, self.second):
            reservation = self.registry.reserve(self.root, self.first)['reservation_id']
            with self.assertRaises(CoordinationError):
                self.registry.pause(self.root, self.second, reservation)
        self.registry.pause(self.root, self.second, reservation)
        self.registry.quiescent(self.root, self.first, reservation)

    def test_unknown_peer_cloud_outcome_prevents_pause_and_checkpoint(self):
        reservation = self.registry.reserve(self.root, self.first)['reservation_id']
        self.registry.pause(self.root, self.second, reservation)
        save_private(self.root, f'.maven/runtime/sessions/{self.second}/pending-memory.json',
                     {'request_key': str(uuid.uuid4())})
        with self.assertRaises(CoordinationError):
            self.registry.pause(self.root, self.second, reservation)
        with self.assertRaises(CoordinationError):
            self.registry.quiescent(self.root, self.first, reservation)

    def test_ordinary_close_keeps_shared_access_until_last_session(self):
        logged_out = []

        def controlled_logout(cache):
            logged_out.append(cache)
            return []

        result = self.registry.close(self.root, self.first, controlled_logout)
        self.assertEqual(logged_out, [])
        self.assertEqual(result['retained_for_peers'][0]['sessions'], [self.second])
        self.assertEqual(self.registry.inspect(self.root, self.second)['session']['state'], 'active')
        self.registry.close(self.root, self.second, controlled_logout)
        self.assertEqual(len(logged_out), 1)


class ApprovalTests(unittest.TestCase):
    def test_other_session_cannot_reuse_same_approval(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        proposal = {'action': 'checkpoint', 'session_id': first, 'root': '/project', 'paths': ['file.txt'],
                    'base_head': 'a' * 40, 'expected_remote_sha': 'a' * 40,
                    'would_push_existing_commits': False}
        code = approval_code(proposal)
        proposal['approval_code'] = code
        receipt = {'status': 'approval_required', 'action': 'checkpoint', 'session_id': first,
                   'root': '/project', 'proposed': proposal}
        self.assertEqual(proposal_from_receipt(receipt, code), proposal)
        with self.assertRaises(WorkflowFailure):
            proposal_from_receipt(dict(receipt, session_id=second), code)
        proposal['paths'] = ['changed.txt']
        with self.assertRaises(WorkflowFailure):
            proposal_from_receipt(receipt, code)

    def test_candidate_fingerprint_detects_editor_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'file.txt'
            path.write_text('before\n')
            original = candidate_digest(root, ['file.txt'])
            path.write_text('after\n')
            edited = candidate_digest(root, ['file.txt'])
            self.assertNotEqual(original, edited)
            path.chmod(0o755)
            self.assertNotEqual(edited, candidate_digest(root, ['file.txt']))

    def test_explicit_session_pointer_rejects_cross_session_before_cloud_read(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid.uuid4())
            memory = MemoryBoundary(None, Path(directory), str(uuid.uuid4()), identifier)
            save_private(directory, memory.SESSION_PATH, {'record_id': str(uuid.uuid4()), 'revision': 1})
            with self.assertRaises(BoundaryError):
                memory.session()


class SessionRecoveryTests(unittest.TestCase):
    def test_legacy_handover_uses_recorded_time_not_random_identifier(self):
        older = {'record_id': 'ffffffff-ffff-4fff-8fff-ffffffffffff',
                 'recorded_at': '2026-01-01T00:00:00+00:00',
                 'data': {'state': 'closed', 'workflow': 'maven-workflow', 'summary': '{}'}}
        newer = {'record_id': '11111111-1111-4111-8111-111111111111',
                 'recorded_at': '2026-02-01T00:00:00+00:00', 'data': dict(older['data'])}
        memory = MemoryBoundary(None, Path('/project'), str(uuid.uuid4()), str(uuid.uuid4()))
        memory.search = lambda kind, state=None: iter([older, newer] if kind == 'session' else [])
        self.assertEqual(memory.project_context()['previous_handover'], newer)

    def test_identity_failure_revokes_shared_access_but_transient_failure_does_not(self):
        from tools.workflow import start_main
        for failure, revoked in (('unexpected_login_identity', True), ('transport_unavailable', False)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                root = home / 'project'
                root.mkdir()
                project = str(uuid.uuid4())
                save_private(root, '.maven/project.json', {
                    'schema': 1, 'project_id': project, 'root': str(root), 'remote': None,
                    'accepted_url': None, 'branch': None, 'git_enabled': False, 'checks': []})
                values = {'MAVEN_AWS_PROFILE': 'synthetic', 'MAVEN_AWS_LOGIN_CACHE': str(home / 'cache')}
                environment = home / '.env'
                environment.write_text('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
                environment.chmod(0o600)
                registry = SessionRegistry(home)
                peer = str(uuid.uuid4())
                registry.register(root, project, peer, values)
                registry.activate(root, peer)
                with patch.dict(os.environ, {'MAVEN_HOME': str(home)}), \
                        patch('tools.memory.environment.configured_client', side_effect=MemoryClientError(failure)), \
                        patch('tools.workflow.logout_cache', return_value=[]) as logout, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(start_main(['--root', str(root), '--env', str(environment), '--json']), 2)
                report = json.loads(output.getvalue())
                self.assertEqual(logout.call_count, int(revoked))
                self.assertEqual(registry.inspect(root, peer)['session']['state'],
                                 'access_revoked' if revoked else 'active')
                if revoked:
                    self.assertIn(peer, report['local_access_cleanup']['affected_sessions'])

if __name__ == '__main__':
    unittest.main()
