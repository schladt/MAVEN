"""Local state/recovery regressions with real controllers and simulated cloud storage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tools.memory.boundary import save_private
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

    def test_checkpoint_pins_cutoff_and_later_work_cannot_change_its_manifest(self):
        operation, token = self.assign()
        boundary = self.memory.checkpoint(summary='Before the later change')
        self.assertEqual(boundary['status'], 'pending')
        checkpoint = boundary['checkpoint']['checkpoint_id']
        later = self.memory.enqueue('Use the new plan', source=self.source, supersede=True)['assignment']['operation_id']
        self.assertNotEqual(self.memory.status(operation)['assignment']['state'], 'superseded')
        self.finish_assignment(operation, token, [{'kind': 'note', 'data': self.note}])
        saved = self.memory.advance_checkpoint(checkpoint)
        self.assertEqual(saved['status'], 'checkpointed')
        reference = self.memory.status(operation)['assignment']['effects'][0]['record']
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
        original = self.memory.status(operation)['assignment']['effects'][0]['request']
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
        item = self.memory.status(operation)['assignment']
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
        lease = first['assignment']['task_effects'][0]['claim']
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
                            self.assertEqual(self.memory.status(operation)['assignment']['state'], 'uncertain')
                            rejection = code
                            with self.assertRaises(MemoryClientError):
                                Maintenance(self.service).recover(operation, worker_stopped=True)
                    item = self.memory.status(operation)['assignment']
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
                saved_ref = applied['assignment']['saved_refs'][0]

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
                self.assertEqual(self.memory.status(operation)['assignment']['state'], 'blocked')
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
                item = self.memory.status(operation)['assignment']
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
        original = self.memory.status(operation)['assignment']['completion_request']
        invoke = self.cloud.invoke

        def reject_lookup(route, body):
            if route == '/v1/operations/lookup':
                raise MemoryClientError('not_authorized')
            return invoke(route, body)

        with patch.object(self.cloud, 'invoke', side_effect=reject_lookup):
            with self.assertRaises(MemoryClientError):
                self.memory.recover(operation, worker_stopped=True)
        item = self.memory.status(operation)['assignment']
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
        saved = self.memory.save('note', self.note, source=self.source, operation_id=operation, request_key=key)
        self.assertEqual(saved['status'], 'saved')
        note_ref = self.memory.status(operation)['assignment']['effects'][0]['record']
        self.assertEqual(self.cloud.records[note_ref['record_id']]['revision'], 1)
        with self.assertRaises(MaintenanceError):
            self.memory.save('note', dict(self.note, title='Different intent'), source=self.source,
                              operation_id=operation, request_key=key)

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
        first = self.memory.checkpoint(summary='Original handover')['checkpoint']['checkpoint_id']
        other = self.memory.checkpoint(summary='Different handover')['checkpoint']['checkpoint_id']
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
        self.assertEqual(self.memory.status(other)['assignment']['effects'], [])
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
        with patch.object(self.cloud, 'load_context', side_effect=MemoryClientError('transport_unavailable')):
            queued = self.memory.enqueue('Reconcile after connectivity returns', source=self.source)
            self.assertFalse(queued['cloud_saved'])
            with self.assertRaises(MemoryClientError):
                self.memory.claim(queued['assignment']['operation_id'], worker='offline-worker')
            self.assertEqual(Maintenance(self.service).status()['pending_count'], 1)
        self.assertFalse(any(record['kind'] == 'operation' for record in self.cloud.records.values()))


if __name__ == '__main__':
    unittest.main()
