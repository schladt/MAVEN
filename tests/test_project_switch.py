"""Offline reviewed rebinding, interruption and old-runtime isolation regressions."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.coordination import CoordinationError, SessionRegistry, project_binding_lock
from tools.home import load_home, save_home
from tools.memory.boundary import BoundaryError, digest, save_private
from tools.memory.client import MemoryClientError
from tools.memory.lifecycle import SessionService
from tools.project import load_project
from tools import project_switch as switching
from tools.setup_common import SetupPending, read_private, save_json
from tools.startup import _conversation_path, bind_conversation, conversation_binding, inspect_startup


class Crash(BaseException):
    """An abrupt process loss, not a recoverable application exception."""


class ProjectSwitchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'instance'
        self.home.mkdir(mode=0o700)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.old_id, self.new_id, self.instance = (str(uuid4()) for _ in range(3))
        self.operation_id = str(uuid4())
        self.source = {'origin': 'owner message', 'excerpt': 'Switch this exact folder and preserve old evidence.'}
        (self.home / '.env').write_text('MAVEN_MEMORY_INSTANCE_ID=' + self.instance + '\n')
        (self.home / '.env').chmod(0o600)
        self.binding = {'connection': {'instance_id': self.instance},
                        'environment': {'MAVEN_MEMORY_INSTANCE_ID': self.instance}}
        self.old = {'schema': 1, 'root': str(self.root), 'project_id': self.old_id,
                    'git_enabled': False, 'checks': [], 'remote': None, 'accepted_url': None, 'branch': None}
        save_private(self.root, '.maven/project.json', self.old)
        self.entities = {identifier: {'record_id': identifier, 'revision': 1, 'kind': 'entity',
                                     'project_id': None, 'data_scope': 'owner', 'tags': [],
                                     'data': {'entity_type': 'project', 'name': identifier}}
                         for identifier in (self.old_id, self.new_id)}
        self.writes = []
        self.client = Mock()
        self.client.invoke.side_effect = self.invoke
        for target, options in (
                ('tools.memory.environment.configured_client', {'return_value': self.client}),
                ('tools.administration.AdminOperations._project_binding', {'side_effect': lambda: self.binding}),
                ('tools.administration.AdminOperations._home_client', {'return_value': self.client}),
                ('tools.setup_common.permission_preflight', {'return_value': {
                    'status': 'Passed', 'checks': [], 'limitations': [], 'remediation': ''}})):
            patched = patch(target, **options)
            patched.start()
            self.addCleanup(patched.stop)
        self.admin = AdminOperations(self.home)

    def invoke(self, route, body):
        if route == '/v1/memory/read':
            if body['record_id'] not in self.entities:
                raise MemoryClientError('record_unavailable')
            return {'data': deepcopy(self.entities[body['record_id']])}
        if route == '/v1/memory/search':
            return {'data': {'records': [deepcopy(item) for item in self.entities.values()
                                         if 'maven-home' in item['tags']]}}
        if route == '/v1/memory/write':
            self.writes.append(deepcopy(body))
            record = self.entities[body['record_id']]
            self.assertEqual(body['expected_revision'], record['revision'])
            record.update(tags=body['tags'], revision=record['revision'] + 1)
            return {'data': {'record_id': record['record_id'], 'revision': record['revision']}}
        self.fail('Unexpected cloud route: ' + route)

    def prepare(self, **options):
        return switching.prepare_switch(self.home, self.root, self.new_id, self.operation_id,
                                        self.binding, **options)

    def apply(self, candidate):
        return switching.apply_switch(self.home, candidate, self.binding, self.source)

    def closed_session(self):
        identifier = str(uuid4())
        prefix = '.maven/runtime/sessions/' + identifier
        save_private(self.root, prefix + '/finish.json', {
            'status': 'closed', 'root': str(self.root), 'project_id': self.old_id, 'session_id': identifier})
        save_private(self.root, prefix + '/binding.json', {'project_id': self.old_id})
        return identifier, prefix

    def superseded_attempt(self):
        from tools.startup import replace_failed_start
        original = {'root': str(self.root), 'project_id': self.old_id,
                    'session_id': str(uuid4()), 'home': str(self.home),
                    'env': str(self.home / '.env'), 'harness': 'omp',
                    'harness_session': 'original-conversation'}
        replacement = {**original, 'session_id': str(uuid4())}
        persisted = {**original, 'initialized': False, 'resume_required': False}
        save_private(self.root, '.maven/runtime/sessions/' + original['session_id'] + '/mcp-binding.json', persisted)
        bind_conversation(self.home, original)
        from tools.startup import failed_start_evidence
        replace_failed_start(self.home, original, replacement, failed_start_evidence(self.home, original))
        replacement_prefix = '.maven/runtime/sessions/' + replacement['session_id']
        save_private(self.root, replacement_prefix + '/finish.json',
                     {**replacement, 'status': 'closed'})
        return '.maven/runtime/sessions/' + original['session_id'], replacement_prefix

    def failed_attempt(self, *, registered=True, native=True):
        original = {'root': str(self.root), 'project_id': self.old_id,
                    'session_id': str(uuid4()), 'home': str(self.home),
                    'env': str(self.home / '.env'), 'harness': 'omp',
                    'harness_session': 'failed-conversation' if native else None}
        prefix = '.maven/runtime/sessions/' + original['session_id']
        save_private(self.root, prefix + '/mcp-binding.json',
                     {**original, 'initialized': False, 'resume_required': False})
        if registered:
            SessionRegistry(self.home).register(
                self.root, self.old_id, original['session_id'], {}, memory_only=True,
                harness=original['harness'], harness_session=original['harness_session'])
            save_private(self.root, prefix + '/lifecycle.json', {'schema': 1, **original})
        bind_conversation(self.home, original)
        return original, prefix

    def test_unavailable_old_project_preserves_failed_attempt_and_frees_only_exact_native_pointer(self):
        original, prefix = self.failed_attempt()
        del self.entities[self.old_id]
        native_path = _conversation_path(original['harness'], original['harness_session'])
        native_record = read_private(self.home / native_path)
        unrelated = {**original, 'session_id': str(uuid4()), 'harness_session': 'unrelated'}
        bind_conversation(self.home, unrelated)
        service = SessionService(self.root, original['session_id'], env=self.home / '.env',
                                 client=self.client, harness='omp', harness_session=original['harness_session'])
        candidate = self.prepare()
        attempt = candidate['failed_attempts'][original['session_id']]
        self.assertEqual(attempt['binding'], original)
        self.assertEqual(attempt['evidence']['conversation_record'], native_record)
        self.assertEqual(attempt['cloud_session_check'], {
            'record_id': original['session_id'], 'include_archived': True, 'status': 'record_unavailable'})
        result = self.apply(candidate)
        archive = Path(result['preservation_path'])
        self.assertEqual(read_private(archive / prefix / 'mcp-binding.json'),
                         {**original, 'initialized': False, 'resume_required': False})
        self.assertFalse((archive / prefix / 'finish.json').exists())
        journal = read_private(self.home / 'runtime/project-switches' / (self.operation_id + '.json'))
        self.assertEqual(journal['intent']['candidate'], candidate)
        self.assertEqual(journal['intent']['source'], self.source)
        self.assertFalse((self.home / native_path).exists())
        self.assertEqual(conversation_binding(self.home, harness='omp', harness_session='unrelated'), unrelated)
        self.assertNotIn(original['session_id'], read_private(self.home / 'runtime/sessions.json')['sessions'])
        startup = inspect_startup(self.home, self.root, harness='omp', harness_session=original['harness_session'])
        self.assertEqual((startup['status'], startup['project_id']), ('initialize', self.new_id))
        fresh = {**original, 'project_id': self.new_id, 'session_id': str(uuid4())}
        bind_conversation(self.home, fresh)
        SessionRegistry(self.home).register(
            self.root, self.new_id, fresh['session_id'], {}, memory_only=True,
            harness='omp', harness_session=fresh['harness_session'])
        with self.assertRaisesRegex(BoundaryError, 'project binding changed'):
            service._binding()
        with self.assertRaisesRegex(BoundaryError, 'project binding changed'):
            with service._session_lock():
                self.fail('Old direct service crossed project fence')
        self.assertFalse(self.writes)
        self.assertTrue(all(call.args[1].get('include_archived') is True
                            for call in self.client.invoke.call_args_list
                            if call.args[1].get('record_id') == original['session_id']))

    def test_pre_registration_attempt_without_native_identity_can_be_preserved(self):
        original, prefix = self.failed_attempt(registered=False, native=False)
        del self.entities[self.old_id]
        candidate = self.prepare()
        self.assertIsNone(candidate['failed_attempts'][original['session_id']]['evidence']['registry_entry'])
        result = self.apply(candidate)
        self.assertTrue((Path(result['preservation_path']) / prefix / 'mcp-binding.json').exists())
        self.assertEqual(inspect_startup(self.home, self.root)['status'], 'initialize')

    def test_failed_attempt_unsafe_local_states_refuse_without_mutation(self):
        original, prefix = self.failed_attempt()
        binding_path = self.root / prefix / 'mcp-binding.json'
        persisted = read_private(binding_path)
        registry_path = self.home / 'runtime/sessions.json'
        registry = read_private(registry_path)
        mutations = (
            (binding_path, lambda value: value.update(initialized=True)),
            (binding_path, lambda value: value.update(resume_required=True)),
            (registry_path, lambda value: value['sessions'][original['session_id']].update(writers=['live'])),
            (registry_path, lambda value: value['sessions'][original['session_id']].update(state='abandoned')),
            (registry_path, lambda value: value['sessions'][original['session_id']].update(state='active')),
            (registry_path, lambda value: value['sessions'][original['session_id']].update(memory_only=False)),
            (registry_path, lambda value: value['reservations'].update({str(self.root): {'owner': 'unknown'}})),
        )
        for path, mutate in mutations:
            with self.subTest(path=path, mutate=mutate):
                changed = deepcopy(persisted if path == binding_path else registry)
                mutate(changed)
                save_json(path, changed)
                with self.assertRaises(SetupPending):
                    self.prepare()
                self.assertEqual(read_private(path), changed)
                self.assertEqual(load_project(self.root), self.old)
                save_json(binding_path, persisted)
                save_json(registry_path, registry)
        for name, content in (
                ('pending-memory.json', {'request_key': str(uuid4())}),
                ('session.json', {'session_id': original['session_id']}),
                ('maintenance.json', {'schema': 1, 'binding': {
                    key: original[key] for key in ('root', 'session_id', 'project_id')},
                    'assignments': {'work': {'state': 'queued'}}, 'checkpoints': {}})):
            path = self.root / prefix / name
            save_json(path, content)
            with self.subTest(name=name), self.assertRaises(SetupPending):
                self.prepare()
            self.assertEqual(read_private(path), content)
            path.unlink()
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())
        self.assertFalse((self.base / ('.maven-preserved-' + self.operation_id)).exists())

    def test_real_cloud_session_and_outage_never_prove_failed_attempt_absent(self):
        original, _ = self.failed_attempt()
        candidate = self.prepare()
        for result in ({'data': {'kind': 'session', 'record_id': original['session_id']}},
                       {'status': 'record_unavailable'}, MemoryClientError('transport_error')):
            def invoke(route, body):
                if body.get('record_id') == original['session_id']:
                    if isinstance(result, Exception):
                        raise result
                    return result
                return self.invoke(route, body)
            with self.subTest(result=result), patch.object(self.client, 'invoke', side_effect=invoke):
                with self.assertRaises((SetupPending, MemoryClientError)):
                    self.prepare()
                with self.assertRaises((SetupPending, MemoryClientError)):
                    self.apply(candidate)
            self.assertFalse(Path(candidate['preservation_path']).exists())
            self.assertFalse(switching.barrier_path(self.home, self.root).exists())
            self.assertEqual(load_project(self.root), self.old)

    def test_changed_failed_proof_or_native_record_invalidates_review(self):
        original, prefix = self.failed_attempt()
        candidate = self.prepare()
        paths = (self.root / prefix / 'mcp-binding.json',
                 self.root / prefix / 'lifecycle.json',
                 self.home / _conversation_path(original['harness'], original['harness_session']))
        for path in paths:
            before = read_private(path)
            save_json(path, {**before, 'changed': True})
            with self.subTest(path=path), self.assertRaisesRegex(SetupPending, 'changed'):
                self.apply(candidate)
            self.assertFalse(Path(candidate['preservation_path']).exists())
            save_json(path, before)
        with SessionRegistry(self.home).transaction() as registry:
            registry['sessions'][original['session_id']]['writers'] = ['live']
        with self.assertRaisesRegex(SetupPending, 'registration changed'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_failed_attempt_crashes_resume_original_journal_through_cleanup(self):
        original, prefix = self.failed_attempt()
        unrelated = {**original, 'session_id': str(uuid4()), 'harness_session': 'other'}
        bind_conversation(self.home, unrelated)
        candidate = self.prepare()
        rename, save, sync = switching._rename, switching.save_json, switching._sync
        native_parent = (self.home / _conversation_path('omp', original['harness_session'])).parent
        for boundary in ('prepared', 'barrier', 'old', 'new', 'cleanup', 'native', 'registry', 'completed'):
            def interrupt_save(path, value):
                save(path, value)
                if ((boundary == 'prepared' and value.get('phase') == 'prepared')
                        or (boundary == 'barrier' and path.name.endswith('.barrier.json'))
                        or (boundary == 'cleanup' and value.get('cleanup_started'))
                        or (boundary == 'registry' and path == self.home / 'runtime/sessions.json')
                        or (boundary == 'completed' and value.get('phase') == 'completed')):
                    raise Crash()
            def interrupt_rename(source, destination):
                rename(source, destination)
                if ((boundary == 'old' and destination == Path(candidate['preservation_path']) / '.maven')
                        or (boundary == 'new' and destination == self.root / '.maven')):
                    raise Crash()
            def interrupt_sync(path):
                sync(path)
                if boundary == 'native' and path == native_parent:
                    raise Crash()
            with self.subTest(boundary=boundary), patch.object(switching, 'save_json', side_effect=interrupt_save), \
                    patch.object(switching, '_rename', side_effect=interrupt_rename), \
                    patch.object(switching, '_sync', side_effect=interrupt_sync), self.assertRaises(Crash):
                self.apply(candidate)
            journal = read_private(self.home / 'runtime/project-switches' / (self.operation_id + '.json'))
            self.assertEqual(journal['intent']['candidate'], candidate)
            self.assertEqual(journal['intent']['source'], self.source)
            self.assertEqual(conversation_binding(self.home, harness='omp', harness_session='other'), unrelated)
        result = self.apply(candidate)
        archive = Path(result['preservation_path'])
        self.assertEqual(read_private(archive / prefix / 'mcp-binding.json'),
                         candidate['failed_attempts'][original['session_id']]['evidence']['persisted_binding'])
        self.assertEqual(inspect_startup(self.home, self.root, harness='omp',
                                        harness_session=original['harness_session'])['status'], 'initialize')
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())

    def test_interrupted_cleanup_refuses_replaced_native_pointer_and_changed_archive(self):
        original, prefix = self.failed_attempt()
        candidate = self.prepare()
        save = switching.save_json
        def interrupt(path, value):
            save(path, value)
            if value.get('cleanup_started'):
                raise Crash()
        with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
            self.apply(candidate)
        native_path = self.home / _conversation_path('omp', original['harness_session'])
        native = read_private(native_path)
        replacement = {**native, 'binding': {**original, 'session_id': str(uuid4())}}
        save_json(native_path, replacement)
        with self.assertRaisesRegex(SetupPending, 'native conversation changed'):
            self.apply(candidate)
        self.assertEqual(read_private(native_path), replacement)
        save_json(native_path, native)
        archived = Path(candidate['preservation_path']) / prefix / 'mcp-binding.json'
        persisted = read_private(archived)
        save_json(archived, {**persisted, 'initialized': True})
        with self.assertRaisesRegex(SetupPending, 'failed-start evidence changed'):
            self.apply(candidate)
        self.assertEqual(read_private(native_path), native)
        save_json(archived, persisted)
        self.assertEqual(self.apply(candidate)['status'], 'ready')

    def historical_session(self, *, failed=False):
        identifier = str(uuid4())
        prefix = '.maven/runtime/sessions/' + identifier
        pointer = {'record_id': identifier, 'revision': 1}
        save_private(self.root, prefix + '/session.json', pointer)
        save_private(self.root, prefix + '/last-memory-write.json', {
            'route': 'write', 'request_key': str(uuid4()), 'record': pointer, 'result': pointer})
        save_private(self.root, prefix + '/skills/registry.json',
                     {'project_id': self.old_id, 'session_id': identifier, 'pages': []})
        if failed:
            request = {'route': 'claim', 'session_id': identifier,
                       'body': {'record_id': str(uuid4()), 'action': 'claim', 'request_key': str(uuid4())}}
            request['digest'] = digest(request)
            save_private(self.root, prefix + '/failed-memory-write.json',
                         {'status': 'claim_conflict', 'request': request})
        else:
            save_private(self.root, prefix + '/approval.json', {'status': 'closed'})
        return identifier, prefix

    def test_reviewed_owner_stopped_history_preserves_cloud_and_local_evidence(self):
        first, first_prefix = self.historical_session()
        second, second_prefix = self.historical_session(failed=True)
        failed, _ = self.failed_attempt(native=False)
        identifiers = sorted([first, second])
        del self.entities[self.old_id]
        candidate = self.prepare(stopped_session_ids=identifiers)
        self.assertEqual(set(candidate['stopped_sessions']), set(identifiers))
        self.assertEqual(set(candidate['failed_attempts']), {failed['session_id']})
        self.assertTrue(all(item['status'] == 'stopped-by-owner'
                            for item in candidate['stopped_sessions'].values()))
        for confirmation in (None, {}, {**candidate['stopped_confirmation'],
                                        'sessions_and_workers_stopped': False}):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(SetupPending, 'owner confirmation'):
                switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=confirmation)
            self.assertFalse(Path(candidate['preservation_path']).exists())
        result = switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
        self.assertEqual(sorted(result['detached_stopped_session_ids']), identifiers)
        for identifier, prefix in ((first, first_prefix), (second, second_prefix)):
            directory = Path(result['preservation_path']) / prefix
            self.assertEqual(switching._files(directory), candidate['stopped_sessions'][identifier]['files'])
            self.assertFalse((directory / 'finish.json').exists())
        journal = read_private(self.home / 'runtime/project-switches' / (self.operation_id + '.json'))
        self.assertEqual(journal['intent']['stopped_confirmation'], candidate['stopped_confirmation'])
        self.assertEqual(journal['intent']['source'], self.source)
        self.assertFalse(self.writes)
        self.assertFalse(any(call.args[1].get('record_id') in identifiers
                             for call in self.client.invoke.call_args_list))

    def test_stopped_history_is_explicit_and_refuses_registered_or_uncertain_work(self):
        identifier, prefix = self.historical_session(failed=True)
        with self.assertRaisesRegex(SetupPending, 'finish receipt'):
            self.prepare()
        candidate = self.prepare(stopped_session_ids=[identifier])
        for status in ('starting', 'active', 'abandoned'):
            SessionRegistry(self.home).register(self.root, self.old_id, identifier, {}, memory_only=True)
            with SessionRegistry(self.home).transaction() as registry:
                registry['sessions'][identifier].update(state=status, writers=['worker'])
            with self.subTest(status=status), self.assertRaises(SetupPending):
                self.prepare(stopped_session_ids=[identifier])
            with self.assertRaises(SetupPending):
                switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
            with SessionRegistry(self.home).transaction() as registry:
                del registry['sessions'][identifier]
        for name, value in (
                ('pending-memory.json', {'request_key': str(uuid4())}),
                ('approval.json', {'status': 'approved'}),
                ('mcp-binding.json', {'initialized': True}),
                ('unknown.json', {}),
                ('maintenance.json', {'schema': 1, 'binding': {
                    'root': str(self.root), 'project_id': self.old_id, 'session_id': identifier},
                    'assignments': {}, 'checkpoints': {}, 'closure': {'state': 'pending'}})):
            path = self.root / prefix / name
            save_json(path, value)
            with self.subTest(name=name), self.assertRaises(SetupPending):
                self.prepare(stopped_session_ids=[identifier])
            with self.assertRaises(SetupPending):
                switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
            self.assertEqual(read_private(path), value)
            path.unlink()
        failed_path = self.root / prefix / 'failed-memory-write.json'
        rejected = read_private(failed_path)
        save_json(failed_path, {**rejected, 'status': 'transport_error'})
        with self.assertRaisesRegex(SetupPending, 'uncertain'):
            self.prepare(stopped_session_ids=[identifier])
        save_json(failed_path, rejected)
        pointer_path = self.root / prefix / 'session.json'
        pointer = read_private(pointer_path)
        save_json(pointer_path, {**pointer, 'record_id': str(uuid4())})
        with self.assertRaisesRegex(SetupPending, 'pointer differs'):
            self.prepare(stopped_session_ids=[identifier])
        self.assertEqual(load_project(self.root), self.old)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_stopped_history_crash_rechecks_exact_archive_and_original_source(self):
        identifier, prefix = self.historical_session(failed=True)
        candidate = self.prepare(stopped_session_ids=[identifier])
        def apply():
            return switching.apply_switch(self.home, candidate, self.binding, self.source,
                                          stopped_confirmation=candidate['stopped_confirmation'])
        rename = switching._rename
        for boundary in ('old', 'new'):
            def interrupt(source, destination):
                rename(source, destination)
                if ((boundary == 'old' and destination == Path(candidate['preservation_path']) / '.maven')
                        or (boundary == 'new' and destination == self.root / '.maven')):
                    raise Crash()
            with patch.object(switching, '_rename', side_effect=interrupt), self.assertRaises(Crash):
                apply()
            archived = Path(candidate['preservation_path']) / prefix / 'last-memory-write.json'
            original = read_private(archived)
            save_json(archived, {**original, 'changed': True})
            with self.assertRaisesRegex(SetupPending, 'stopped-session evidence changed'):
                apply()
            save_json(archived, original)
            with self.assertRaisesRegex(SetupPending, 'Switch intent changed'):
                switching.apply_switch(self.home, candidate, self.binding,
                                       {**self.source, 'excerpt': 'different source'},
                                       stopped_confirmation=candidate['stopped_confirmation'])
        self.assertEqual(apply()['status'], 'ready')
        journal = read_private(self.home / 'runtime/project-switches' / (self.operation_id + '.json'))
        self.assertEqual(journal['intent']['candidate'], candidate)
        self.assertEqual(journal['intent']['source'], self.source)

    def test_admin_stopped_history_requires_exact_separate_confirmation(self):
        identifier, _ = self.historical_session()
        state = self.admin.begin('project', {'root': str(self.root), 'stopped_session_ids': [identifier]})
        selection = {'mode': 'switch', 'root': str(self.root), 'project_id': self.new_id, 'name': ''}
        for mode in ('existing', 'rename', 'create_cloud'):
            with self.subTest(mode=mode), self.assertRaisesRegex(AdminInputError, 'only.*switch'):
                self.answer(state, {**selection, 'mode': mode})
        state = self.answer(state, selection)
        review = self.admin.advance(state['operation_id'])
        self.assertEqual(review['state'], 'awaiting_decision', review.get('error'))
        question = review['pending_interaction']
        confirmations = question['confirmations']
        self.assertEqual(confirmations, {'stopped_session_ids': [identifier],
                                        'sessions_and_workers_stopped': True,
                                        'preserve_without_cloud_closure': True})
        self.assertIn(identifier, question['description'])
        with self.assertRaisesRegex(AdminInputError, 'confirmations'):
            self.approve(review)
        state = self.answer(review, {'approved': True, 'review_digest': question['review_digest'],
                                     'source': self.source, 'confirmations': confirmations})
        result = self.admin.advance(state['operation_id'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(result['outcome']['detached_stopped_session_ids'], [identifier])

    def test_stopped_ids_parameter_rejects_noncanonical_or_duplicate_selection(self):
        identifier = str(uuid4())
        for value in (identifier, [identifier, identifier], ['not-a-session'], [None]):
            with self.subTest(value=value), self.assertRaises(AdminInputError):
                self.admin.begin('project', {'root': str(self.root), 'stopped_session_ids': value})

    def test_legacy_stopped_receipt_requires_exact_outer_canonical_digest(self):
        identifier = '00000000-0000-4000-8000-000000000001'
        prefix = '.maven/runtime/sessions/' + identifier
        save_private(self.root, prefix + '/session.json', {'record_id': identifier, 'revision': 1})
        # Deterministic legacy-shaped receipt with entirely invented identifiers.
        request = {'route': 'write', 'session_id': identifier, 'body': {
            'record_id': '00000000-0000-4000-8000-000000000002',
            'request_key': '00000000-0000-4000-8000-000000000003', 'expected_revision': 1}}
        canonical = '8d007b40e0615fd61048e3f83f99bffa443b431cb7576b19cb8fa81c3f1e01b8'
        self.assertEqual(digest(request), canonical)
        receipt = {'request': request, 'status': 'claim_conflict', 'pending_digest': canonical}
        path = self.root / prefix / 'failed-memory-write.json'
        save_json(path, receipt)
        candidate = self.prepare(stopped_session_ids=[identifier])
        for changed in (
                {'request': request, 'status': 'claim_conflict'},
                {**receipt, 'pending_digest': None},
                {**receipt, 'pending_digest': '0' * 64},
                {**receipt, 'request': {**request, 'digest': canonical}},
                {**receipt, 'request': {**request, 'body': {**request['body'], 'expected_revision': 2}}}):
            save_json(path, changed)
            with self.subTest(receipt=changed), self.assertRaisesRegex(SetupPending, 'uncertain'):
                self.prepare(stopped_session_ids=[identifier])
            with self.assertRaisesRegex(SetupPending, 'uncertain'):
                switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
            self.assertFalse(Path(candidate['preservation_path']).exists())
            self.assertEqual(read_private(path), changed)
        save_json(path, receipt)
        result = switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
        self.assertEqual(read_private(Path(result['preservation_path']) / prefix / path.name), receipt)

    def make_home(self):
        return save_home(self.home, root=self.root, project_id=self.old_id,
                         instance_id=self.instance, label='Personal', consent=self.source)

    def answer(self, state, response):
        question = state['pending_interaction']
        return self.admin.respond(state['operation_id'], question['interaction_id'], question['revision'], response)

    def review(self, kind='project', *, replace=True):
        state = self.admin.begin(kind, {'root': str(self.root)})
        response = {'mode': 'switch', 'root': str(self.root), 'project_id': self.new_id}
        response.update({'name': ''} if kind == 'project' else {'label': 'Switched Home', 'replace': replace})
        state = self.answer(state, response)
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'awaiting_decision', state.get('error'))
        return state

    def approve(self, state):
        return self.answer(state, {'approved': True,
                                  'review_digest': state['pending_interaction']['review_digest'],
                                  'source': self.source})

    def test_switch_preserves_cloud_git_files_and_isolates_entire_old_runtime(self):
        identifier, prefix = self.closed_session()
        self.old.update(git_enabled=True, remote='origin', accepted_url='git@example.org:owner/repo.git',
                        branch='main', checks=[['true']])
        save_private(self.root, '.maven/project.json', self.old)
        files = {'.git/config': '[remote "origin"]\nurl = unchanged\n', '.gitignore': 'unchanged\n',
                 'code.py': 'print("untouched")\n'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(content)
        before = deepcopy(self.entities)
        candidate = self.prepare()
        result = self.apply(candidate)
        archive = Path(result['preservation_path'])
        self.assertEqual(read_private(archive / prefix / 'binding.json')['project_id'], self.old_id)
        self.assertFalse((self.root / prefix).exists())
        self.assertEqual(list((self.root / '.maven').iterdir()), [self.root / '.maven/project.json'])
        self.assertEqual(load_project(self.root), {**self.old, 'project_id': self.new_id})
        self.assertEqual(self.entities, before)
        self.assertFalse(self.writes)
        for name, content in files.items():
            self.assertEqual((self.root / name).read_text(), content)
        self.assertEqual(self.apply(candidate), result)
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())

    def test_standalone_switch_updates_selected_home_with_exact_new_consent(self):
        self.make_home()
        candidate = self.prepare()
        self.assertTrue(candidate['update_home'])
        self.apply(candidate)
        current = load_home(self.home)
        self.assertEqual((current['root'], current['project_id'], current['label']),
                         (str(self.root), self.new_id, 'Personal'))
        self.assertEqual(current['consent'], self.source)
        self.assertFalse(self.writes)

    def test_open_starting_paused_abandoned_and_writer_registrations_all_block(self):
        registry = SessionRegistry(self.home)
        for status in ('starting', 'active', 'paused', 'abandoned', 'cleanup_pending'):
            with self.subTest(status=status):
                identifier = str(uuid4())
                registry.register(self.root, self.old_id, identifier, {}, memory_only=True)
                with registry.transaction() as state:
                    state['sessions'][identifier].update(state=status, writers=['uncertain-writer'])
                with self.assertRaisesRegex(SetupPending, 'open session registrations'):
                    self.prepare()
                with registry.transaction() as state:
                    state['sessions'].clear()
        self.assertEqual(load_project(self.root)['project_id'], self.old_id)

    def test_new_registration_after_approval_is_refused_without_rename(self):
        candidate = self.prepare()
        SessionRegistry(self.home).register(self.root, self.old_id, str(uuid4()), {}, memory_only=True)
        with self.assertRaisesRegex(SetupPending, 'open session registrations'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_unregistered_open_session_and_pending_cloud_or_maintenance_refuse_switch(self):
        identifier, prefix = self.closed_session()
        finish = self.root / prefix / 'finish.json'
        finish.unlink()
        with self.assertRaisesRegex(SetupPending, 'finish receipt'):
            self.prepare()
        save_json(finish, {'status': 'closed', 'root': str(self.root),
                           'project_id': self.old_id, 'session_id': identifier})
        pending = self.root / prefix / 'pending-memory.json'
        save_json(pending, {'request_key': str(uuid4())})
        with self.assertRaisesRegex(SetupPending, 'unresolved cloud request'):
            self.prepare()
        pending.unlink()
        save_private(self.root, prefix + '/maintenance.json', {'assignments': {'work': {'state': 'claimed'}}})
        with self.assertRaisesRegex(SetupPending, 'unfinished maintenance'):
            self.prepare()

    def test_finish_requires_exact_identity_and_legacy_marker_requires_stopped_review(self):
        identifier, prefix = self.historical_session()
        exact = {'status': 'closed', 'root': str(self.root),
                 'project_id': self.old_id, 'session_id': identifier}
        for key, value in (('root', str(self.base)), ('project_id', self.new_id), ('session_id', str(uuid4()))):
            save_private(self.root, prefix + '/finish.json', {**exact, key: value})
            with self.subTest(key=key), self.assertRaises(SetupPending):
                self.prepare()
            with self.assertRaises(SetupPending):
                self.prepare(stopped_session_ids=[identifier])
        save_private(self.root, prefix + '/finish.json', {'status': 'closed'})
        with self.assertRaisesRegex(SetupPending, 'finish receipt'):
            self.prepare()
        candidate = self.prepare(stopped_session_ids=[identifier])
        result = switching.apply_switch(self.home, candidate, self.binding, self.source,
                                       stopped_confirmation=candidate['stopped_confirmation'])
        self.assertEqual(read_private(Path(result['preservation_path']) / prefix / 'finish.json'),
                         {'status': 'closed'})
        self.assertEqual(result['detached_stopped_session_ids'], [identifier])

    def test_cancelled_checkpoint_is_preserved_terminal_legacy_evidence(self):
        identifier, prefix = self.closed_session()
        state = {'schema': 1, 'binding': {'root': str(self.root), 'project_id': self.old_id,
                                         'session_id': identifier},
                 'assignments': {}, 'checkpoints': {str(uuid4()): {'state': 'cancelled'}}}
        save_private(self.root, prefix + '/maintenance.json', state)
        result = self.apply(self.prepare())
        self.assertEqual(read_private(Path(result['preservation_path']) / prefix / 'maintenance.json'), state)

    def test_nested_worktree_requires_existing_exclusion_before_preservation(self):
        subprocess.run(['git', 'init', '-q', str(self.base)], check=True, capture_output=True)
        archive = self.base / ('.maven-preserved-' + self.operation_id)
        with self.assertRaisesRegex(SetupPending, 'ancestor Git worktree'):
            self.prepare()
        self.assertFalse(archive.exists())
        self.assertFalse((self.base / '.gitignore').exists())
        exclusion = self.base / '.gitignore'
        exclusion.write_text('/' + archive.name + '/\n')
        candidate = self.prepare()
        exclusion.write_text('')
        with self.assertRaisesRegex(SetupPending, 'ancestor Git worktree'):
            self.apply(candidate)
        self.assertFalse(archive.exists())
        exclusion.write_text('/' + archive.name + '/\n')
        result = self.apply(candidate)
        self.assertEqual(result['preservation_path'], str(archive))
        self.assertEqual(exclusion.read_text(), '/' + archive.name + '/\n')
        visible = subprocess.run(['git', '-C', str(self.base), 'ls-files', '--others', '--exclude-standard'],
                                 check=True, capture_output=True, text=True).stdout
        self.assertNotIn(archive.name, visible)

    def test_switch_preserves_superseded_start_after_replacement_closes(self):
        original, replacement = self.superseded_attempt()
        receipt = read_private(self.root / original / 'failed-start-recovery.json')
        result = self.apply(self.prepare())
        archive = Path(result['preservation_path'])
        self.assertEqual(load_project(self.root)['project_id'], self.new_id)
        self.assertEqual(read_private(archive / original / 'failed-start-recovery.json'), receipt)
        self.assertEqual(read_private(archive / replacement / 'finish.json')['status'], 'closed')
        self.assertFalse((archive / original / 'finish.json').exists())
        self.assertFalse(self.writes)

    def test_superseded_start_needs_complete_fence_and_closed_replacement(self):
        original, replacement = self.superseded_attempt()
        mutations = (
            (original + '/mcp-binding.json', lambda item: item.pop('superseded_by')),
            (original + '/failed-start-recovery.json',
             lambda item: item['cloud_session_check'].update(status='unknown')),
            (original + '/failed-start-recovery.json',
             lambda item: item['persisted_binding'].update(initialized=True)),
            (replacement + '/mcp-binding.json', lambda item: item.update(project_id=self.new_id)),
            (replacement + '/finish.json', lambda item: item.update(status='pending')),
        )
        for relative, mutate in mutations:
            with self.subTest(relative=relative):
                path = self.root / relative
                before = read_private(path)
                changed = deepcopy(before)
                mutate(changed)
                save_private(self.root, relative, changed)
                with self.assertRaises(SetupPending):
                    self.prepare()
                self.assertEqual(load_project(self.root)['project_id'], self.old_id)
                save_private(self.root, relative, before)
        save_private(self.root, original + '/pending-memory.json', {'request_key': str(uuid4())})
        with self.assertRaises(SetupPending):
            self.prepare()
        self.assertFalse(self.writes)

    def test_changed_project_home_and_cloud_revision_invalidate_review(self):
        candidate = self.prepare()
        save_private(self.root, '.maven/project.json', {**self.old, 'checks': [['changed']]})
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        save_private(self.root, '.maven/project.json', self.old)
        self.make_home()
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        (self.home / 'home.json').unlink()
        self.entities[self.new_id]['revision'] += 1
        with self.assertRaisesRegex(SetupPending, 'Destination cloud project changed'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_destination_must_be_universal_owner_project(self):
        for changes in ({'data_scope': 'project'}, {'project_id': self.old_id}, {'kind': 'note'},
                        {'data': {'entity_type': 'person'}}):
            with self.subTest(changes=changes):
                original = deepcopy(self.entities[self.new_id])
                self.entities[self.new_id].update(changes)
                with self.assertRaisesRegex(SetupPending, 'universal owner project'):
                    self.prepare()
                self.entities[self.new_id] = original

    def test_symlink_runtime_and_preservation_collision_are_refused(self):
        (self.root / '.maven/runtime').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(SetupPending):
            self.prepare()
        (self.root / '.maven/runtime').unlink()
        archive = self.root.parent / ('.maven-preserved-' + self.operation_id)
        archive.mkdir(mode=0o700)
        with self.assertRaisesRegex(SetupPending, 'Preservation path already exists'):
            self.prepare()

    def test_admin_exact_review_and_stale_digest(self):
        review = self.review()
        candidate = review['pending_interaction']['review']['switch']
        self.assertEqual(candidate['old_project']['project_id'], self.old_id)
        self.assertEqual(candidate['new_project']['project_id'], self.new_id)
        self.assertIn(candidate['preservation_path'], review['pending_interaction']['description'])
        with self.assertRaises(AdminInputError):
            self.answer(review, {'approved': True, 'review_digest': 'stale', 'source': self.source})
        state = self.approve(review)
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'completed', state.get('error'))
        self.assertEqual(load_project(self.root)['project_id'], self.new_id)

    def test_changed_instance_binding_invalidates_admin_approval(self):
        state = self.approve(self.review())
        self.binding = {**self.binding, 'connection': {'instance_id': str(uuid4())}}
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'blocked')
        self.assertIn('approval is invalid', state['error'])
        self.assertEqual(load_project(self.root)['project_id'], self.old_id)

    def test_home_switch_requires_replace_and_changes_only_target_tag(self):
        self.make_home()
        self.entities[self.old_id]['tags'] = ['maven-home']
        old = deepcopy(self.entities[self.old_id])
        state = self.admin.begin('home', {'root': str(self.root)})
        state = self.answer(state, {'mode': 'switch', 'root': str(self.root), 'project_id': self.new_id,
                                    'label': 'Home', 'replace': False})
        blocked = self.admin.advance(state['operation_id'])
        self.assertEqual(blocked['state'], 'blocked')
        state = self.approve(self.review('home'))
        state = self.admin.advance(state['operation_id'])
        self.assertEqual(state['state'], 'completed', state.get('error'))
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)
        self.assertEqual(self.entities[self.old_id], old)
        self.assertEqual([item['record_id'] for item in self.writes], [self.new_id])

    def test_crash_after_each_rename_resumes_exact_archive_and_blocks_new_sessions(self):
        self.closed_session()
        self.make_home()
        candidate = self.prepare()
        original = switching._rename
        for boundary in ('old', 'new'):
            def interrupt(source, destination):
                original(source, destination)
                if (boundary == 'old' and destination.name == '.maven' and destination.parent.name.startswith('.maven-preserved-')) or (
                        boundary == 'new' and destination == self.root / '.maven'):
                    raise Crash()
            with patch.object(switching, '_rename', side_effect=interrupt), self.assertRaises(Crash):
                self.apply(candidate)
            with self.assertRaises(CoordinationError):
                SessionRegistry(self.home).register(self.root, self.new_id, str(uuid4()), {}, memory_only=True)
            changed = deepcopy(candidate)
            changed['new_project']['project_id'] = str(uuid4())
            with self.assertRaisesRegex(SetupPending, 'Switch intent changed'):
                self.apply(changed)
        result = self.apply(candidate)
        self.assertTrue((Path(result['preservation_path']) / '.maven/runtime/sessions').exists())
        self.assertFalse((self.root / '.maven/runtime').exists())
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)

    def test_crash_after_barrier_and_staged_write_and_completion_recovers(self):
        candidate = self.prepare()
        original = switching.save_json
        boundaries = ('barrier', 'stage', 'identity', 'completed')
        for boundary in boundaries:
            def interrupt(path, value):
                original(path, value)
                hit = ((boundary == 'barrier' and path.name.endswith('.barrier.json'))
                       or (boundary == 'stage' and path.name == 'project.json')
                       or (boundary == 'identity' and 'new_directory' in value)
                       or (boundary == 'completed' and value.get('phase') == 'completed'))
                if hit:
                    raise Crash()
            with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
                self.apply(candidate)
            self.assertTrue(switching.barrier_path(self.home, self.root).exists())
        result = self.apply(candidate)
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(switching.barrier_path(self.home, self.root).exists())

    def test_crash_after_home_write_and_before_admin_receipt_is_resumable(self):
        self.make_home()
        state = self.approve(self.review('home'))
        original = switching.save_json
        def interrupt(path, value):
            original(path, value)
            if path == self.home / 'home.json':
                raise Crash()
        with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
            self.admin.advance(state['operation_id'])
        resumed = self.admin.advance(state['operation_id'])
        self.assertEqual(resumed['state'], 'completed', resumed.get('error'))
        self.assertEqual(load_home(self.home)['project_id'], self.new_id)

    def test_home_tag_crash_reuses_completed_local_switch_without_duplicate_tagging(self):
        self.make_home()
        state = self.approve(self.review('home'))
        original = self.admin._tag_home
        def interrupt(*args):
            original(*args)
            raise Crash()
        with patch.object(self.admin, '_tag_home', side_effect=interrupt), self.assertRaises(Crash):
            self.admin.advance(state['operation_id'])
        resumed = self.admin.advance(state['operation_id'])
        self.assertEqual(resumed['state'], 'completed', resumed.get('error'))
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]['record_id'], self.new_id)

    def test_directory_replacement_invalidates_review_even_with_identical_project_id(self):
        candidate = self.prepare()
        (self.root / '.maven').rename(self.root / 'original-runtime')
        save_private(self.root, '.maven/project.json', self.old)
        with self.assertRaisesRegex(SetupPending, 'approval is invalid'):
            self.apply(candidate)
        self.assertFalse(Path(candidate['preservation_path']).exists())

    def test_tampered_stage_after_old_runtime_move_is_not_activated(self):
        candidate = self.prepare()
        original = switching._rename
        def interrupt(source, destination):
            original(source, destination)
            raise Crash()
        with patch.object(switching, '_rename', side_effect=interrupt), self.assertRaises(Crash):
            self.apply(candidate)
        stage = Path(candidate['preservation_path']) / 'new/project.json'
        save_json(stage, {**candidate['new_project'], 'project_id': str(uuid4())})
        with self.assertRaisesRegex(SetupPending, 'Staged replacement changed'):
            self.apply(candidate)
        self.assertFalse((self.root / '.maven').exists())
        self.assertTrue(switching.barrier_path(self.home, self.root).exists())

    def test_crash_before_barrier_does_not_trap_a_later_open_registration(self):
        candidate = self.prepare()
        original = switching.save_json
        def interrupt(path, value):
            original(path, value)
            if value.get('phase') == 'prepared':
                raise Crash()
        with patch.object(switching, 'save_json', side_effect=interrupt), self.assertRaises(Crash):
            self.apply(candidate)
        barrier = switching.barrier_path(self.home, self.root)
        self.assertFalse(barrier.exists())
        registry, identifier = SessionRegistry(self.home), str(uuid4())
        registry.register(self.root, self.old_id, identifier, {}, memory_only=True)
        with self.assertRaisesRegex(SetupPending, 'New registration appeared'):
            self.apply(candidate)
        self.assertFalse(barrier.exists())
        registry.finish_memory(self.root, identifier)
        self.assertEqual(self.apply(candidate)['status'], 'ready')

    def test_new_start_cannot_cross_exclusive_binding_gate(self):
        # Separate flock descriptors in the same process still contend, without threads or cloud calls.
        with project_binding_lock(self.home, self.root, shared=False):
            with patch('tools.coordination.SESSION_LOCK_TIMEOUT', 0), self.assertRaises(CoordinationError):
                with project_binding_lock(self.home, self.root):
                    self.fail('Shared gate entered while cutover owned it')


if __name__ == '__main__':
    unittest.main()
