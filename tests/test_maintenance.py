"""Local state/recovery regressions with real controllers and simulated cloud storage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tools.memory.boundary import read_private, save_private
from tools.memory.client import MemoryClientError
from tools.memory.lifecycle import SessionService
from tools.memory.maintenance import Maintenance, MaintenanceError
from test_workflow import MemoryCloud


def identifier():
    return str(uuid.uuid4())


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'ordinary-project'
        self.root.mkdir()
        self.project, self.session = identifier(), identifier()
        save_private(self.root, '.maven/project.json', {
            'schema': 1, 'root': str(self.root), 'project_id': self.project})
        self.cloud = MemoryCloud()
        self.service = SessionService(self.root, self.session, client=self.cloud,
                                      env=self.root.parent / 'instance' / '.env')
        skills = patch('tools.memory.skills.SessionSkills.start', return_value={'status': 'ready', 'data': {'resources': []}})
        skills.start()
        self.addCleanup(skills.stop)
        self.service.initialize()
        self.memory = Maintenance(self.service)
        self.source = {'origin': 'Owner conversation', 'excerpt': 'Keep the revised plan in memory.'}
        self.note = {'title': 'Plan', 'body_markdown': 'Original plan.', 'category': 'plan'}

    def assign(self, intent='Reconcile current plan', **kwargs):
        queued = self.memory.enqueue(intent, source=self.source, **kwargs)
        operation = queued['assignment']['operation_id']
        token = self.memory.claim(operation, worker='fixture-reconciler')['assignment']['worker_token']
        return operation, token

    def finish_assignment(self, operation, token, changes):
        self.memory.apply(operation, worker_token=token, changes=changes)
        return self.memory.complete(operation, worker_token=token, summary='Reconciled the authorized plan.')

    def test_pause_preserves_accepted_work_and_resume_is_atomic_and_idempotent(self):
        queued = self.memory.enqueue('Accepted before pause', source=self.source)
        self.memory.pause(anchor='conversation:turn-10')
        rejected = self.memory.enqueue('Not captured during pause', source=self.source)
        self.assertFalse(rejected['accepted'])
        self.assertEqual(self.memory.claim(queued['assignment']['operation_id'], worker='worker')['status'], 'paused')
        explicit = self.memory.save('note', self.note, source=self.source, scope='universal')
        self.assertTrue(explicit['cloud_saved'])
        self.assertEqual(self.memory.resume()['status'], 'context_required')
        self.assertFalse(self.memory.status()['automatic_updates'])
        resumed = self.memory.resume(discussion='Available discussion changes the deadline.',
                                      source=self.source, context_complete=False)
        count = self.memory.status()['pending_count']
        self.assertFalse(resumed['context_complete'])
        repeated = self.memory.resume(discussion='Available discussion changes the deadline.', source=self.source)
        self.assertEqual(repeated['catch_up']['operation_id'], resumed['assignment']['operation_id'])
        self.assertEqual(self.memory.status()['pending_count'], count)
        self.memory.pause(anchor='conversation:turn-20')
        self.memory.resume(fresh=True)
        self.assertEqual(self.memory.status()['pending_count'], count)
        self.assertIsNone(self.memory.status()['catch_up'])
        self.assertIn('universal', explicit['assignment']['scopes'])

    def test_same_cutoff_checkpoint_head_preserves_explicit_refs_and_legacy_ancestry(self):
        first_note = self.service.save('note', self.note, source=self.source)
        second_note = self.service.save('note', dict(self.note, title='Extra reference'), source=self.source)
        first_ref = {'record_id': first_note['record_id'], 'revision': 1}
        second_ref = {'record_id': second_note['record_id'], 'revision': 1}
        with patch('tools.memory.maintenance.now', return_value='2026-01-01T00:00:00+00:00'):
            first = self.memory.checkpoint(summary='First boundary', refs=[first_ref])['checkpoint']
            second = self.memory.checkpoint(summary='Additional explicit version', refs=[second_ref])['checkpoint']
            self.assertEqual(first['cutoff'], second['cutoff'])
            # A schema-one ledger without the new head still retains its real base chain.
            state = read_private(self.root, self.memory.path)
            state.pop('latest_checkpoint')
            save_private(self.root, self.memory.path, state)
            third = Maintenance(self.service).checkpoint(summary='Final boundary')['checkpoint']
        self.assertEqual(third['base'], second['receipt'])
        self.assertIn(first['receipt'], second['refs'])
        self.assertIn(second_ref, second['refs'])
        with self.assertRaises(MaintenanceError):
            self.memory.finish(first['checkpoint_id'])
        self.assertEqual(self.memory.finish(third['checkpoint_id'])['status'], 'closed')
        seen, pending = set(), [third['receipt']]
        while pending:
            ref = pending.pop()
            key = (ref['record_id'], ref['revision'])
            if key in seen:
                continue
            seen.add(key)
            record = self.cloud.history[key]
            if record['kind'] == 'checkpoint':
                pending.extend(record['data']['memory_refs'])
        self.assertIn((first_ref['record_id'], 1), seen)
        self.assertIn((second_ref['record_id'], 1), seen)

    def test_overlapping_checkpoint_completion_keeps_both_ancestry_branches(self):
        operation, token = self.assign()
        first = self.memory.checkpoint(summary='First pending boundary')['checkpoint']
        note = self.service.save('note', self.note, source=self.source)
        extra = {'record_id': note['record_id'], 'revision': 1}
        second = self.memory.checkpoint(summary='Second pending boundary', refs=[extra])['checkpoint']
        self.memory.complete(operation, worker_token=token, summary='No changes required', no_changes=True)
        saved_second = self.memory.advance_checkpoint(second['checkpoint_id'])['checkpoint']
        saved_first = self.memory.advance_checkpoint(first['checkpoint_id'])['checkpoint']
        final = self.memory.checkpoint(summary='Join completed boundaries')['checkpoint']
        self.assertEqual(final['base'], saved_first['receipt'])
        self.assertIn(saved_second['receipt'], final['refs'])
        self.assertIn(extra, saved_second['refs'])

    def test_drain_preserves_queued_partial_and_applied_workers_through_finish(self):
        queued = self.memory.enqueue('Queued work', source=self.source, workstream='queued')['assignment']
        partial, partial_token = self.assign('Partial work', workstream='partial')
        applied, applied_token = self.assign('Applied work', workstream='applied')
        self.memory.apply(applied, worker_token=applied_token, changes=[{'kind': 'note', 'data': self.note}])
        self.memory.apply_budget = 0
        result = self.memory.apply(partial, worker_token=partial_token, changes=[
            {'kind': 'note', 'data': self.note}, {'kind': 'note', 'data': dict(self.note, title='Second effect')}])
        self.assertTrue(result['continue'])
        self.memory.pause(anchor='conversation:end')
        self.assertEqual(self.memory.complete(applied, worker_token=applied_token, summary='Saved')['status'], 'paused')
        drain = self.memory.pause(drain=True)
        self.assertEqual(drain['drain_cutoff'], 3)
        restored = Maintenance(self.service)
        self.assertEqual(restored.pause(drain=True)['drain_cutoff'], 3)
        for automatic in (True, False):
            with self.subTest(automatic=automatic), self.assertRaises(MaintenanceError):
                restored.enqueue('Too late', source=self.source, automatic=automatic)
        checkpoint = restored.checkpoint(summary='Drain accepted work')['checkpoint']
        self.assertEqual(checkpoint['cutoff'], 3)
        queued_token = restored.claim(queued['operation_id'], worker='draining-worker')['assignment']['worker_token']
        self.assertEqual(restored.apply(partial, worker_token=partial_token)['status'], 'applied')
        restored.complete(partial, worker_token=partial_token, summary='Both effects verified')
        restored.complete(applied, worker_token=applied_token, summary='Saved')
        restored.complete(queued['operation_id'], worker_token=queued_token, summary='Reviewed, no change', no_changes=True)
        saved = restored.advance_checkpoint(checkpoint['checkpoint_id'])
        self.assertEqual(saved['status'], 'checkpointed')
        self.assertEqual(len([record for record in self.cloud.records.values()
                              if record['kind'] == 'operation' and record['data']['name'] == 'memory-maintenance']), 3)
        self.assertEqual(restored.finish(checkpoint['checkpoint_id'])['status'], 'closed')

    def test_resume_cancels_drain_only_with_explicit_resume_evidence(self):
        self.memory.pause(drain=True)
        self.assertEqual(self.memory.resume()['status'], 'context_required')
        self.assertEqual(self.memory.status()['drain_cutoff'], 0)
        resumed = self.memory.resume(discussion='Actual discussion', source=self.source)
        self.assertEqual(resumed['status'], 'queued')
        self.assertIsNone(self.memory.status()['drain_cutoff'])
        self.assertTrue(self.memory.enqueue('New admission', source=self.source)['accepted'])

    def test_catch_up_state_tracks_normal_and_recovered_completion_after_restart(self):
        for recover in (False, True):
            with self.subTest(recover=recover):
                self.memory.pause()
                queued = self.memory.resume(discussion='Actual paused discussion', source=self.source)
                operation = queued['assignment']['operation_id']
                token = self.memory.claim(operation, worker='catch-up-worker')['assignment']['worker_token']
                if recover:
                    self.cloud.lose_next = True
                    with self.assertRaises(MemoryClientError):
                        self.memory.complete(operation, worker_token=token, summary='Reviewed discussion', no_changes=True)
                    self.memory.recover(operation, worker_stopped=True)
                else:
                    self.memory.complete(operation, worker_token=token, summary='Reviewed discussion', no_changes=True)
                restored = Maintenance(self.service)
                self.assertEqual(restored.status()['catch_up']['state'], 'completed')
                self.assertEqual(restored.resume()['catch_up']['state'], 'completed')
                self.assertEqual(restored.status()['pending_count'], 0)

    def test_claim_recovery_keeps_historical_receipt_without_expired_ownership(self):
        task = self.service.save('task', {'title': 'Task', 'description': '', 'state': 'open'}, source=self.source)
        operation, token = self.assign()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.apply(operation, worker_token=token, changes=[{
                'kind': 'task', 'task_action': 'claim', 'record_id': task['record_id'],
                'expected_revision': 1, 'scope': 'project', 'lease_seconds': 60}])
        self.cloud.records[task['record_id']]['claim'] = None
        recovered = self.memory.recover(operation, worker_stopped=True)
        effect = recovered['assignment']['task_effects'][0]
        self.assertIsNone(effect['claim'])
        self.assertEqual(effect['claim_receipt']['session_id'], self.session)

    def test_explicit_claim_replay_rechecks_lease_instead_of_returning_cached_ownership(self):
        task = self.service.save('task', {'title': 'Task', 'description': '', 'state': 'open'}, source=self.source)
        operation = identifier()
        arguments = dict(action='claim', expected_revision=1, lease_seconds=60,
                         source=self.source, operation_id=operation)
        first = self.memory.claim_task(task['record_id'], **arguments)
        self.assertEqual(first['claim']['session_id'], self.session)
        self.cloud.records[task['record_id']]['claim'] = None
        replay = self.memory.claim_task(task['record_id'], **arguments)
        self.assertIsNone(replay['claim'])
        self.assertIsNone(replay['assignment']['task_effects'][0]['claim'])
        self.assertEqual(replay['claim_receipt'], first['claim_receipt'])
        self.assertEqual(self.memory.status()['pending_count'], 0)

    def test_checkpoint_pins_cutoff_and_later_work_cannot_change_its_manifest(self):
        operation, token = self.assign()
        boundary = self.memory.checkpoint(summary='Before the later change')
        self.assertEqual(boundary['status'], 'pending')
        checkpoint = boundary['checkpoint']['checkpoint_id']
        later = self.memory.enqueue('Use the new plan', source=self.source, supersede=True)['assignment']['operation_id']
        self.assertNotEqual(self.memory.status(operation)['state'], 'superseded')
        self.finish_assignment(operation, token, [{'kind': 'note', 'data': self.note}])
        saved = self.memory.advance_checkpoint(checkpoint)
        self.assertEqual(saved['status'], 'checkpointed')
        reference = self.memory.status(operation, detail=True)['assignment']['effects'][0]['record']
        self.assertIn(reference, saved['checkpoint']['refs'])
        self.assertNotIn(later, saved['checkpoint']['assignments'])
        later_token = self.memory.claim(later, worker='later-worker')['assignment']['worker_token']
        self.finish_assignment(later, later_token, [{'kind': 'note', 'data': dict(self.note, body_markdown='New plan.'),
                                                    'record_id': reference['record_id'], 'expected_revision': 1}])
        self.assertEqual(self.cloud.history[(reference['record_id'], 1)]['data']['body_markdown'], 'Original plan.')
        with self.assertRaises(MaintenanceError):
            self.memory.finish(checkpoint)
        final = self.memory.checkpoint(summary='All accepted work saved', wait=True, wait_seconds=0)
        self.assertIn(saved['checkpoint']['receipt'], final['checkpoint']['refs'])
        self.assertEqual(self.memory.finish(final['checkpoint']['checkpoint_id'])['status'], 'closed')
        self.assertEqual(self.cloud.records[self.session]['data']['state'], 'closed')

    def test_superseded_unknown_write_is_reconciled_before_successor_runs(self):
        operation, token = self.assign()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.apply(operation, worker_token=token, changes=[{'kind': 'note', 'data': self.note}])
        original = self.memory.status(operation, detail=True)['assignment']['effects'][0]['request']
        successor = self.memory.enqueue('The new plan replaces that intent', source=self.source,
                                        supersede=True)['assignment']['operation_id']
        with self.assertRaises(MaintenanceError):
            self.memory.claim(successor, worker='successor')
        restored = Maintenance(self.service)
        result = restored.recover(operation, worker_stopped=True)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(self.cloud.records[original['record_id']]['revision'], 1)
        successor_token = restored.claim(successor, worker='successor')['assignment']['worker_token']
        restored.apply(successor, worker_token=successor_token, changes=[{
            'kind': 'note', 'data': dict(self.note, body_markdown='Reconciled newer intent.'),
            'record_id': original['record_id'], 'expected_revision': 1}])
        restored.complete(successor, worker_token=successor_token, summary='Newer intent reconciled.')
        with self.assertRaises(MaintenanceError):
            restored.apply(operation, worker_token=token)
        self.assertEqual(self.cloud.records[original['record_id']]['revision'], 2)
        self.assertIn(original['request_key'], self.cloud.requests)

    def test_partial_revision_conflict_preserves_saved_effect_and_requires_reconciliation(self):
        existing = self.service.save('note', self.note, source=self.source)
        operation, token = self.assign()
        checkpoint = self.memory.checkpoint(summary='Pin the reconciliation before replacing its worker')
        with self.assertRaises(MemoryClientError):
            self.memory.apply(operation, worker_token=token, changes=[
                {'kind': 'note', 'data': dict(self.note, title='Universal lesson'), 'scope': 'universal'},
                {'kind': 'note', 'data': self.note, 'record_id': existing['record_id'], 'expected_revision': 0}])
        item = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual([effect['state'] for effect in item['effects']], ['saved', 'failed'])
        with self.assertRaises(MaintenanceError):
            self.memory.complete(operation, worker_token=token, summary='Not actually complete')
        self.assertEqual(self.memory.claim(operation, worker='fixture-reconciler')['assignment']['state'], 'blocked')
        self.assertEqual(self.memory.release(operation, worker_token=token, worker_stopped=True)['status'], 'blocked')
        restored = Maintenance(self.service)
        replacement = restored.claim(operation, worker='replacement')['assignment']
        self.assertEqual(replacement['state'], 'blocked')
        with self.assertRaises(MaintenanceError):
            restored.revise(operation, worker_token=token, changes=[{'kind': 'note', 'data': self.note}])
        token = replacement['worker_token']
        self.memory.revise(operation, worker_token=token, changes=[{
            'kind': 'note', 'data': dict(self.note, body_markdown='Merged current state.'),
            'record_id': existing['record_id'], 'expected_revision': 1}])
        self.memory.apply(operation, worker_token=token)
        self.memory.complete(operation, worker_token=token, summary='Both verified effects saved.')
        summary = self.memory.summary()['memory_summary']
        self.assertEqual(summary['universal'][0]['revision'], 1)
        self.assertEqual(summary['project'][0]['revision'], 2)
        self.assertEqual(summary['pending'], [])
        saved = restored.advance_checkpoint(checkpoint['checkpoint']['checkpoint_id'])
        self.assertEqual(saved['status'], 'checkpointed')
        self.assertIn({'record_id': existing['record_id'], 'revision': 2}, saved['checkpoint']['refs'])
        self.assertIn(item['effects'][0]['record'], saved['checkpoint']['refs'])

    def test_worker_task_claim_phases_retain_real_fences_and_checkpoint_accounting(self):
        task = self.service.save('task', {'title': 'Reconcile', 'description': '', 'state': 'open'},
                                 source=self.source, scope='universal')
        operation, token = self.assign('Reflect completed task work')
        first = self.memory.apply(operation, worker_token=token, changes=[{
            'kind': 'task', 'task_action': 'claim', 'record_id': task['record_id'],
            'expected_revision': 1, 'scope': 'universal', 'lease_seconds': 60}])
        lease = first['task_effects'][0]['claim']
        self.assertEqual(lease['session_id'], self.session)
        self.assertEqual(self.memory.release(operation, worker_token=token, worker_stopped=True)['status'], 'applied')
        replacement = Maintenance(self.service).claim(operation, worker='replacement')['assignment']
        self.assertEqual(replacement['state'], 'applied')
        token = replacement['worker_token']
        self.memory.revise(operation, worker_token=token, changes=[{
            'kind': 'task', 'task_action': 'complete', 'record_id': task['record_id'],
            'expected_revision': 2, 'scope': 'universal', 'fence': lease['fence']}])
        self.memory.apply(operation, worker_token=token)
        self.memory.complete(operation, worker_token=token, summary='Actual task completion recorded.')
        saved = self.memory.checkpoint(summary='Task state boundary')
        self.assertIn({'record_id': task['record_id'], 'revision': 3}, saved['checkpoint']['refs'])
        self.assertEqual(self.cloud.records[task['record_id']]['data']['state'], 'completed')
        self.assertIsNone(self.cloud.records[task['record_id']]['claim'])

    def test_definitive_rejections_allow_revised_effects_after_worker_replacement(self):
        invoke = self.cloud.invoke
        for code in ('setting_conflict', 'reference_unavailable', 'session_unavailable'):
            for during_recovery in (False, True):
                with self.subTest(code=code, during_recovery=during_recovery):
                    operation, token = self.assign(workstream=identifier())
                    record_id = identifier()
                    change = {'kind': 'note', 'data': self.note, 'record_id': record_id}

                    def reject_write(route, body):
                        if route == '/v1/memory/write' and body['record_id'] == record_id:
                            raise MemoryClientError(rejection, request_key=body['request_key'])
                        return invoke(route, body)

                    rejection = 'outcome_unknown' if during_recovery else code
                    with patch.object(self.cloud, 'invoke', side_effect=reject_write):
                        with self.assertRaises(MemoryClientError):
                            self.memory.apply(operation, worker_token=token, changes=[change])
                        if during_recovery:
                            self.assertEqual(self.memory.status(operation)['state'], 'uncertain')
                            rejection = code
                            with self.assertRaises(MemoryClientError):
                                Maintenance(self.service).recover(operation, worker_stopped=True)
                    item = self.memory.status(operation, detail=True)['assignment']
                    self.assertEqual(item['state'], 'blocked')
                    self.assertEqual(item['effects'][0]['state'], 'failed')
                    self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())
                    self.assertNotIn(record_id, self.cloud.records)
                    with self.assertRaises(MaintenanceError):
                        self.memory.apply(operation, worker_token=token)
                    self.memory.release(operation, worker_token=token, worker_stopped=True)
                    token = self.memory.claim(operation, worker='replacement')['assignment']['worker_token']
                    self.memory.revise(operation, worker_token=token, changes=[
                        dict(change, data=dict(self.note, body_markdown='Reconciled the rejection.'))])
                    self.memory.apply(operation, worker_token=token)
                    self.memory.complete(operation, worker_token=token, summary='Revised effect verified.')
                    self.assertEqual(self.cloud.records[record_id]['revision'], 1)

    def test_definitive_completion_rejection_allows_reconciled_completion(self):
        invoke = self.cloud.invoke
        for during_recovery in (False, True):
            with self.subTest(during_recovery=during_recovery):
                operation, token = self.assign(workstream=identifier())
                applied = self.memory.apply(operation, worker_token=token, changes=[{'kind': 'note', 'data': self.note}])
                saved_ref = applied['saved_refs'][0]

                def reject_completion(route, body):
                    if route == '/v1/memory/write' and body['record_id'] == operation:
                        raise MemoryClientError(rejection, request_key=body['request_key'])
                    return invoke(route, body)

                rejection = 'outcome_unknown' if during_recovery else 'session_unavailable'
                with patch.object(self.cloud, 'invoke', side_effect=reject_completion):
                    with self.assertRaises(MemoryClientError):
                        self.memory.complete(operation, worker_token=token, summary='Original completion')
                    if during_recovery:
                        rejection = 'session_unavailable'
                        with self.assertRaises(MemoryClientError):
                            Maintenance(self.service).recover(operation, worker_stopped=True)
                self.assertEqual(self.memory.status(operation)['state'], 'blocked')
                self.memory.release(operation, worker_token=token, worker_stopped=True)
                token = self.memory.claim(operation, worker='replacement')['assignment']['worker_token']
                result = self.memory.complete(operation, worker_token=token, summary='Completion after reconciliation')
                self.assertEqual(result['status'], 'completed')
                self.assertIn(saved_ref, result['assignment']['saved_refs'])
                self.assertEqual(self.cloud.records[saved_ref['record_id']]['revision'], 1)
                self.assertEqual(self.cloud.records[operation]['data']['summary'], 'Completion after reconciliation')

    def test_recovery_lookup_and_readback_rejections_keep_unknown_effect_fenced(self):
        invoke = self.cloud.invoke
        for failed_route in ('/v1/operations/lookup', '/v1/memory/read'):
            with self.subTest(failed_route=failed_route):
                operation, token = self.assign(workstream=identifier())
                record_id = identifier()
                change = {'kind': 'note', 'data': self.note, 'record_id': record_id}
                self.cloud.lose_next = True
                with self.assertRaises(MemoryClientError):
                    self.memory.apply(operation, worker_token=token, changes=[change])

                def reject_recovery(route, body):
                    if route == failed_route and (route.endswith('/lookup') or body['record_id'] == record_id):
                        raise MemoryClientError('not_authorized')
                    return invoke(route, body)

                with patch.object(self.cloud, 'invoke', side_effect=reject_recovery):
                    with self.assertRaises(MemoryClientError):
                        self.memory.recover(operation, worker_stopped=True)
                item = self.memory.status(operation, detail=True)['assignment']
                self.assertEqual(item['state'], 'uncertain')
                self.assertEqual(item['effects'][0]['state'], 'uncertain')
                self.assertTrue((self.root / self.memory.runtime / 'pending-memory.json').exists())
                for action in (
                        lambda: self.memory.claim(operation, worker='replacement'),
                        lambda: self.memory.release(operation, worker_token=token, worker_stopped=True),
                        lambda: self.memory.revise(operation, worker_token=token, changes=[change]),
                        lambda: self.memory.apply(operation, worker_token=token),
                        lambda: self.memory.complete(operation, worker_token=token, summary='Unverified')):
                    with self.assertRaises(MaintenanceError):
                        action()
                self.memory.recover(operation, worker_stopped=True)
                self.memory.apply(operation, worker_token=token)
                self.memory.complete(operation, worker_token=token, summary='Recovered original effect')
                self.assertEqual(self.cloud.records[record_id]['revision'], 1)

    def test_unknown_completion_retains_its_request_and_worker_fence(self):
        operation, token = self.assign()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.complete(operation, worker_token=token, summary='Original completion', no_changes=True)
        original = self.memory.status(operation, detail=True)['assignment']['completion_request']
        invoke = self.cloud.invoke

        def reject_lookup(route, body):
            if route == '/v1/operations/lookup':
                raise MemoryClientError('not_authorized')
            return invoke(route, body)

        with patch.object(self.cloud, 'invoke', side_effect=reject_lookup):
            with self.assertRaises(MemoryClientError):
                self.memory.recover(operation, worker_stopped=True)
        item = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual(item['state'], 'uncertain')
        self.assertEqual(item['completion_request'], original)
        with self.assertRaises(MaintenanceError):
            self.memory.release(operation, worker_token=token, worker_stopped=True)
        with self.assertRaises(MaintenanceError):
            self.memory.complete(operation, worker_token=token, summary='Replacement completion', no_changes=True)
        self.assertEqual(self.memory.recover(operation, worker_stopped=True)['status'], 'completed')
        self.assertEqual(self.cloud.records[operation]['revision'], 1)
        self.assertEqual(self.cloud.records[operation]['data']['summary'], 'Original completion')

    def test_explicit_original_request_survives_unknown_outcome_and_changed_intent_is_rejected(self):
        operation, key = identifier(), identifier()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.save('note', self.note, source=self.source, operation_id=operation, request_key=key)
        self.assertIn(key, self.cloud.requests)
        self.memory.recover(operation, worker_stopped=True)
        self.assertEqual(self.memory.status(operation)['state'], 'completed')
        self.assertEqual(self.memory.status()['pending_count'], 0)
        saved = self.memory.save('note', self.note, source=self.source, operation_id=operation, request_key=key)
        self.assertEqual(saved['status'], 'saved')
        note_ref = self.memory.status(operation, detail=True)['assignment']['effects'][0]['record']
        self.assertEqual(self.cloud.records[note_ref['record_id']]['revision'], 1)
        with self.assertRaises(MaintenanceError):
            self.memory.save('note', dict(self.note, title='Different intent'), source=self.source,
                              operation_id=operation, request_key=key)

    def test_explicit_single_action_recovery_verifies_completion_and_unblocks_new_records(self):
        for kind, data in (
                ('task', {'title': 'Actual task', 'description': 'Task evidence. ' * 100, 'state': 'open'}),
                ('decision', {'title': 'Actual decision', 'body_markdown': 'Decision evidence. ' * 195,
                              'state': 'accepted'})):
            with self.subTest(kind=kind):
                operation, key = identifier(), identifier()
                before = len(self.cloud.requests)
                self.cloud.lose_next = True
                with self.assertRaises(MemoryClientError) as error:
                    self.memory.save(kind, data, source=self.source, expected_revision=0,
                                     operation_id=operation, request_key=key)
                self.assertEqual(error.exception.code, 'outcome_unknown')
                item = self.memory.status(operation, detail=True)['assignment']
                original = deepcopy(item['effects'][0]['request'])
                self.assertEqual(item['completion_intent'], {
                    'worker': 'explicit-save', 'summary': item['intent'], 'changes_digest': item['changes_digest']})
                self.assertEqual(item['effects'][0]['state'], 'uncertain')
                self.assertNotIn(operation, self.cloud.records)
                with self.assertRaises(MaintenanceError):
                    self.memory.recover(operation, worker_stopped=False)
                recovered = Maintenance(self.service).recover(operation, worker_stopped=True)
                self.assertEqual((recovered['status'], recovered['cloud_saved']), ('completed', True))
                self.assertEqual(recovered['assignment']['completion'], {'record_id': operation, 'revision': 1})
                self.assertEqual(self.cloud.records[original['record_id']]['data'], data)
                self.assertEqual(self.cloud.records[original['record_id']]['revision'], 1)
                self.assertEqual(len(self.cloud.requests), before + 2)
                self.assertEqual(self.memory.status(operation, detail=True)['assignment']['effects'][0]['request'], original)
                self.assertEqual(self.memory.status()['pending_count'], 0)
                self.assertEqual(self.memory.recover(operation, worker_stopped=True), recovered)
                self.assertEqual(len(self.cloud.requests), before + 2)
                self.assertEqual(self.memory.save('note', self.note, source=self.source)['status'], 'saved')

    def test_explicit_recovery_finishes_crashes_after_effect_receipt_without_reapplying(self):
        for crash_state in ('claimed', 'applied'):
            with self.subTest(crash_state=crash_state):
                operation = identifier()
                with patch.object(self.memory, 'complete', side_effect=SystemExit('worker stopped')):
                    with self.assertRaises(SystemExit):
                        self.memory.save('note', self.note, source=self.source, operation_id=operation)
                with self.memory._transaction() as state:
                    state['assignments'][operation]['state'] = crash_state
                original = self.memory.status(operation, detail=True)['assignment']['effects'][0]
                calls = len(self.cloud.calls)
                result = Maintenance(self.service).recover(operation, worker_stopped=True)
                writes = [body for route, body in self.cloud.calls[calls:] if route == '/v1/memory/write']
                self.assertEqual([body['record_id'] for body in writes], [operation])
                self.assertEqual(result['status'], 'completed')
                self.assertIn(original['record'], result['assignment']['saved_refs'])
                self.assertEqual(self.cloud.records[original['record']['record_id']]['revision'], 1)

    def test_legacy_explicit_save_recovery_requires_original_worker_and_input_digests(self):
        for caller_ids in (False, True):
            for saved_effect in (False, True):
                with self.subTest(caller_ids=caller_ids, saved_effect=saved_effect):
                    operation = identifier()
                    arguments = {'record_id': identifier(), 'request_key': identifier()} if caller_ids else {}
                    if saved_effect:
                        with patch.object(self.memory, 'complete', side_effect=SystemExit('worker stopped')):
                            with self.assertRaises(SystemExit):
                                self.memory.save('note', self.note, source=self.source, operation_id=operation, **arguments)
                    else:
                        self.cloud.lose_next = True
                        with self.assertRaises(MemoryClientError):
                            self.memory.save('note', self.note, source=self.source, operation_id=operation, **arguments)
                    with self.memory._transaction() as state:
                        item = state['assignments'][operation]
                        item.pop('completion_intent')
                        if saved_effect:
                            item['state'] = 'claimed'  # Old recover stopped after verifying the effect.
                    result = Maintenance(self.service).recover(operation, worker_stopped=True)
                    self.assertEqual(result['status'], 'completed')
                    item = self.memory.status(operation, detail=True)['assignment']
                    self.assertEqual(item['completion_intent']['summary'], item['intent'])
                    self.assertEqual(self.cloud.records[item['effects'][0]['record']['record_id']]['revision'], 1)
                    self.assertEqual(self.memory.status()['pending_count'], 0)

    def test_general_or_modified_explicit_plans_are_not_automatically_completed(self):
        for invalid_field, value in (('worker', 'semantic-worker'), ('context', 'Not an explicit input digest'),
                                     ('changes_digest', 'different-plan'), ('plan_revision', 2)):
            with self.subTest(invalid_field=invalid_field):
                operation = identifier()
                self.cloud.lose_next = True
                with self.assertRaises(MemoryClientError):
                    self.memory.save('note', self.note, source=self.source, record_id=identifier(), operation_id=operation)
                with self.memory._transaction() as state:
                    item = state['assignments'][operation]
                    item.pop('completion_intent')
                    item[invalid_field] = value
                result = self.memory.recover(operation, worker_stopped=True)
                self.assertEqual(result['status'], 'claimed')
                self.assertNotIn(operation, self.cloud.records)
                self.assertNotIn('completion', result['assignment'])
                with self.assertRaises(MaintenanceError):
                    self.memory.recover(operation, worker_stopped=True)
        operation, token = self.assign(workstream='semantic-multiple', automatic=False)
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.apply(operation, worker_token=token, changes=[
                {'kind': 'note', 'data': self.note}, {'kind': 'note', 'data': dict(self.note, title='Second effect')}])
        result = self.memory.recover(operation, worker_stopped=True)
        self.assertEqual(result['status'], 'claimed')
        self.assertEqual(result['assignment']['pending_effects'], 1)
        self.assertNotIn(operation, self.cloud.records)

    def test_explicit_recovery_keeps_uncertain_completion_fenced_until_its_receipt_is_verified(self):
        operation = identifier()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.save('note', self.note, source=self.source, operation_id=operation)
        original = self.memory.status(operation, detail=True)['assignment']['effects'][0]['request']
        self.cloud.lose_next = True  # The effect replay is known; lose only the new completion response.
        with self.assertRaises(MemoryClientError) as error:
            self.memory.recover(operation, worker_stopped=True)
        self.assertEqual(error.exception.code, 'outcome_unknown')
        item = self.memory.status(operation, detail=True)['assignment']
        completion = deepcopy(item['completion_request'])
        self.assertEqual((item['state'], item['effects'][0]['state']), ('uncertain', 'saved'))
        self.assertNotIn('completion', item)
        self.assertEqual(self.memory.status()['pending_count'], 1)
        successor = self.memory.enqueue('Next record', source=self.source, automatic=False,
                                        workstream='record:new')['assignment']['operation_id']
        with self.assertRaises(MaintenanceError):
            self.memory.claim(successor, worker='next-worker')
        recovered = Maintenance(self.service).recover(operation, worker_stopped=True)
        self.assertEqual(recovered['status'], 'completed')
        item = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual(item['completion_request'], completion)
        self.assertEqual(item['effects'][0]['request'], original)
        self.assertEqual(self.cloud.records[operation]['revision'], 1)
        self.assertEqual(self.cloud.records[original['record_id']]['revision'], 1)
        self.assertEqual(self.memory.claim(successor, worker='next-worker')['status'], 'ready')

    def test_explicit_recovery_cancels_only_definitively_rejected_original_effect(self):
        operation = identifier()
        invoke = self.cloud.invoke

        def reject(route, body):
            if route == '/v1/memory/write' and body['kind'] == 'note':
                raise MemoryClientError(rejection, request_key=body['request_key'])
            return invoke(route, body)

        rejection = 'outcome_unknown'
        with patch.object(self.cloud, 'invoke', side_effect=reject):
            with self.assertRaises(MemoryClientError):
                self.memory.save('note', self.note, source=self.source, operation_id=operation)
            original = self.memory.status(operation, detail=True)['assignment']['effects'][0]['request']
            rejection = 'reference_unavailable'
            with self.assertRaises(MemoryClientError):
                self.memory.recover(operation, worker_stopped=True)
        item = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual((item['state'], item['effects'][0]['state']), ('cancelled', 'failed'))
        self.assertNotIn(original['record_id'], self.cloud.records)
        self.assertNotIn(operation, self.cloud.records)
        self.assertEqual(self.memory.status()['pending_count'], 0)
        self.assertEqual(self.memory.save('note', self.note, source=self.source)['status'], 'saved')

    def test_explicit_completion_definitive_rejection_retries_original_completion_only(self):
        operation = identifier()
        invoke = self.cloud.invoke

        def reject_completion(route, body):
            if route == '/v1/memory/write' and body['record_id'] == operation:
                raise MemoryClientError('context_stale', request_key=body['request_key'])
            return invoke(route, body)

        with patch.object(self.cloud, 'invoke', side_effect=reject_completion):
            with self.assertRaises(MemoryClientError):
                self.memory.save('note', self.note, source=self.source, operation_id=operation)
        item = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual((item['state'], item['effects'][0]['state']), ('blocked', 'saved'))
        self.assertNotIn('completion_request', item)
        self.assertNotIn(operation, self.cloud.records)
        result = self.memory.recover(operation, worker_stopped=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.cloud.records[item['effects'][0]['record']['record_id']]['revision'], 1)
        self.assertEqual(self.cloud.records[operation]['data']['summary'], item['intent'])

    def test_explicit_completion_intent_precedes_submission_and_survives_process_loss(self):
        operation = identifier()

        def stop_before_submit(request):
            item = self.memory.status(operation, detail=True)['assignment']
            self.assertEqual(item['completion_intent']['changes_digest'], item['changes_digest'])
            self.assertEqual(item['effects'][0]['request'], request)
            self.assertNotIn(request['record_id'], self.cloud.records)
            raise SystemExit('worker stopped before network submission')

        with patch.object(self.memory, '_execute', side_effect=stop_before_submit):
            with self.assertRaises(SystemExit):
                self.memory.save('note', self.note, source=self.source, operation_id=operation)
        item = self.memory.status(operation, detail=True)['assignment']
        original = deepcopy(item['effects'][0]['request'])
        self.assertEqual(item['state'], 'applying')
        self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())
        recovered = Maintenance(self.service).recover(operation, worker_stopped=True)
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(self.cloud.records[original['record_id']]['revision'], 1)
        self.assertIn(original['request_key'], self.cloud.requests)

    def test_explicit_task_recovery_finishes_only_after_verified_claim_and_completion(self):
        task = self.service.save('task', {'title': 'Owned task', 'description': '', 'state': 'open'},
                                 source=self.source)
        operation = identifier()
        self.cloud.lose_next = True
        with self.assertRaises(MemoryClientError):
            self.memory.claim_task(task['record_id'], action='claim', expected_revision=1,
                                   source=self.source, lease_seconds=60, operation_id=operation)
        original = self.memory.status(operation, detail=True)['assignment']['effects'][0]['request']
        recovered = self.memory.recover(operation, worker_stopped=True)
        self.assertEqual(recovered['status'], 'completed')
        receipt = recovered['assignment']['task_effects'][0]
        self.assertEqual(receipt['record'], {'record_id': task['record_id'], 'revision': 2})
        self.assertEqual(receipt['task_state'], 'in_progress')
        self.assertEqual(receipt['claim']['session_id'], self.session)
        self.assertEqual(receipt['claim']['fence'], 2)
        self.assertEqual(self.cloud.records[task['record_id']]['revision'], 2)
        self.assertIn(original['request_key'], self.cloud.requests)
        self.assertEqual(recovered['assignment']['completion'], {'record_id': operation, 'revision': 1})

    def test_checkpoint_context_rejection_retries_same_eight_refs_and_cutoff_after_reload(self):
        self.cloud.generation = 16
        self.service.context()
        for count in (1, 2, 2):
            operation, token = self.assign()
            self.finish_assignment(operation, token, [
                {'kind': 'note', 'data': dict(self.note, title=f'Part {index}')} for index in range(count)])
        checkpoint_id = identifier()
        self.cloud.generation = 17
        with self.assertRaises(MemoryClientError) as error:
            self.memory.checkpoint(summary='Original boundary', checkpoint_id=checkpoint_id)
        self.assertEqual(error.exception.code, 'context_stale')
        checkpoint = self.memory.status(detail=True)['checkpoints'][0]
        self.assertEqual((checkpoint['state'], checkpoint['error'], checkpoint['cutoff'], len(checkpoint['refs'])),
                         ('blocked', 'context_stale', 3, 8))
        self.assertNotIn(checkpoint_id, self.cloud.records)
        self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())
        manifest_path = self.memory.runtime + '/checkpoints/' + checkpoint_id + '.json'
        original_manifest = read_private(self.root, manifest_path)['manifest']
        original_request = next(body for route, body in self.cloud.calls
                                if route == '/v1/memory/write' and body['record_id'] == checkpoint_id)
        self.assertEqual(original_request['authority_generation'], 16)
        later = self.memory.enqueue('After the original cutoff', source=self.source)['assignment']['operation_id']
        self.service.context()
        result = Maintenance(self.service).advance_checkpoint(checkpoint_id)
        self.assertEqual(result['status'], 'checkpointed')
        self.assertEqual(result['checkpoint']['checkpoint_id'], checkpoint_id)
        self.assertEqual(result['checkpoint']['cutoff'], 3)
        self.assertEqual(result['checkpoint']['refs'], checkpoint['refs'])
        self.assertNotIn(later, result['checkpoint']['assignments'])
        self.assertNotIn('error', result['checkpoint'])
        self.assertEqual(read_private(self.root, manifest_path)['manifest'], original_manifest)
        prepares = [body for route, body in self.cloud.calls if route == '/v1/memory/write'
                    and body['record_id'] == checkpoint_id and body['data']['state'] == 'prepared']
        self.assertEqual([body['authority_generation'] for body in prepares], [16, 17])
        self.assertEqual({body['request_key'] for body in prepares}, {original_request['request_key']})
        self.assertEqual(self.cloud.records[checkpoint_id]['revision'], 2)

    def test_checkpoint_rejection_preserves_prepared_record_and_saved_manifest_segments(self):
        refs = []
        for index in range(100):
            record = self.service.save('note', dict(self.note, title=f'Reference {index}'), source=self.source)
            refs.append({'record_id': record['record_id'], 'revision': record['revision']})
        checkpoint_id = identifier()
        segment_id = str(uuid.uuid5(uuid.UUID(checkpoint_id), 'manifest:0'))
        invoke = self.cloud.invoke

        def reject_finalization(route, body):
            if (route == '/v1/memory/write' and body['record_id'] == checkpoint_id
                    and body['data']['state'] == 'saved'):
                raise MemoryClientError('context_stale', request_key=body['request_key'])
            return invoke(route, body)

        with patch.object(self.cloud, 'invoke', side_effect=reject_finalization):
            with self.assertRaises(MemoryClientError):
                self.memory.checkpoint(summary='Preserve every segment', refs=refs, checkpoint_id=checkpoint_id)
        self.assertEqual(self.cloud.records[segment_id]['revision'], 2)
        self.assertEqual(self.cloud.records[segment_id]['data']['state'], 'saved')
        self.assertEqual(self.cloud.records[checkpoint_id]['revision'], 1)
        self.assertEqual(self.cloud.records[checkpoint_id]['data']['state'], 'prepared')
        self.assertEqual(self.memory.status(detail=True)['checkpoints'][0]['state'], 'blocked')
        directory = self.memory.runtime + '/checkpoints/'
        manifests = {key: read_private(self.root, directory + key + '.json')['manifest']
                     for key in (checkpoint_id, segment_id)}
        calls = len(self.cloud.calls)
        result = self.memory.recover_checkpoint(checkpoint_id, worker_stopped=True)
        self.assertEqual(result['status'], 'checkpointed')
        self.assertEqual(result['checkpoint']['refs'], refs)
        self.assertEqual(self.cloud.records[segment_id]['revision'], 2)
        self.assertEqual(self.cloud.records[checkpoint_id]['revision'], 2)
        writes = [body for route, body in self.cloud.calls[calls:] if route == '/v1/memory/write']
        self.assertEqual([(body['record_id'], body['expected_revision']) for body in writes], [(checkpoint_id, 1)])
        for key, manifest in manifests.items():
            self.assertEqual(read_private(self.root, directory + key + '.json')['manifest'], manifest)

    def test_checkpoint_unknown_outcomes_and_recovery_read_errors_remain_fenced(self):
        for lost_state, failed_route in (('prepared', '/v1/operations/lookup'), ('saved', '/v1/memory/read')):
            with self.subTest(lost_state=lost_state, failed_route=failed_route):
                checkpoint_id = identifier()
                invoke = self.cloud.invoke

                def lose_checkpoint(route, body):
                    if (route == '/v1/memory/write' and body['record_id'] == checkpoint_id
                            and body['data']['state'] == lost_state):
                        self.cloud.lose_next = True
                    return invoke(route, body)

                with patch.object(self.cloud, 'invoke', side_effect=lose_checkpoint):
                    with self.assertRaises(MemoryClientError):
                        self.memory.checkpoint(summary='Original unknown boundary', checkpoint_id=checkpoint_id)
                pending_path = self.memory.runtime + '/pending-memory.json'
                pending = read_private(self.root, pending_path)
                calls = len(self.cloud.calls)
                result = self.memory.advance_checkpoint(checkpoint_id)
                self.assertEqual((result['status'], result['checkpoint']['state']), ('uncertain', 'applying'))
                self.assertEqual(len(self.cloud.calls), calls)
                with self.assertRaises(MaintenanceError):
                    self.memory.recover_checkpoint(checkpoint_id, worker_stopped=False)

                def reject_recovery(route, body):
                    if route == failed_route and (route.endswith('/lookup') or body['record_id'] == checkpoint_id):
                        raise MemoryClientError('context_stale')
                    return invoke(route, body)

                with patch.object(self.cloud, 'invoke', side_effect=reject_recovery):
                    with self.assertRaises(MemoryClientError):
                        self.memory.recover_checkpoint(checkpoint_id, worker_stopped=True)
                self.assertEqual(self.memory.advance_checkpoint(checkpoint_id)['status'], 'uncertain')
                self.assertEqual(read_private(self.root, pending_path), pending)
                recovered = Maintenance(self.service).recover_checkpoint(checkpoint_id, worker_stopped=True)
                self.assertEqual(recovered['status'], 'checkpointed')
                self.assertEqual(recovered['checkpoint']['checkpoint_id'], checkpoint_id)
                self.assertEqual(self.cloud.records[checkpoint_id]['revision'], 2)
                self.assertIn(pending['body']['request_key'], self.cloud.requests)
                self.assertFalse((self.root / pending_path).exists())

    def test_checkpoint_unknown_then_definitive_replay_rejection_keeps_original_boundary_retryable(self):
        checkpoint_id = identifier()
        invoke = self.cloud.invoke

        def reject(route, body):
            if route == '/v1/memory/write' and body['record_id'] == checkpoint_id:
                raise MemoryClientError(rejection, request_key=body['request_key'])
            return invoke(route, body)

        rejection = 'outcome_unknown'
        with patch.object(self.cloud, 'invoke', side_effect=reject):
            with self.assertRaises(MemoryClientError):
                self.memory.checkpoint(summary='Exact original cutoff', checkpoint_id=checkpoint_id)
            checkpoint = self.memory.status(detail=True)['checkpoints'][0]
            self.assertEqual(checkpoint['state'], 'applying')
            rejection = 'context_stale'
            with self.assertRaises(MemoryClientError):
                self.memory.recover_checkpoint(checkpoint_id, worker_stopped=True)
        rejected = self.memory.status(detail=True)['checkpoints'][0]
        self.assertEqual((rejected['state'], rejected['error']), ('blocked', 'context_stale'))
        self.assertEqual(rejected['refs'], checkpoint['refs'])
        self.assertEqual(rejected['cutoff'], checkpoint['cutoff'])
        self.assertNotIn(checkpoint_id, self.cloud.records)
        self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())
        # Old versions left even definitive rejections applying. Their original
        # boundary must remain recoverable, not be replaced by a new checkpoint.
        with self.memory._transaction() as state:
            state['checkpoints'][checkpoint_id]['state'] = 'applying'
        recovered = self.memory.recover_checkpoint(checkpoint_id, worker_stopped=True)
        self.assertEqual(recovered['status'], 'checkpointed')
        self.assertEqual(recovered['checkpoint']['checkpoint_id'], checkpoint_id)
        self.assertEqual(recovered['checkpoint']['refs'], checkpoint['refs'])
        self.assertEqual(self.cloud.records[checkpoint_id]['revision'], 2)

    def test_closure_barrier_prevents_new_acceptance_between_validation_and_cloud_close(self):
        saved = self.memory.checkpoint(summary='No outstanding work')
        original_finish = self.service.finish

        def finish_while_new_intent_arrives(**kwargs):
            with self.assertRaises(MaintenanceError):
                self.memory.enqueue('Arrived after closure started', source=self.source)
            return original_finish(**kwargs)

        with patch.object(self.service, 'finish', side_effect=finish_while_new_intent_arrives):
            self.assertEqual(self.memory.finish(saved['checkpoint']['checkpoint_id'])['status'], 'closed')
        self.assertEqual(self.memory.status()['pending_count'], 0)

    def test_finish_recovery_requires_the_original_persisted_closure(self):
        other = self.memory.checkpoint(summary='Different handover')['checkpoint']['checkpoint_id']
        first = self.memory.checkpoint(summary='Original handover')['checkpoint']['checkpoint_id']
        with patch.object(self.service, 'finish') as finish:
            with self.assertRaises(MaintenanceError):
                self.memory.finish(first, recover_only=True)
            finish.assert_not_called()
        with patch.object(self.service, 'finish', side_effect=MemoryClientError('outcome_unknown')):
            with self.assertRaises(MemoryClientError):
                self.memory.finish(first)
        restored = Maintenance(self.service)
        with patch.object(self.service, 'finish') as finish:
            with self.assertRaises(MaintenanceError):
                restored.finish(other, recover_only=True)
            finish.assert_not_called()
        self.assertEqual(restored.finish(first, recover_only=True)['status'], 'closed')

    def test_large_snapshot_chains_bounded_manifests_without_losing_exact_versions(self):
        expected = set()
        for batch in range(2):
            operation, token = self.assign(f'Import bounded batch {batch}', workstream=f'batch:{batch}')
            result = self.finish_assignment(operation, token, [
                {'kind': 'note', 'data': dict(self.note, title=f'Note {batch}-{index}')}
                for index in range(50)])
            expected.update((ref['record_id'], ref['revision']) for ref in result['assignment']['saved_refs'])
        saved = self.memory.checkpoint(summary='All accepted versions retained')
        root = saved['checkpoint']['receipt']
        visited = set()

        def walk(ref):
            record = self.cloud.history[(ref['record_id'], ref['revision'])]
            if record['kind'] == 'checkpoint':
                self.assertLessEqual(len(record['data']['memory_refs']), 100)
                for child in record['data']['memory_refs']:
                    walk(child)
            else:
                visited.add((ref['record_id'], ref['revision']))

        walk(root)
        self.assertEqual(visited, expected)
        next_boundary = self.memory.checkpoint(summary='No later changes')
        self.assertEqual(next_boundary['checkpoint']['refs'], [root])

    def test_scope_promotion_and_correction_preserve_source_and_task_ownership(self):
        task = self.service.save('task', {'title': 'Shared lesson', 'description': 'Project-specific evidence.', 'state': 'open'},
                                 source=self.source)
        claimed = self.memory.claim_task(task['record_id'], action='claim', expected_revision=1,
                                         source=self.source, lease_seconds=60)
        self.memory.claim_task(task['record_id'], action='complete', expected_revision=2,
                               source=self.source, fence=claimed['claim']['fence'])
        self.memory.reclassify(task['record_id'], scope='universal', source=self.source, reason='Share the lesson')
        promoted = next(record for record in self.cloud.records.values()
                        if record['kind'] == 'note' and record.get('project_id') is None)
        self.assertIn('maven-origin:' + task['record_id'], promoted['tags'])
        self.assertIn('completed', promoted['data']['body_markdown'])
        self.assertEqual(self.cloud.records[task['record_id']]['revision'], 3)
        prior = deepcopy(promoted)
        self.memory.reclassify(promoted['record_id'], scope='project', source=self.source,
                                reason='Keep this project-specific after review')
        retired = self.cloud.records[promoted['record_id']]
        self.assertIn('maven-scope-superseded', retired['tags'])
        self.assertEqual(self.cloud.history[(promoted['record_id'], prior['revision'])], prior)
        self.assertTrue(any(record['kind'] == 'note' and record.get('project_id') == self.project
                            and 'maven-origin:' + promoted['record_id'] in record['tags']
                            for record in self.cloud.records.values()))

    def test_ordinary_writes_cannot_impersonate_completion_or_occupy_lifecycle_ids(self):
        target, target_token = self.assign('Target reconciliation', workstream='target')
        other, other_token = self.assign('Other reconciliation', workstream='other')
        checkpoint = self.memory.checkpoint(summary='Both assignments required')['checkpoint']['checkpoint_id']
        forged = {'session_id': self.session, 'name': 'memory-maintenance',
                  'state': 'completed', 'summary': 'Falsely completed'}
        attempts = [
            {'kind': 'operation', 'data': forged, 'record_id': identifier()},
            {'kind': 'operation', 'data': dict(forged, name='workspace-intake'), 'record_id': target},
            {'kind': 'note', 'data': self.note, 'record_id': checkpoint},
        ]
        for change in attempts:
            with self.subTest(change=change), self.assertRaises(MaintenanceError):
                self.memory.apply(other, worker_token=other_token, changes=[change])
        with self.assertRaises(MaintenanceError):
            self.memory.save('operation', forged, source=self.source, record_id=target)
        self.assertNotIn(target, self.cloud.records)
        self.assertNotIn(checkpoint, self.cloud.records)
        self.assertEqual(self.memory.status(other, detail=True)['assignment']['effects'], [])
        self.assertEqual(self.memory.advance_checkpoint(checkpoint)['status'], 'pending')
        real = self.memory.complete(target, worker_token=target_token, no_changes=True,
                                    summary='Current memory already reflects the authorized intent.')
        self.assertEqual(real['status'], 'completed')
        self.assertEqual(self.cloud.records[target]['data']['summary'],
                         'Current memory already reflects the authorized intent.')
        workspace = self.memory.save('operation', dict(forged, name='workspace-intake',
                                                       summary='Actual workspace intake completed.'),
                                     source=self.source)
        self.assertTrue(workspace['cloud_saved'])
        self.assertEqual(self.memory.advance_checkpoint(checkpoint)['status'], 'pending')

    def test_outage_accepts_bounded_intent_but_does_not_claim_cloud_completion(self):
        # A worker process without loaded authority cannot start semantic work during an outage.
        worker = SessionService(self.root, self.session, client=self.cloud, env=self.root.parent / 'instance' / '.env')
        with patch.object(self.cloud, 'load_context', side_effect=MemoryClientError('transport_unavailable')):
            queued = self.memory.enqueue('Reconcile after connectivity returns', source=self.source)
            self.assertFalse(queued['cloud_saved'])
            with self.assertRaises(MemoryClientError):
                Maintenance(worker).claim(queued['assignment']['operation_id'], worker='offline-worker')
            self.assertEqual(Maintenance(self.service).status()['pending_count'], 1)
        self.assertFalse(any(record['kind'] == 'operation' for record in self.cloud.records.values()))

    def test_claim_returns_cached_context_reference_without_reloading_authority(self):
        loads, calls = self.cloud.context_loads, len(self.cloud.calls)
        queued = self.memory.enqueue('Reconcile current plan', source=self.source)
        claimed = self.memory.claim(queued['assignment']['operation_id'], worker='fixture-reconciler')
        self.assertEqual(claimed['context_ref'], {'authority_generation': 1, 'slugs': ['core']})
        self.assertNotIn('context', claimed)
        self.assertEqual((self.cloud.context_loads, len(self.cloud.calls)), (loads, calls))

    def test_status_apply_and_revise_default_to_bounded_summaries(self):
        operation, token = self.assign()
        applied = self.memory.apply(operation, worker_token=token, changes=[
            {'kind': 'note', 'data': dict(self.note, title=f'Note {index}')} for index in range(3)])
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(applied['counts'], {'saved': 3, 'applying': 0, 'pending': 0, 'failed': 0})
        self.assertEqual(len(applied['saved_refs']), 3)
        self.assertEqual((applied['last_error'], applied['pending_effects']), (None, []))
        self.assertNotIn('assignment', applied)
        summary = self.memory.status(operation)
        self.assertEqual({key: summary[key] for key in ('state', 'counts', 'saved_refs')},
                         {'state': 'applied', 'counts': applied['counts'], 'saved_refs': applied['saved_refs']})
        self.assertNotIn('effects', summary)
        self.assertNotIn('request_key', str(summary))  # No effect request bodies in summaries.
        detailed = self.memory.status(operation, detail=True)['assignment']
        self.assertEqual(len(detailed['effects']), 3)
        self.assertIn('request', detailed['effects'][0])
        revised = self.memory.revise(operation, worker_token=token, changes=[{'kind': 'note', 'data': self.note}])
        self.assertEqual((revised['status'], revised['counts']['pending']), ('ready', 1))
        self.assertIn('assignment', self.memory.revise(operation, worker_token=token, detail=True,
                                                       changes=[{'kind': 'note', 'data': self.note}]))
        overall = self.memory.status()
        self.assertEqual([item['operation_id'] for item in overall['assignments']], [operation])
        self.assertNotIn('request_key', str(overall))
        self.assertIn('scopes', self.memory.status(detail=True)['assignments'][0])

    def test_apply_returns_continue_after_its_budget_and_resumes_with_the_same_token(self):
        operation, token = self.assign()
        self.memory.apply_budget = 0
        changes = [{'kind': 'note', 'data': dict(self.note, title=f'Note {index}')} for index in range(3)]
        first = self.memory.apply(operation, worker_token=token, changes=changes)
        self.assertEqual((first['status'], first['state'], first['continue']), ('applying', 'applying', True))
        self.assertEqual(first['counts'], {'saved': 1, 'applying': 0, 'pending': 2, 'failed': 0})
        self.assertEqual(self.memory.status(operation)['state'], 'claimed')
        second = self.memory.apply(operation, worker_token=token)
        self.assertEqual(second['counts']['saved'], 2)
        self.memory.apply_budget = 20
        final = self.memory.apply(operation, worker_token=token, changes=changes)
        self.assertEqual((final['status'], final['counts']['saved']), ('applied', 3))
        self.assertNotIn('continue', final)
        self.assertEqual(len({ref['record_id'] for ref in final['saved_refs']}), 3)

    def test_explicit_save_definitive_rejection_leaves_nothing_pending_and_reports_detail(self):
        invoke = self.cloud.invoke
        detail = 'data.subject_id must reference an entity record'

        def reject(route, body):
            if route == '/v1/memory/write' and body['kind'] == 'risk':
                raise MemoryClientError('reference_unavailable', request_key=body['request_key'], detail=detail)
            return invoke(route, body)

        self.memory.checkpoint(summary='Nothing pending before the rejected save')
        risk = {'title': 'Exposure', 'body_markdown': 'Possible exposure.', 'state': 'open', 'subject_id': identifier()}
        with patch.object(self.cloud, 'invoke', side_effect=reject), self.assertRaises(MemoryClientError) as error:
            self.memory.save('risk', risk, source=self.source)
        self.assertEqual((error.exception.code, error.exception.detail), ('reference_unavailable', detail))
        self.assertEqual(self.memory.status()['pending_count'], 0)
        self.assertEqual(self.memory.status()['assignment_states'].get('cancelled'), 1)
        self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())
        later = self.memory.checkpoint(summary='Rejected save does not block checkpoints')
        self.assertEqual(later['status'], 'checkpointed')

    def test_explicit_save_field_errors_are_rejected_locally_before_queueing(self):
        calls = len(self.cloud.calls)
        with self.assertRaises(MemoryClientError) as error:
            self.memory.save('note', {'title': 'Missing body', 'body': 'wrong field', 'category': 'work'},
                             source=self.source)
        self.assertEqual(error.exception.code, 'invalid_request')
        self.assertIn('body_markdown', error.exception.detail)
        self.assertIsNone(error.exception.request_key)
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertEqual(self.memory.status()['assignment_states'], {})
        with self.assertRaises(MemoryClientError) as direct:
            self.service.save('note', {'title': 'Missing body', 'category': 'work'}, source=self.source)
        self.assertIn('body_markdown', direct.exception.detail)
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertFalse((self.root / self.memory.runtime / 'pending-memory.json').exists())

    def test_explicit_task_claim_rejection_cancels_its_assignment(self):
        task = self.service.save('task', {'title': 'Check', 'description': '', 'state': 'open'}, source=self.source)
        invoke = self.cloud.invoke

        def reject(route, body):
            if route == '/v1/memory/claim':
                raise MemoryClientError('claim_conflict', request_key=body['request_key'])
            return invoke(route, body)

        with patch.object(self.cloud, 'invoke', side_effect=reject), self.assertRaises(MemoryClientError):
            self.memory.claim_task(task['record_id'], action='claim', expected_revision=1,
                                   source=self.source, lease_seconds=60)
        self.assertEqual(self.memory.status()['pending_count'], 0)
        claimed = self.memory.claim_task(task['record_id'], action='claim', expected_revision=1,
                                         source=self.source, lease_seconds=60)
        self.assertEqual(claimed['task_state'], 'in_progress')

    def test_resume_with_nothing_to_record_completes_catch_up_inline(self):
        self.memory.pause(anchor='conversation:turn-3')
        result = self.memory.resume(discussion='resume', source=self.source, nothing_to_record=True)
        self.assertEqual((result['status'], result['automatic_updates'], result['cloud_saved']), ('ready', True, True))
        self.assertEqual(result['catch_up']['state'], 'completed')
        self.assertEqual(result['assignment']['state'], 'completed')
        self.assertEqual(self.memory.status()['pending_count'], 0)
        operation = result['catch_up']['operation_id']
        self.assertEqual(self.cloud.records[operation]['data']['state'], 'completed')
        self.assertEqual(self.memory.checkpoint(summary='Nothing pending after resume')['status'], 'checkpointed')


if __name__ == '__main__':
    unittest.main()
