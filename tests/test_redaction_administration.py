"""Content-free owner approvals and resumable effects; only temporary homes and simulated providers."""
import argparse
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.administration import AdminInputError, AdminOperations
from tools.memory.redaction_objects import ObjectRedactionError
from tools.memory.validation import MemoryError
from tools.setup_common import SetupPending
from tools.tui.administration import _typed_target, present


def uid():
    return str(uuid4())


class RedactionAdministrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / 'home'
        self.controller = AdminOperations(self.home)
        self.binding = {'instance_id': uid(), 'recovery_epoch': uid()}
        self.session = object()
        self.secret = 'synthetic-private-storage-name-not-for-any-journal'
        self.mock('tools.administration.AdminOperations._cleanup_admin', return_value=(
            {'MAVEN_AWS_PROFILE': self.secret}, self.binding, self.session))
        self.mock('tools.environment_migrations.prerequisite', return_value=None)
        self.target = {'kind': 'project_record', 'id': uid(), 'project_id': uid()}
        self.object = {'kind': 'recovery_object', 'id': uid()}
        self.objects = {'format': 1, 'scope': uid(), 'objects': [
            {**self.object, 'class': 'retirement_manifest', 'versions': ['e' * 64], 'preserve': None}]}
        closure = [self.target, self.object]
        self.plan = {**self.binding, 'targets': [self.target], 'permanent_targets': closure,
                     'revisions': [{**t, 'revision_token': 'b' * 64} for t in closure],
                     'bundles': [], 'plan_digest': 'a' * 64, 'objects': self.objects}
        self.review = self.mock('tools.memory.admin.review_redaction', return_value=self.plan)
        self.effect = self.mock('tools.memory.admin.apply_redaction', side_effect=self.apply)
        self.sweep = self.mock('tools.reconcile_redaction.sweep_reviewed')
        self.store = self.mock('tools.redaction_administration._store').return_value
        self.salt = uid()
        self.mock('tools.memory.admin.redaction_salt', return_value=self.salt)
        self.purged = {'status': 'objects_verified_purged', 'objects': [{**self.object, 'versions': 1}]}

    def mock(self, name, **kwargs):
        mocked = patch(name, **kwargs)
        self.addCleanup(mocked.stop)
        return mocked.start()

    def apply(self, config, session, *, operation_id, review):
        self.assertEqual(config, self.binding)
        self.assertIs(session, self.session)
        self.assertEqual(review, self.plan)
        return {'job_id': operation_id, 'live_data_redacted': True, 'objects': self.purged,
                'backup_retention_pending': True, 'offline_copies_unverified': True}

    def begin(self, **changes):
        return self.controller.begin('cleanup', {'action': 'redact', 'targets': [self.target], **changes})

    def response(self, result, **changes):
        q = result['pending_interaction']
        return {'approved': True, 'review_digest': q['review_digest'],
                'source': {'origin': 'owner_chat', 'excerpt': q['owner_phrase']},
                **({'confirmations': q['confirmations']} if q['confirmations'] else {}), **changes}

    def respond(self, result, response):
        q = result['pending_interaction']
        return self.controller.respond(result['operation_id'], q['interaction_id'], q['revision'], response)

    def approve(self, result):
        return self.respond(result, self.response(result))

    def journal(self):
        return ''.join(path.read_text() for path in self.controller.directory.glob('*.json'))

    def test_selections_and_old_authority_are_disjoint(self):
        for changes in ({'reason': self.secret}, {'record_ids': [uid()]}, {'targets': []}, {'targets': [None]},
                        {'targets': [{**self.target, 'label': self.secret}]}, {'targets': [{**self.target, 'id': None}]},
                        {'targets': [{**self.target, 'kind': 'ssm'}]}, {'targets': [self.target, self.target]},
                        {'targets': [{**self.target, 'project_id': self.target['project_id'].upper()}]},
                        {'targets': [{'kind': 'project_record', 'id': uid()}]}, {'action': 'erase'}, {'action': 'restore'},
                        {'action': 'purge_provider'}, {'action': 'restore_redactions'}, {'action': 'retry_objects'},
                        {'action': 'redaction_pending'}):
            with self.subTest(changes=changes), self.assertRaises(AdminInputError):
                self.begin(**changes)
        self.assertFalse(self.controller.directory.exists())
        self.review.assert_not_called()
        self.assertEqual(_typed_target('project_record:' + self.target['project_id'] + ':' + self.target['id']), self.target)
        with self.assertRaises(argparse.ArgumentTypeError):
            _typed_target('ssm:' + self.secret)

    def test_review_and_decline_never_accept_free_text_or_replay_prior_owner_phrase(self):
        result = self.begin()
        self.assertEqual(result['pending_interaction']['review']['candidate'], self.plan)
        original = self.response(result)
        for fields in ({'source': {'origin': self.secret, 'excerpt': original['source']['excerpt']}},
                       {'source': {'origin': 'owner_chat', 'excerpt': self.secret}},
                       {'source': {**original['source'], 'origin': 'owner_terminal'}},
                       {'approved': False, 'source': {'origin': self.secret}}, {self.secret: True},
                       {'approved': 1}, {'confirmations': {self.secret: self.secret}}):
            with self.subTest(fields=fields), self.assertRaises(AdminInputError) as err:
                self.respond(result, {**original, **fields})
            self.assertNotIn(self.secret, str(err.exception))
        paused = self.respond(result, {'approved': False, 'review_digest': original['review_digest']})
        self.assertNotEqual(paused['pending_interaction']['owner_phrase'], original['source']['excerpt'])
        with self.assertRaises(AdminInputError):
            self.approve(result)
        with self.assertRaises(AdminInputError):
            self.respond(paused, original)
        self.assertNotIn(self.secret, self.journal())
        self.effect.assert_not_called()
        self.sweep.assert_not_called()

    def test_blocked_object_then_local_failure_report_distinct_effects_and_resume(self):
        result = self.approve(self.begin())
        op = result['operation_id']
        verified = self.purged
        self.purged = {'status': 'blocked', 'code': 'object_retained'}
        result = self.controller.advance(op)
        self.assertEqual((result['state'], result['error']), ('blocked', 'object_retained'))
        self.assertTrue(result['outcome']['live_data_redacted'])
        self.assertTrue(result['outcome']['known_local_sweep_verified'])
        self.assertTrue(result['outcome']['backup_retention_pending'])
        self.assertTrue(result['outcome']['offline_copies_unverified'])
        self.purged = verified
        self.sweep.side_effect = SetupPending(self.secret)
        result = self.controller.advance(op)
        self.assertEqual((result['state'], result['error']), ('blocked', 'known_local_sweep_blocked'))
        self.assertFalse(result['outcome']['known_local_sweep_verified'])
        self.assertEqual(result['outcome']['objects'], verified)
        self.sweep.side_effect = None
        result = AdminOperations(self.home).advance(op)
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['outcome']['known_local_sweep_verified'])
        self.assertEqual(self.effect.call_count, 2)
        for call in self.effect.call_args_list:
            self.assertEqual(call.kwargs, {'operation_id': op, 'review': self.plan})
        self.assertEqual(self.sweep.call_args.args, (self.home, self.binding['instance_id'], self.plan['permanent_targets']))
        self.assertNotIn(self.secret, self.journal())
        with self.assertRaises(AdminInputError):
            self.controller.begin('recovery', {'operation_id': op, 'action': 'recover'})

    def test_lost_sql_reply_reuses_original_operation_and_exact_approval(self):
        approved = self.approve(self.begin())
        op = approved['operation_id']
        self.effect.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.controller.advance(op)
        resumed = AdminOperations(self.home)
        self.assertEqual(resumed.status(op)['state'], 'uncertain')
        self.effect.side_effect = self.apply
        self.assertEqual(resumed.advance(op)['state'], 'completed')
        self.assertEqual(self.effect.call_args_list[0], self.effect.call_args_list[1])
        self.assertEqual(self.review.call_count, 1)
        self.assertEqual(self.sweep.call_count, 1)

    def test_bundle_callback_covers_markers_from_earlier_jobs_on_this_machine(self):
        from tools.memory.redaction_objects import target
        old_sha = '9' * 64
        marker = target(self.binding, self.salt, 'skill_bundle', old_sha)
        self.mock('tools.reconcile_redaction.Replica.targets', return_value=[marker, self.target])
        self.sweep.side_effect = lambda *args, bundle_check, **kwargs: self.assertEqual(
            bundle_check(['8' * 64, old_sha]), {1})
        op = self.approve(self.begin())['operation_id']
        self.assertEqual(self.controller.advance(op)['state'], 'completed')
        self.assertNotIn(old_sha, self.journal())
        self.assertNotIn(self.salt, self.journal())

    def test_candidate_inventory_is_read_only_and_never_returns_provider_names(self):
        self.mock('tools.memory.admin.redaction_candidates', return_value={'candidates': [self.object], 'next_after': None})
        counts = self.mock('tools.memory.redaction_objects.recovery_inventory', return_value={
            'protected_ledger': 2, 'foreign_or_malformed_manifest': 1})
        result = self.controller.begin('cleanup', {'action': 'redaction_candidates'})
        self.assertEqual(result['outcome']['recovery_inventory'], counts.return_value)
        self.assertEqual(result['outcome']['candidates'], [self.object])
        self.assertTrue(result['outcome']['read_only'])
        self.store.redaction_candidates.return_value = [{'kind': 'ssm', 'id': uid(), 'name': self.secret}]
        result = self.controller.begin('cleanup', {'action': 'provider_candidates'})
        self.assertEqual(result['state'], 'blocked')
        self.assertNotIn(self.secret, self.journal())
        self.effect.assert_not_called()
        self.sweep.assert_not_called()

    def test_rejected_sql_scope_never_sweeps_or_discloses_diagnostics(self):
        result = self.approve(self.begin())
        self.effect.side_effect = MemoryError('redaction_scope_changed', 409, detail=self.secret)
        result = self.controller.advance(result['operation_id'])
        self.assertEqual((result['state'], result['error']), ('blocked', 'redaction_scope_changed'))
        self.assertNotIn('outcome', result)
        self.sweep.assert_not_called()
        self.assertNotIn(self.secret, self.journal())

    def test_malformed_review_and_provider_result_never_journal_raw_service_output(self):
        for edit in ({'title': self.secret}, {'revisions': [{**self.target, 'revision_token': self.secret}]},
                     {'targets': [{'kind': 'record', 'id': uid()}]}, {'objects': {**self.objects, 'bucket': self.secret}}):
            with self.subTest(edit=edit):
                self.review.return_value = {**self.plan, **edit}
                result = self.begin()
                self.assertIsNone(result['pending_interaction'])
                self.assertEqual(result['state'], 'blocked')
        self.review.return_value = self.plan
        approved = self.approve(self.begin())
        self.purged = {**self.purged, 'diagnostics': self.secret}
        result = self.controller.advance(approved['operation_id'])
        self.assertEqual(result['state'], 'uncertain')
        self.sweep.assert_not_called()
        self.assertNotIn(self.secret, self.journal())

    def test_ssm_purge_requires_both_attestations_private_resume_and_same_plan(self):
        target = {'kind': 'ssm', 'id': uid()}
        plan = {**target, 'scope': uid(), 'format': 1, 'versions': ['c' * 64]}
        self.store.review_redaction.return_value = plan
        self.store.apply_redaction.side_effect = [ObjectRedactionError('provider_locator_required'),
            {**target, 'status': 'provider_deletion_verified', 'versions': 1, 'issuer_revocation': 'owner_attested'}]
        result = self.begin(action='purge_provider', targets=[target])
        for confirmations in ({}, {'issuer_revoked': 'ISSUER REVOKED'}, {'issuer_revoked': self.secret}):
            with self.assertRaises(AdminInputError):
                self.respond(result, self.response(result, confirmations=confirmations))
        self.store.apply_redaction.assert_not_called()
        op = self.approve(result)['operation_id']
        result = self.controller.advance(op)
        self.assertEqual(result['error'], 'provider_locator_required')
        retry = AdminOperations(self.home, private_provider_retry=(op, self.secret))
        self.assertEqual(retry.advance(op)['outcome']['status'], 'provider_deletion_verified')
        self.store.apply_redaction.assert_called_with(plan, self.salt, issuer_revoked=True,
                                                      writers_quiesced=True, private_name=self.secret)
        self.assertNotIn(self.secret, self.journal())
        self.assertNotIn(self.salt, self.journal())
        self.effect.assert_not_called()

    def test_restore_only_approved_isolation_plan_can_release(self):
        plan = {**self.binding, 'source_epoch': uid(), 'source_cluster': 'a' * 64, 'destination_cluster': 'b' * 64,
                'checkpoint': 'f' * 64, 'sequence': 19}
        self.mock('tools.memory.admin.redaction_restore_review', return_value=plan)
        restore = self.mock('tools.memory.admin.redaction_restore_apply', return_value={**self.binding,
            'status': 'restore_verified', 'checkpoint': '0' * 64})
        result = self.controller.begin('cleanup', {'action': 'restore_redactions'})
        restore.assert_not_called()
        ui = Mock()
        ui.review.return_value = True
        ui.form.return_value = {'phrase': result['pending_interaction']['owner_phrase']}
        self.assertEqual(present(ui, self.home, operation_id=result['operation_id'], controller=self.controller), 0)
        self.assertIn(plan['checkpoint'], ui.review.call_args.args[1])
        saved = self.controller.status(result['operation_id'])
        self.assertEqual(saved['approvals'][0]['source']['origin'], 'owner_terminal')
        restore.assert_called_once_with(self.binding, self.session, plan)
        self.effect.assert_not_called()

    def test_pending_and_retry_resume_original_job_without_sql_resubmission(self):
        original = uid()
        self.mock('tools.memory.admin.redaction_pending', return_value=[{'job_id': original, 'objects': [self.object]}])
        result = self.controller.begin('cleanup', {'action': 'redaction_pending'})
        self.assertTrue(result['outcome']['read_only'])
        self.assertEqual(result['outcome']['jobs'][0]['job_id'], original)
        self.mock('tools.memory.admin.review_redaction_objects', return_value=deepcopy(self.objects))
        finish = self.mock('tools.memory.admin.finish_redaction_objects', return_value=self.purged)
        op = self.approve(self.controller.begin('cleanup', {'action': 'retry_objects', 'job_id': original}))['operation_id']
        finish.assert_not_called()
        result = self.controller.advance(op)
        self.assertEqual(result['outcome'], {'job_id': original, 'objects': self.purged})
        finish.assert_called_once_with(self.binding, self.session, original)
        self.effect.assert_not_called()
        self.sweep.assert_not_called()
