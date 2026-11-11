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
from tools.memory.boundary import BoundaryError, MemoryBoundary, read_private, save_private
from tools.memory.client import MemoryClientError
from tools.git_workflow import WorkflowFailure, approval_code, candidate_digest, proposal_from_receipt


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
    def test_project_context_is_one_server_request_and_never_lists_self_as_peer(self):
        session, project = str(uuid.uuid4()), str(uuid.uuid4())
        handover = {'session_id': str(uuid.uuid4()), 'checkpoint_record_id': str(uuid.uuid4()), 'revision': 2,
                    'recorded_at': '2026-02-01T00:00:00+00:00', 'session_state': 'active', 'summary': 'Main line'}
        data = {'latest_handover': handover, 'active_peers': [], 'tasks': [], 'blockers': [],
                'owner_preferences': [{'record_id': str(uuid.uuid4()), 'revision': 1, 'title': 'Style',
                                       'body_markdown': 'Never name clients publicly.'}]}
        client = unittest.mock.MagicMock()
        client.invoke.return_value = {'status': 'ready', 'data': data}
        memory = MemoryBoundary(client, Path('/project'), project, session)
        self.assertEqual(memory.project_context(), data)
        client.invoke.assert_called_once_with('/v1/memory/project_context',
                                              {'project_id': project, 'session_id': session})
        client.invoke.return_value = {'status': 'ready', 'data': dict(data, active_peers=[{'session_id': session}])}
        with self.assertRaises(BoundaryError):
            memory.project_context()
        client.invoke.return_value = {'status': 'ready', 'data': dict(data, tasks=None)}
        with self.assertRaises(BoundaryError):
            memory.project_context()

    def test_identity_failure_revokes_shared_access_but_transient_failure_does_not(self):
        from tools.git_workflow import start_main
        for failure, revoked in (('unexpected_login_identity', True), ('transport_unavailable', False)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                root = home / 'project'
                root.mkdir()
                project = str(uuid.uuid4())
                save_private(root, '.maven/project.json', {
                    'schema': 1, 'project_id': project, 'root': str(root), 'remote': 'origin',
                    'accepted_url': 'git@example.org:owner/project.git', 'branch': 'main', 'git_enabled': True, 'checks': []})
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
                        patch('tools.git_workflow.logout_cache', return_value=[]) as logout, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(start_main(['--root', str(root), '--env', str(environment), '--json']), 2)
                report = json.loads(output.getvalue())
                self.assertEqual(logout.call_count, int(revoked))
                self.assertEqual(registry.inspect(root, peer)['session']['state'],
                                 'access_revoked' if revoked else 'active')
                if revoked:
                    self.assertIn(peer, report['local_access_cleanup']['affected_sessions'])

class MemoryCloud:
    """Deterministic cloud fixture with versioned records and exact request receipts."""
    def __init__(self):
        self.records, self.history, self.requests = {}, {}, {}
        self.calls = []
        self.context_loads = 0
        self.generation = 1
        self.last_generation = None
        self.sequence = {}
        self.lose_next = False
        self.closed = False

    def close(self):
        self.closed = True

    def load_context(self, scope):
        self.context_loads += 1
        self.last_generation = self.generation
        return {'status': 'ready', 'authority_generation': self.generation, 'data': {
            'authority_generation': self.generation, 'instructions': [
                {'slug': 'core', 'revision': 1, 'content_hash': 'a' * 64,
                 'page_kind': 'directive', 'body_markdown': 'Complete current governing instructions.'}]}}

    def invoke(self, route, body):
        response = self._invoke(route, body)
        self.last_generation = self.generation
        return {**response, 'authority_generation': self.generation}

    @staticmethod
    def _owner(record):
        return record.get('data_scope') == 'owner'

    def _project_context(self, body):
        import json as _json
        project, requester = body['project_id'], body['session_id']
        sessions = {identifier: record for identifier, record in self.records.items()
                    if record['kind'] == 'session' and record.get('project_id') == project}
        checkpoints = sorted((record for record in self.records.values()
                              if record['kind'] == 'checkpoint' and record['data'].get('session_id') in sessions),
                             key=lambda record: self.sequence.get(record['record_id'], 0))
        latest = {}
        for record in checkpoints:
            latest[record['data']['session_id']] = record
        handover = None
        if checkpoints:
            record = checkpoints[-1]
            handover = {'session_id': record['data']['session_id'], 'checkpoint_record_id': record['record_id'],
                        'revision': record['revision'], 'recorded_at': None,
                        'session_state': sessions[record['data']['session_id']]['data']['state'],
                        'summary': record['data'].get('summary')}
        peers = [{'session_id': identifier,
                  'harness': _json.loads(record['data']['summary']).get('harness'), 'started_at': None,
                  'latest_checkpoint_at': None,
                  'latest_summary': latest[identifier]['data'].get('summary') if identifier in latest else None}
                 for identifier, record in sessions.items()
                 if identifier != requester and record['data']['state'] == 'active']

        def items(kind, states):
            return [{'record_id': record['record_id'], 'revision': record['revision'],
                     'title': record['data'].get('title'), 'state': record['data'].get('state'),
                     'tags': record.get('tags', [])}
                    for record in sorted(self.records.values(), key=lambda value: value['record_id'])
                    if record['kind'] == kind and self._owner(record) and record['data'].get('state') in states
                    and record.get('project_id') in (None, project)]

        preferences = [{'record_id': record['record_id'], 'revision': record['revision'],
                        'title': record['data']['title'], 'body_markdown': record['data']['body_markdown'][:1000]}
                       for record in self.records.values()
                       if record['kind'] == 'decision' and self._owner(record) and record.get('project_id') is None
                       and record['data'].get('state') == 'accepted'][:20]
        return {'latest_handover': handover, 'active_peers': peers,
                'tasks': items('task', ('open', 'in_progress', 'blocked')), 'blockers': items('risk', ('open',)),
                'owner_preferences': preferences}

    def _search(self, body):
        import copy
        import json as _json
        import re as _re
        records = [record for record in self.records.values()
                   if (body.get('kind') is None or record['kind'] == body['kind'])
                   and (body.get('state') is None or record['data'].get('state') == body['state'])
                   and (body.get('include_archived') or ('archived' not in record.get('tags', [])
                                                         and record['data'].get('state') != 'archived'))]
        scope = body.get('scope', 'project')
        records = [record for record in records if scope == 'all'
                   or scope == 'context' and record.get('project_id') in (None, body.get('project_id'))
                   or scope == 'universal' and record.get('project_id') is None
                   or scope == 'project' and record.get('project_id') == body.get('project_id')]
        limit = body.get('limit', 20)
        if body.get('query'):
            ranked = []
            for record in records:
                title = str(record['data'].get('title') or record['data'].get('name') or '')
                fields = (title, ' '.join(record.get('tags', [])), _json.dumps(record['data']))
                rank = 0
                for token in body['query'].lower().split():
                    pattern = r'\b' + _re.escape(token) + r'\b'
                    weights = [weight for weight, text in zip((3, 2, 1), fields) if _re.search(pattern, text.lower())]
                    if not weights:
                        break
                    rank += sum(weights)
                else:
                    ranked.append(dict(copy.deepcopy(record), rank=rank, snippet=None))
            ranked.sort(key=lambda record: (-record['rank'], record['record_id']))
            offset = body.get('offset', 0)
            page = ranked[offset:offset + limit]
            return {'records': page, 'next_offset': offset + limit if len(ranked) > offset + limit else None}
        records = sorted((record for record in records
                          if record['record_id'] > body.get('after_id', '')), key=lambda record: record['record_id'])
        page = [dict(copy.deepcopy(record), snippet=None) for record in records[:limit]]
        return {'records': page, 'next_after_id': page[-1]['record_id'] if len(records) > len(page) else None}

    def _invoke(self, route, body):
        import copy
        from tools.memory.validation import validate
        validate(route, body)
        self.calls.append((route, copy.deepcopy(body)))
        action = route.rsplit('/', 1)[1]
        if action == 'lookup':
            if body['request_key'] not in self.requests:
                raise MemoryClientError('operation_unavailable')
            return {'status': 'ready', 'data': copy.deepcopy(self.requests[body['request_key']][1])}
        if action == 'read':
            record = self.history.get((body['record_id'], body['revision'])) if 'revision' in body else self.records.get(body['record_id'])
            if record is None:
                raise MemoryClientError('record_unavailable')
            record = copy.deepcopy(record)
            if 'revision' in body:
                record['claim'] = None  # Real historical reads exclude mutable leases.
            return {'status': 'ready', 'data': record}
        if action == 'search':
            return {'status': 'ready', 'data': self._search(body)}
        if action == 'project_context':
            return {'status': 'ready', 'data': self._project_context(body)}
        if action == 'revisions':
            return {'status': 'ready', 'data': {'revisions': {
                identifier: self.records[identifier]['revision'] for identifier in body['record_ids']
                if identifier in self.records and self._owner(self.records[identifier])}}}
        key = body['request_key']
        if key in self.requests:
            original, result = self.requests[key]
            if original != body:
                raise MemoryClientError('invalid_request')
            return {'status': 'ready', 'data': dict(copy.deepcopy(result), replayed=True)}
        if body.get('authority_generation', self.generation) != self.generation:
            raise MemoryClientError('context_stale')
        current = self.records.get(body['record_id'])
        if (current['revision'] if current else 0) != body['expected_revision']:
            raise MemoryClientError('revision_conflict')
        revision = body['expected_revision'] + 1
        if action == 'write':
            record = {key: copy.deepcopy(body[key]) for key in ('record_id', 'kind', 'data', 'classification', 'data_scope')}
            record.update(revision=revision, project_id=body.get('project_id'), tags=body.get('tags', []))
            result = {'record_id': body['record_id'], 'revision': revision, 'request_key': key}
        else:
            record = copy.deepcopy(current)
            record['revision'] = revision
            if body['action'] == 'claim':
                record['claim'] = {'session_id': body['session_id'], 'fence': revision, 'expires_at': '2099-01-01T00:00:00Z'}
                record['data']['state'] = 'in_progress'
                result = dict(record['claim'])
            else:
                if current['claim']['fence'] != body['fence']:
                    raise MemoryClientError('revision_conflict')
                record['claim'] = None
                record['data']['state'] = 'completed' if body['action'] == 'complete' else 'open'
                result = {'session_id': None, 'fence': body['fence']}
            result.update(record_id=body['record_id'], revision=revision, request_key=key, state=record['data']['state'])
        self.records[body['record_id']] = record
        self.history[(body['record_id'], revision)] = copy.deepcopy(record)
        self.requests[key] = (copy.deepcopy(body), copy.deepcopy(result))
        self.sequence[body['record_id']] = len(self.requests)
        if self.lose_next:
            self.lose_next = False
            raise MemoryClientError('outcome_unknown', request_key=key)
        return {'status': 'ready', 'data': result}


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        from tools.memory.lifecycle import SessionService
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'ordinary-directory'
        self.root.mkdir()
        self.project = str(uuid.uuid4())
        self.identifier = str(uuid.uuid4())
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'project_id': self.project, 'root': str(self.root)})
        self.cloud = MemoryCloud()
        self.env = self.root.parent / 'home' / '.env'
        self.service = SessionService(self.root, self.identifier, env=self.env, client=self.cloud)
        skills = patch('tools.memory.skills.SessionSkills.start', return_value={'status': 'ready', 'data': {'resources': []}})
        skills.start()
        self.addCleanup(skills.stop)
        self.git = patch('tools.git_workflow.git', side_effect=AssertionError('memory invoked Git')).start()
        self.addCleanup(patch.stopall)
        self.source = {'origin': 'Owner conversation', 'excerpt': 'Preserve this relevant finding.'}
        self.note = {'title': 'Finding', 'body_markdown': 'Verified local behavior.', 'category': 'work'}

    def test_full_lifecycle_without_git_and_immutable_historical_checkpoint(self):
        initialized = self.service.initialize()
        self.assertEqual(initialized['status'], 'ready')
        self.assertIn('instructions', initialized['context'])
        self.assertEqual(self.service.initialize()['session_id'], self.identifier)
        note = self.service.save('note', self.note, source=self.source)
        updated = self.service.save('note', dict(self.note, body_markdown='Later finding.'), source=self.source,
                                    record_id=note['record_id'], expected_revision=note['revision'])
        identifier = str(uuid.uuid4())
        refs = [MemoryBoundary.ref(note)]
        checkpoint = self.service.checkpoint(summary='Point in time', refs=refs, cutoff=1, checkpoint_id=identifier)
        self.assertEqual(checkpoint['status'], 'checkpointed')
        self.assertEqual(self.cloud.records[identifier]['data']['memory_refs'], refs)
        self.assertEqual(self.service.checkpoint(summary='Point in time', refs=refs, cutoff=1,
                                                 checkpoint_id=identifier), checkpoint)
        with self.assertRaises(BoundaryError):
            self.service.checkpoint(summary='Changed', refs=[MemoryBoundary.ref(updated)], cutoff=2, checkpoint_id=identifier)
        result = self.service.finish(summary='Truthful handover; source remains unpublished.', checkpoint_ref=checkpoint['checkpoint'])
        self.assertEqual(result['status'], 'closed')
        self.assertEqual(self.service.status()['status'], 'closed')
        self.git.assert_not_called()

    def test_scope_consultation_and_memory_writes_ignore_git_reservation(self):
        self.service.initialize()
        git_session = str(uuid.uuid4())
        self.service.registry.register(self.root, self.project, git_session, {})
        self.service.registry.activate(self.root, git_session)
        reservation = self.service.registry.reserve(self.root, git_session)
        universal = self.service.save('note', self.note, source=self.source, scope='universal')
        self.assertEqual(universal['scope'], 'universal')
        project = self.service.save('note', self.note, source=self.source)
        other_id = str(uuid.uuid4())
        other = dict(project, record_id=other_id, project_id=str(uuid.uuid4()))
        self.cloud.records[other_id] = other
        self.cloud.history[(other_id, 1)] = other
        records = self.service.search(kind='note')['records']
        self.assertEqual({record['record_id'] for record in records}, {universal['record_id'], project['record_id']})
        with self.assertRaises(BoundaryError):
            self.service.read_record(other_id)
        self.assertEqual(self.service.read_record(other_id, scope='all')['project_id'], other['project_id'])
        self.assertEqual(len(self.service.search(kind='note', scope='all')['records']), 3)
        self.service.registry.quiescent(self.root, git_session, reservation['reservation_id'])

    def test_unknown_universal_write_blocks_project_write_and_recovers_original_key(self):
        from tools.memory.lifecycle import SessionService
        self.service.initialize()
        key = str(uuid.uuid4())
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.service.save('note', self.note, source=self.source, scope='universal', request_key=key)
        calls = len(self.cloud.calls)
        self.assertEqual(self.service.status()['pending_request']['request_key'], key)
        self.assertEqual(len(self.cloud.calls), calls)
        with self.assertRaises(BoundaryError):
            self.service.save('note', self.note, source=self.source)
        restored = SessionService(self.root, self.identifier, env=self.env, client=self.cloud)
        receipt = restored.recover()['recovery']
        self.assertEqual(receipt['result']['request_key'], key)
        replay = restored.save('note', self.note, source=self.source, scope='universal', request_key=key)
        self.assertEqual(MemoryBoundary.ref(replay), receipt['record'])
        self.assertEqual(len([record for record in self.cloud.records.values() if record['kind'] == 'note']), 1)

    def test_claim_fences_and_definitive_revision_conflict_leave_no_uncertain_write(self):
        self.service.initialize()
        task = self.service.save('task', {'title': 'Check', 'description': '', 'state': 'open'}, source=self.source)
        claimed = self.service.claim(task['record_id'], action='claim', expected_revision=1, lease_seconds=60, source=self.source)
        self.assertEqual(claimed['claim']['session_id'], self.identifier)
        completed = self.service.claim(task['record_id'], action='complete', expected_revision=2,
                                       fence=claimed['claim']['fence'], source=self.source)
        self.assertEqual(completed['data']['state'], 'completed')
        with self.assertRaises(MemoryClientError):
            self.service.save('task', task['data'], source=self.source, record_id=task['record_id'], expected_revision=1)
        self.assertIsNone(self.service.status()['pending_request'])

    def test_closing_intent_blocks_new_work_before_cloud_close(self):
        from tools.memory.lifecycle import SessionService
        self.service.initialize()
        checkpoint = self.service.checkpoint(summary='Original handover', refs=[], cutoff=0,
                                             checkpoint_id=str(uuid.uuid4()))['checkpoint']
        with patch.object(MemoryBoundary, 'write', side_effect=OSError('interrupted before submission')):
            with self.assertRaises(OSError):
                self.service.finish(summary='Original handover', checkpoint_ref=checkpoint)
        self.assertEqual(self.cloud.records[self.identifier]['data']['state'], 'active')
        restored = SessionService(self.root, self.identifier, env=self.env, client=self.cloud)
        with self.assertRaises(BoundaryError):
            restored.initialize(resume=True)
        with self.assertRaises(BoundaryError):
            restored.save('note', self.note, source=self.source)
        with self.assertRaises(BoundaryError):
            restored.finish(summary='Replacement handover', checkpoint_ref=checkpoint)
        self.assertEqual(restored.finish(summary='Original handover', checkpoint_ref=checkpoint)['status'], 'closed')

    def test_definitive_mutation_rejections_clear_only_rejected_journals(self):
        self.service.initialize()
        invoke = self.cloud.invoke
        for code in ('setting_conflict', 'reference_unavailable', 'session_unavailable'):
            for recovery in (False, True):
                with self.subTest(code=code, recovery=recovery):
                    key = str(uuid.uuid4())

                    def reject(route, body):
                        if route == '/v1/memory/write':
                            raise MemoryClientError(code)
                        return invoke(route, body)

                    if recovery:
                        def unknown(route, body):
                            if route == '/v1/memory/write':
                                raise MemoryClientError('outcome_unknown', request_key=body['request_key'])
                            return invoke(route, body)

                        with patch.object(self.cloud, 'invoke', side_effect=unknown), self.assertRaises(MemoryClientError):
                            self.service.save('note', self.note, source=self.source, request_key=key)
                        self.assertEqual(self.service.status()['pending_request']['request_key'], key)
                        with patch.object(self.cloud, 'invoke', side_effect=unknown), self.assertRaises(MemoryClientError):
                            self.service.recover()
                        self.assertEqual(self.service.status()['pending_request']['request_key'], key)
                    with patch.object(self.cloud, 'invoke', side_effect=reject), self.assertRaises(MemoryClientError) as error:
                        if recovery:
                            self.service.recover()
                        else:
                            self.service.save('note', self.note, source=self.source, request_key=key)
                    self.assertEqual(error.exception.code, code)
                    self.assertIsNone(self.service.status()['pending_request'])
                    failed = read_private(self.root, self.service.runtime + '/failed-memory-write.json')
                    self.assertEqual(failed['status'], code)
                    self.assertEqual(failed['request']['body']['request_key'], key)
                    self.assertNotIn(key, self.cloud.requests)
        self.assertEqual(self.service.save('note', self.note, source=self.source)['revision'], 1)

    def test_recovery_lookup_and_readback_rejections_keep_unknown_journal(self):
        self.service.initialize()
        self.cloud.lose_next = True
        key = str(uuid.uuid4())
        with self.assertRaises(MemoryClientError):
            self.service.save('note', self.note, source=self.source, request_key=key)
        invoke = self.cloud.invoke
        for rejected_route in ('/v1/operations/lookup', '/v1/memory/read'):
            with self.subTest(route=rejected_route):
                def reject(route, body):
                    if route == rejected_route:
                        raise MemoryClientError('not_authorized')
                    return invoke(route, body)

                with patch.object(self.cloud, 'invoke', side_effect=reject), self.assertRaises(MemoryClientError):
                    self.service.recover()
                self.assertEqual(self.service.status()['pending_request']['request_key'], key)
        self.assertEqual(self.service.recover()['recovery']['result']['request_key'], key)
        self.assertIsNone(self.service.status()['pending_request'])

    def test_owner_context_skips_synthetic_pages_without_reading_them(self):
        self.service.initialize()
        owner_ids, synthetic_ids = set(), set()
        for kind in ('task', 'risk'):
            for index in range(21):
                identifier = str(uuid.uuid5(uuid.UUID(self.identifier), f'{kind}:{index}'))
                record = {'record_id': identifier, 'revision': 1, 'kind': kind,
                          'classification': 'Sensitive', 'data_scope': 'synthetic', 'project_id': None,
                          'data': {'state': 'open'}, 'tags': []}
                self.cloud.records[identifier] = record
                synthetic_ids.add(identifier)
            identifier = 'ffffffff-ffff-4fff-8fff-' + ('fffffffffff1' if kind == 'task' else 'fffffffffff2')
            record = dict(record, record_id=identifier, data_scope='owner')
            self.cloud.records[identifier] = record
            self.cloud.history[(identifier, 1)] = record
            owner_ids.add(identifier)
        calls = len(self.cloud.calls)
        context = self.service.context()['context']
        self.assertEqual({record['record_id'] for record in context['tasks'] + context['blockers']}, owner_ids)
        routes = [route for route, _ in self.cloud.calls[calls:]]
        self.assertEqual(routes.count('/v1/memory/project_context'), 1)
        read_ids = {body['record_id'] for route, body in self.cloud.calls[calls:] if route == '/v1/memory/read'}
        self.assertEqual(read_ids, {self.identifier})  # Only the session-active check; no per-record reads.
        first = self.service.search(kind='task')
        self.assertEqual(first['records'], [])
        self.assertIsNotNone(first['next_after_id'])
        second = self.service.search(kind='task', after_id=first['next_after_id'])
        self.assertEqual({record['record_id'] for record in second['records']}, owner_ids & {
            record['record_id'] for record in context['tasks']})
        for scope in ('universal', 'context', 'all'):
            with self.subTest(scope=scope), self.assertRaises(BoundaryError):
                self.service.read_record(next(iter(synthetic_ids)), scope=scope)

    def test_interrupted_closure_resumes_only_original_checkpoint_after_restart(self):
        from tools.memory.lifecycle import SessionService
        from tools.memory.maintenance import Maintenance
        self.service.initialize()
        maintenance = Maintenance(self.service)
        checkpoint_id = str(uuid.uuid4())
        original = maintenance.checkpoint(summary='Original exact handover', checkpoint_id=checkpoint_id)
        other = maintenance.checkpoint(summary='Different handover', checkpoint_id=str(uuid.uuid4()))
        with patch.object(self.service.registry, 'finish_memory', side_effect=OSError('interrupted local release')):
            with self.assertRaises(OSError):
                maintenance.finish(checkpoint_id)
        self.assertEqual(self.cloud.records[self.identifier]['data']['state'], 'closed')
        self.assertEqual(self.service.status()['finish']['status'], 'closing')
        restored = SessionService(self.root, self.identifier, env=self.env, client=self.cloud)
        controller = Maintenance(restored)
        calls = len(self.cloud.calls)
        with self.assertRaises(BoundaryError):
            restored.initialize(resume=True)
        with self.assertRaises(BoundaryError):
            restored.save('note', self.note, source=self.source)
        with self.assertRaises(BoundaryError):
            controller.finish(other['checkpoint']['checkpoint_id'], recover_only=True)
        with self.assertRaises(BoundaryError):
            restored.finish(summary='Changed handover', checkpoint_ref=original['checkpoint']['receipt'])
        result = controller.finish(checkpoint_id, recover_only=True)
        self.assertEqual(result['status'], 'closed')
        self.assertEqual(result['checkpoint'], original['checkpoint']['receipt'])
        self.assertEqual(controller.finish(checkpoint_id, recover_only=True), result)
        self.assertEqual(restored.status()['finish']['status'], 'closed')
        self.assertFalse(any(route in ('/v1/memory/write', '/v1/memory/claim')
                             for route, _ in self.cloud.calls[calls:]))

    def test_unknown_closure_requires_exact_journal_recovery_before_finish(self):
        from tools.memory.lifecycle import SessionService
        self.service.initialize()
        checkpoint = self.service.checkpoint(summary='Preserved handover', refs=[], cutoff=0,
                                             checkpoint_id=str(uuid.uuid4()))['checkpoint']
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.service.finish(summary='Preserved handover', checkpoint_ref=checkpoint)
        key = self.service.status()['pending_request']['request_key']
        restored = SessionService(self.root, self.identifier, env=self.env, client=self.cloud)
        with self.assertRaises(BoundaryError):
            restored.finish(summary='Preserved handover', checkpoint_ref=checkpoint)
        recovered = restored.recover()['recovery']
        self.assertEqual(recovered['result']['request_key'], key)
        calls = len(self.cloud.calls)
        self.assertEqual(restored.finish(summary='Preserved handover', checkpoint_ref=checkpoint)['status'], 'closed')
        self.assertFalse(any(route == '/v1/memory/write' for route, _ in self.cloud.calls[calls:]))
        (self.root / restored.runtime / 'finish.json').unlink()
        with self.assertRaises(BoundaryError):
            restored.finish(summary='Preserved handover', checkpoint_ref=checkpoint)
        self.assertFalse((self.root / restored.runtime / 'finish.json').exists())

    def test_cancelled_approval_allows_memory_and_git_reconfiguration(self):
        from tools.project import ProjectError, configure_git
        path = self.service.runtime + '/approval.json'
        receipt = {'status': 'approval_cancelled', 'checkpoint_sha': 'a' * 40}
        save_private(self.root, path, receipt)
        self.assertEqual(self.service.initialize()['status'], 'ready')
        self.assertEqual(self.service.save('note', self.note, source=self.source)['revision'], 1)
        with patch('tools.project.validate_git'):
            result = configure_git(self.root, remote='origin', accepted_url='git@example.org:owner/project.git',
                                   branch='main', env=self.env)
        self.assertEqual(result['status'], 'configured')
        self.assertEqual(read_private(self.root, path), receipt)
        for status in ('approval_required', 'unsynchronized', 'pushing', 'unknown', None):
            with self.subTest(status=status):
                pending = dict(receipt, status=status)
                save_private(self.root, path, pending)
                with self.assertRaises(BoundaryError):
                    self.service.save('note', self.note, source=self.source)
                with self.assertRaises(ProjectError):
                    configure_git(self.root, remote='origin', accepted_url='git@example.org:owner/other.git',
                                  branch='main', env=self.env)
                self.assertEqual(read_private(self.root, path), pending)

    def test_cli_recovered_effect_reports_success_with_real_controller(self):
        from tools.memory.maintenance import Maintenance
        from tools.workflow import main
        self.service.initialize()
        maintenance = Maintenance(self.service)
        identifier = str(uuid.uuid4())
        maintenance.enqueue('Preserve finding', source=self.source, operation_id=identifier)
        token = maintenance.claim(identifier, worker='fixture')['assignment']['worker_token']
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            maintenance.apply(identifier, worker_token=token, changes=[{'kind': 'note', 'data': self.note}])
        pending = self.service.status()['pending_request']
        save_private(self.root, '.maven/recovery.json', {'operation_id': identifier, 'worker_stopped': True})
        with patch('tools.memory.lifecycle.SessionService', return_value=self.service), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['maintenance', '--operation', 'recover', '--root', str(self.root),
                         '--session', self.identifier, '--env', str(self.env), '--json',
                         '--input', str(self.root / '.maven/recovery.json')])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'claimed')
        self.assertTrue(result['cloud_saved'])
        self.assertEqual(result['assignment']['saved_refs'], [
            {'record_id': pending['record_id'], 'revision': pending['expected_revision'] + 1}])
        self.assertEqual(self.cloud.records[pending['record_id']]['revision'], 1)
        self.assertIsNone(self.service.status()['pending_request'])

    def test_pending_publication_blocks_cutover_without_overwriting_receipt(self):
        path = f'.maven/runtime/sessions/{self.identifier}/approval.json'
        receipt = {'status': 'unsynchronized', 'checkpoint_sha': 'a' * 40}
        save_private(self.root, path, receipt)
        with self.assertRaises(BoundaryError):
            self.service.initialize()
        from tools.memory.boundary import read_private
        self.assertEqual(read_private(self.root, path), receipt)
        self.assertFalse(self.cloud.calls)

    def test_cli_reports_real_argument_errors_and_status_never_connects(self):
        from tools.workflow import main, start_main
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = start_main(['--root', str(self.root), '--resume', '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'ArgumentError')
        self.service.initialize()
        with patch('tools.memory.environment.configured_client', side_effect=AssertionError('status connected')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['status', '--root', str(self.root), '--session', self.identifier,
                         '--env', str(self.env), '--json'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['live_checked'])

    def test_universal_task_claim_has_explicit_scope_and_historical_receipt(self):
        self.service.initialize()
        task = self.service.save('task', {'title': 'Shared check', 'description': '', 'state': 'open'},
                                 source=self.source, scope='universal')
        with self.assertRaises(BoundaryError):
            self.service.claim(task['record_id'], action='claim', expected_revision=1,
                               lease_seconds=60, source=self.source, scope='project')
        claimed = self.service.claim(task['record_id'], action='claim', expected_revision=1,
                                     lease_seconds=60, source=self.source, scope='universal')
        self.assertEqual(claimed['claim']['fence'], claimed['claim_receipt']['fence'])
        self.assertEqual(self.service.read_record(task['record_id'], revision=2)['claim'], None)

    def test_explicit_git_enrolls_existing_memory_session_and_binds_exact_candidate(self):
        from tools.git_workflow import summarize
        self.service.initialize()
        self.service.registry.use_git(self.root, self.identifier)
        memory = self.service._memory()
        with patch('tools.git_workflow.inspect_git', return_value={'head': 'a' * 40, 'changed_paths': []}), \
                patch('tools.git_workflow.candidate_paths', return_value=[]), \
                patch('tools.git_workflow.head', return_value='a' * 40):
            report = summarize(self.root, {'git_enabled': True}, memory, self.service.registry,
                               'Explicit optional Git summary', [])
        metadata = memory.metadata(memory.session())
        self.assertEqual(metadata['git_base'], 'a' * 40)
        self.assertEqual(metadata['reservation_id'], report['reservation']['reservation_id'])
        self.assertEqual(report['session']['record_id'], self.identifier)
        self.assertFalse(self.service.registry.inspect(self.root, self.identifier)['session']['memory_only'])

    def test_raw_put_keeps_original_request_identity_inside_checkpoint_sequence(self):
        from tools.memory import cli
        from tools.memory.maintenance import Maintenance
        self.service.initialize()
        key, identifier = str(uuid.uuid4()), str(uuid.uuid4())
        body = {'request_key': key, 'record_id': identifier, 'expected_revision': 0, 'kind': 'note',
                'classification': 'Sensitive', 'data_scope': 'owner', 'data': self.note,
                'source': self.source, 'change_reason': 'Exact original request reason'}
        save_private(self.root, '.maven/raw-request.json', body)
        with patch('tools.memory.cli._connection', return_value=({}, {})), \
                patch('tools.memory.cli.selected_session', return_value=None), \
                patch('tools.memory.cli.MemoryClient', return_value=self.cloud), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['--env', str(self.env), 'record', 'put', str(self.root / '.maven/raw-request.json'),
                             '--root', str(self.root), '--session', self.identifier])
        self.assertEqual(code, 0, output.getvalue())
        actual = self.cloud.requests[key][0]
        for name, value in body.items():
            self.assertEqual(actual[name], value)
        self.assertEqual(Maintenance(self.service).status()['pending_count'], 0)
        result = Maintenance(self.service).checkpoint(summary='Include raw write')
        self.assertIn(MemoryBoundary.ref(self.cloud.records[identifier]),
                      self.cloud.records[result['checkpoint']['receipt']['record_id']]['data']['memory_refs'])

    def test_workspace_receipts_are_sequence_tracked_with_source_writer_guard(self):
        from tools.memory.maintenance import Maintenance
        from tools.workspace import Workspace, WorkspaceMemory
        self.service.initialize()
        workspace = Workspace(WorkspaceMemory(self.service))
        with self.service.registry.writer(self.root, self.identifier):
            setting = workspace.store('fixture', {'truthful': True})
            operation = workspace.operation('fixture', 'completed', {'setting': MemoryBoundary.ref(setting)})
        result = Maintenance(self.service).checkpoint(summary='Include workspace receipts')
        refs = self.cloud.records[result['checkpoint']['receipt']['record_id']]['data']['memory_refs']
        self.assertIn(MemoryBoundary.ref(setting), refs)
        self.assertIn(MemoryBoundary.ref(operation), refs)

class WireCloud(MemoryCloud):
    """Answers the real signed client at its HTTP boundary, so every service request is counted."""
    BODY = 'Complete current governing instructions.'

    def __init__(self):
        super().__init__()
        self.wire = []

    def page(self):
        import hashlib
        from tools.memory.validation import digest
        candidate = {'slug': 'core', 'page_kind': 'directive', 'title': 'Core', 'scope': {},
                     'body_markdown': self.BODY, 'authority_state': 'active'}
        return {'page_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'maven:core')), 'slug': 'core',
                'page_kind': 'directive', 'title': 'Core', 'scope': {}, 'revision': 1,
                'content_hash': digest(candidate), 'data_scope': 'owner', 'body_markdown': self.BODY,
                'body_sha256': hashlib.sha256(self.BODY.encode()).hexdigest()}

    def post(self, route, body, remaining):
        from tools.memory.validation import CLIENT_PROTOCOL
        self.wire.append(route)
        if route == '/v1/memory/context':
            return 200, {'status': 'ready', 'authority_generation': self.generation, 'data': {
                'authority_generation': self.generation, 'minimum_client_protocol': CLIENT_PROTOCOL,
                'pages': [self.page()], 'bodies_inline': True, 'tasks': [], 'next_task_after_id': None}}
        try:
            return 200, self.invoke(route, body)
        except MemoryClientError as error:
            return 409, {'status': error.code}


class RequestBudgetTests(unittest.TestCase):
    """Once initialized, authority is cached: each memory call costs only its own request."""
    def setUp(self):
        from tools.memory.client import MemoryClient
        from tools.memory.lifecycle import SessionService
        from test_memory_client import CONFIG
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project'
        self.root.mkdir()
        self.identifier = str(uuid.uuid4())
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'project_id': str(uuid.uuid4()), 'root': str(self.root)})
        self.cloud = WireCloud()
        self.client = MemoryClient(CONFIG, None)
        post = patch.object(self.client, '_post', side_effect=self.cloud.post)
        post.start()
        self.addCleanup(post.stop)
        skills = patch('tools.memory.skills.SessionSkills.start', return_value={'status': 'ready', 'data': {'resources': []}})
        skills.start()
        self.addCleanup(skills.stop)
        self.service = SessionService(self.root, self.identifier, env=self.root.parent / 'home' / '.env',
                                      client=self.client)
        self.addCleanup(self.service.close)
        self.source = {'origin': 'Owner conversation', 'excerpt': 'Preserve this relevant finding.'}
        self.note = {'title': 'Finding', 'body_markdown': 'Verified local behavior.', 'category': 'work'}
        start = len(self.cloud.wire)
        self.service.initialize()
        # New session: session lookup, one inline context, session write, one project context.
        self.assertEqual(self.cloud.wire[start:], ['/v1/memory/read', '/v1/memory/context',
                                                   '/v1/memory/write', '/v1/memory/project_context'])

    def requests(self, operation):
        start = len(self.cloud.wire)
        return operation(), self.cloud.wire[start:]

    def test_initialized_search_read_and_save_each_issue_exactly_one_request(self):
        saved, wire = self.requests(lambda: self.service.save('note', self.note, source=self.source))
        self.assertEqual(wire, ['/v1/memory/write'])
        self.assertEqual({key: saved[key] for key in ('kind', 'data', 'tags', 'revision')},
                         {'kind': 'note', 'data': self.note, 'tags': [], 'revision': 1})
        record, wire = self.requests(lambda: self.service.read_record(saved['record_id']))
        self.assertEqual(wire, ['/v1/memory/read'])
        self.assertEqual(record['data'], self.note)
        found, wire = self.requests(lambda: self.service.search(query='finding'))
        self.assertEqual(wire, ['/v1/memory/search'])
        self.assertEqual([item['record_id'] for item in found['records']], [saved['record_id']])
        self.assertIsNone(found['next_offset'])
        updated, wire = self.requests(lambda: self.service.save(
            'note', dict(self.note, body_markdown='Later finding.'), source=self.source,
            record_id=saved['record_id'], expected_revision=1))
        self.assertEqual(wire, ['/v1/memory/write'])
        self.assertEqual(self.cloud.records[saved['record_id']]['data'], updated['data'])
        self.assertEqual(self.cloud.context_loads, 0)
        self.assertNotIn('authority_changed', found)

    def test_malformed_requests_fail_locally_with_field_detail_and_no_request(self):
        start = len(self.cloud.wire)
        for arguments, field in (({'offset': 20}, 'offset'), ({'query': 'x', 'after_id': str(uuid.uuid4())}, 'after_id'),
                                 ({'limit': 50}, 'limit'), ({'scope': 'all', 'project_id': str(uuid.uuid4())}, 'project_id')):
            with self.subTest(arguments=arguments):
                with self.assertRaises(MemoryClientError) as error:
                    self.service.search(**arguments)
                self.assertEqual(error.exception.code, 'invalid_request')
                self.assertIn(field, error.exception.detail)
        with self.assertRaises(MemoryClientError) as error:
            self.service.save('note', {'title': 'Missing body', 'category': 'work'}, source=self.source)
        self.assertIn('body_markdown', error.exception.detail)
        self.assertEqual(self.cloud.wire[start:], [])
        self.assertIsNone(self.service.status()['pending_request'])

    def test_explicit_save_is_one_write_plus_its_completion_receipt(self):
        from tools.memory.maintenance import Maintenance
        result, wire = self.requests(lambda: Maintenance(self.service).save('note', self.note, source=self.source))
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(wire, ['/v1/memory/write', '/v1/memory/write'])

    def test_inline_context_is_a_single_request(self):
        context, wire = self.requests(lambda: self.client.load_context({'workflow': 'maven-workflow'}))
        self.assertEqual(wire, ['/v1/memory/context'])
        self.assertEqual(context['data']['instructions'][0]['body_markdown'], WireCloud.BODY)

    def test_checkpoint_verifies_up_to_one_hundred_refs_with_one_revisions_request(self):
        refs = [MemoryBoundary.ref(self.service.save('note', dict(self.note, title=f'Note {index}'), source=self.source))
                for index in range(99)]
        checkpoint, wire = self.requests(lambda: self.service.checkpoint(
            summary='Bounded handover', refs=refs, cutoff=0, checkpoint_id=str(uuid.uuid4())))
        self.assertEqual(checkpoint['status'], 'checkpointed')
        self.assertEqual(wire, ['/v1/memory/revisions', '/v1/memory/write', '/v1/memory/write'])
        missing = [dict(refs[0], revision=2)]
        with self.assertRaises(BoundaryError):
            self.requests(lambda: self.service.checkpoint(summary='Unavailable version', refs=missing, cutoff=0,
                                                           checkpoint_id=str(uuid.uuid4())))
        self.assertEqual(self.cloud.wire[-1], '/v1/memory/revisions')

    def test_generation_change_is_reported_and_authority_reloads_before_the_next_write(self):
        self.cloud.generation = 2
        found, wire = self.requests(lambda: self.service.search(kind='note'))
        self.assertEqual(wire, ['/v1/memory/search'])
        self.assertTrue(found['authority_changed'])
        again, wire = self.requests(lambda: self.service.search(kind='note'))
        self.assertEqual((wire, again['authority_changed']), (['/v1/memory/search'], True))
        saved, wire = self.requests(lambda: self.service.save('note', self.note, source=self.source))
        # Session re-check and one inline authority reload precede the governed write.
        self.assertEqual(wire, ['/v1/memory/read', '/v1/memory/context', '/v1/memory/write'])
        self.assertNotIn('authority_changed', saved)
        found, wire = self.requests(lambda: self.service.search(kind='note'))
        self.assertEqual(wire, ['/v1/memory/search'])
        self.assertNotIn('authority_changed', found)

    def test_write_under_superseded_authority_commits_nothing_and_next_write_reloads(self):
        # Authority advanced elsewhere with no intervening response to this session.
        self.cloud.generation = 2
        before = dict(self.cloud.records)
        with self.assertRaises(MemoryClientError) as error:
            self.service.save('note', self.note, source=self.source)
        self.assertEqual(error.exception.code, 'context_stale')
        self.assertEqual(self.cloud.calls[-1][1]['authority_generation'], 1)
        self.assertEqual(self.cloud.records, before)
        self.assertIsNone(self.service.status()['pending_request'])
        saved, wire = self.requests(lambda: self.service.save('note', self.note, source=self.source))
        self.assertEqual(wire, ['/v1/memory/read', '/v1/memory/context', '/v1/memory/write'])
        self.assertEqual(self.cloud.calls[-1][1]['authority_generation'], 2)
        self.assertEqual(self.cloud.records[saved['record_id']]['data'], self.note)

    def test_rejected_lookup_after_lost_write_response_keeps_the_original_request_pending(self):
        original = self.cloud.post

        def post(route, body, remaining):
            if route == '/v1/memory/write':
                original(route, body, remaining)  # Commits, then the response is lost.
                raise MemoryClientError('transport_unavailable')
            if route == '/v1/operations/lookup':
                self.cloud.wire.append(route)
                return 409, {'status': 'context_stale'}
            return original(route, body, remaining)
        with patch.object(self.client, '_post', side_effect=post), \
                patch('tools.memory.client.time.sleep'):
            with self.assertRaises(MemoryClientError) as error:
                self.service.save('note', self.note, source=self.source)
        self.assertEqual(error.exception.code, 'outcome_unknown')
        pending = self.service.status()['pending_request']
        self.assertIsNotNone(pending)
        self.assertIn(self.cloud.calls[-1][1]['record_id'], self.cloud.records)

    def test_instructions_list_every_applicable_active_page_and_mark_loaded_versions(self):
        index = [dict(self.cloud.page(), body_markdown=None) | {'slug': slug, 'revision': revision, 'scope': scope}
                 for slug, revision, scope in (('core', 1, {}), ('maven-google', 2, {}),
                                               ('other-project', 1, {'project': str(uuid.uuid4())}))]
        for page in index:
            page.pop('body_markdown')
            page.pop('body_sha256')
        original = self.cloud.post

        def post(route, body, remaining):
            if route == '/v1/pages/index':
                self.cloud.wire.append(route)
                return 200, {'status': 'ready', 'authority_generation': 1, 'data': {
                    'pages': index, 'authority_generation': 1, 'next_after_slug': None}}
            return original(route, body, remaining)

        with patch.object(self.client, '_post', side_effect=post):
            listed, wire = self.requests(self.service.instructions)
        self.assertEqual(wire, ['/v1/pages/index'])
        self.assertEqual(listed['authority_generation'], 1)
        self.assertEqual([(page['slug'], page['loaded']) for page in listed['pages']],
                         [('core', True), ('maven-google', False)])


class SessionLockTests(unittest.TestCase):
    def test_reads_share_the_session_lock_and_writers_wait_then_report_busy(self):
        from tools.coordination import SessionBusy, session_lock
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid.uuid4())
            with session_lock(directory, identifier, shared=True), \
                    session_lock(directory, identifier, shared=True, timeout=0.2):
                with self.assertRaises(SessionBusy):
                    with session_lock(directory, identifier, timeout=0.2):
                        self.fail('exclusive lock acquired during shared reads')
            with session_lock(directory, identifier):
                with self.assertRaises(SessionBusy) as busy:
                    with session_lock(directory, identifier, shared=True, timeout=0.2):
                        self.fail('shared lock acquired during an exclusive write')
                self.assertIsInstance(busy.exception, BoundaryError)
            with session_lock(directory, identifier, timeout=0.2):
                pass

    def test_registered_sessions_is_a_read_only_registry_snapshot(self):
        from tools.memory.lifecycle import registered_sessions
        with tempfile.TemporaryDirectory() as directory:
            home, root = Path(directory) / 'home', Path(directory) / 'project'
            root.mkdir()
            self.assertEqual(registered_sessions(root, home=home), [])
            self.assertFalse((home / 'runtime' / 'sessions.json').exists())
            registry = SessionRegistry(home)
            first, second = str(uuid.uuid4()), str(uuid.uuid4())
            registry.register(root, str(uuid.uuid4()), first, {}, harness='omp', memory_only=True)
            registry.activate(root, first)
            registry.register(Path(directory), str(uuid.uuid4()), second, {}, memory_only=True)
            before = (home / 'runtime' / 'sessions.json').read_bytes()
            self.assertEqual(registered_sessions(root, home=home),
                             [{'session_id': first, 'harness': 'omp', 'state': 'active'}])
            self.assertEqual((home / 'runtime' / 'sessions.json').read_bytes(), before)

if __name__ == '__main__':
    unittest.main()
