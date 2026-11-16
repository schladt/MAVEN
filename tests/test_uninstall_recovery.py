"""Offline removal recovery boundaries; all removal targets are disposable fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from tools.tui import runtime
from tools.tui.uninstall import _approve
from tools.uninstall import cli, cloud, lifecycle, local
from tools.uninstall.common import PauseRequested, UninstallError, read_private, save_private


class RemovalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / 'installation'
        self.home.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {'HOME': str(self.base), 'MAVEN_HOME': str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        source = self.base / 'source'
        source.mkdir()
        venv = self.home / 'venv'
        venv.mkdir()
        (venv / 'fixture-runtime').write_text('owned disposable runtime')
        local.record_installation(self.home, source, [venv], [])

    def plan(self, *options):
        return cli.build_plan(cli.parser().parse_args([
            'plan', '--machine', '--home', str(self.home), '--output', str(self.base / 'operation'), *options]))

    def approval(self, planned):
        return cli.parser().parse_args(['resume', '--plan', str(planned['plan_path']),
            '--confirm', planned['plan']['plan_digest'], '--writers-quiescent', '--harness-config-risk'])

    def test_detach_failure_preserves_external_evidence_after_home_purge(self):
        root = self.base / 'project'
        root.mkdir()
        identifier = str(UUID(int=1))
        original = {'root': str(root), 'state': 'active', 'writers': ['original-stopped-writer'],
                    'caches': [str(self.home / 'login-cache')]}
        save_private(self.home / 'runtime/sessions.json', {
            'schema': 1, 'sessions': {identifier: original}, 'reservations': {}})
        cache = self.home / 'login-cache'
        cache.write_text('DO-NOT-ARCHIVE-LOGIN-BYTES')
        planned = self.plan('--offline')
        write = local.save_private
        failed = False

        def lose_response(path, value):
            nonlocal failed
            write(path, value)
            if Path(path) == self.home / 'runtime/sessions.json' and not failed:
                failed = True
                raise OSError('injected interruption after durable registry detach')

        with patch.object(local, 'save_private', side_effect=lose_response):
            first = cli.apply(self.approval(planned), reexec=False)
        self.assertEqual(first['status'], 'blocked')
        second = cli.apply(self.approval(planned), reexec=False)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['home_retention'][str(self.home)], 'removed')
        self.assertFalse(self.home.exists())
        evidence = list((self.base / 'operation/evidence').glob('uninstall-sessions-*.json'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(read_private(evidence[0])['original_registry']['sessions'][identifier], original)
        self.assertEqual(evidence[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue(list((self.base / 'operation/evidence').glob('machine-*.json')))
        for path in (self.base / 'operation/evidence').iterdir():
            self.assertNotIn(b'DO-NOT-ARCHIVE-LOGIN-BYTES', path.read_bytes())
        self.assertEqual(cli.apply(self.approval(planned), reexec=False)['status'], 'completed')

    def test_actual_cli_stdlib_only_apply_and_resume_after_runtime_deletion(self):
        planned = self.plan('--offline')
        command = [sys.executable, '-I', '-S', '-B', str(self.base / 'operation/runner/run.py'),
                   *cli._resume_arguments(self.approval(planned))]
        for _ in range(2):
            child = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['home_retention'][str(self.home)], 'removed')
            self.assertFalse(result['live_state_checked'])
        self.assertFalse(self.home.exists())

    def test_ui_keeps_old_stopped_attestation_distinct_from_new_risk(self):
        ui = Mock()
        ui.review.return_value = True
        ui.confirm.return_value = True
        for required, expected in [('harnesses-stopped', '--harnesses-stopped'),
                                   ('harness-config-risk', '--harness-config-risk')]:
            plan = {'requires': [required], 'targets': [], 'actions': [],
                    'confirmations': {}, 'plan_digest': 'a' * 64}
            with patch('tools.tui.uninstall._unchanged'):
                arguments = _approve(ui, self.base / 'plan.json', plan)
            self.assertIn(expected, arguments)
            self.assertNotIn('--harness-config-risk' if required == 'harnesses-stopped'
                             else '--harnesses-stopped', arguments)
        ui.confirm.return_value = False
        old = {'requires': ['harnesses-stopped'], 'targets': [], 'actions': [],
               'confirmations': {}, 'plan_digest': 'a' * 64}
        with patch('tools.tui.uninstall._unchanged'):
            self.assertIsNone(_approve(ui, self.base / 'plan.json', old))
        args = self.approval({'plan_path': self.base / 'plan.json', 'plan': old})
        with self.assertRaisesRegex(UninstallError, 'close affected assistant harnesses'):
            cli._approve(old, args)

    def test_provider_window_finishes_local_scope_and_resumes_exact_cloud_action(self):
        instance = str(UUID(int=2))
        cache = self.home / 'login-cache'
        cache.mkdir()
        identity = cloud._identity({'MAVEN_STACK_NAME': 'fixture', 'MAVEN_MEMORY_CLUSTER_ARN': 'cluster',
                                    'MAVEN_DEPLOY_LOGIN_CACHE': str(cache)})
        details = {'home': str(self.home), 'identity': identity, 'instance_id': instance,
                   'mode': 'purge', 'retained_only': True, 'resources': [], 'buckets': [],
                   'secrets': [{'arn': 'secret', 'versions': {}}], 'selected': [],
                   'snapshots': [], 'automated_backups': []}
        fragment = {'actions': [{'handler': 'cloud', 'kind': 'cloud-teardown', 'target': 'cluster',
                                'stage': 30, 'details': details}], 'requires': [], 'warnings': [],
                    'retained': [], 'targets': [{'scope': 'cloud', 'instance_id': instance}]}
        # The provider is a stateful offline boundary; dependency staging uses the real
        # protected inventory writer with an empty fixture distribution set.
        with patch.object(cloud, 'plan_cloud', return_value=fragment), \
                patch.object(runtime, '_distributions', return_value={}):
            planned = self.plan('--cloud', '--cloud-mode', 'purge')
        args = self.approval(planned)
        args.confirm_purge = instance
        manifest = self.base / 'operation/cloud-runtime/manifest.json'
        original_manifest = manifest.read_bytes()
        manifest.write_text('{}')
        with self.assertRaisesRegex(UninstallError, 'manifest changed'):
            cli.apply(args, reexec=False)
        self.assertTrue(self.home.exists())
        manifest.write_bytes(original_manifest)
        scheduled = False
        present = True
        deletes = []
        secret = Mock()

        def describe(**kwargs):
            if not present:
                error = Exception('absent')
                error.response = {'Error': {'Code': 'ResourceNotFoundException'}}
                raise error
            return {'ARN': 'secret', **({'DeletedDate': 'future-date'} if scheduled else {})}

        def delete(**kwargs):
            nonlocal scheduled
            scheduled = True
            deletes.append(kwargs)
            return {'DeletionDate': 'future-date'}

        secret.describe_secret.side_effect = describe
        secret.delete_secret.side_effect = delete
        cf, rds = Mock(), Mock()
        cf.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'DELETE_COMPLETE'}]}
        rds.describe_db_clusters.side_effect = type('Missing', (Exception,), {
            'response': {'Error': {'Code': 'DBClusterNotFoundFault'}}})()
        clients = {'secretsmanager': secret, 'cloudformation': cf, 'rds': rds}
        provider = Mock()
        provider.client.side_effect = clients.__getitem__
        with patch.object(cloud, '_Cloud', return_value=provider) as construct, patch.object(cloud, '_load') as load:
            first = cli.apply(args, reexec=False)
            self.assertEqual(first['status'], 'pending-provider')
            self.assertEqual(first['scope_status'], {'machine': 'completed', 'cloud': 'pending-provider'})
            self.assertFalse(self.home.exists())
            self.assertEqual(len(deletes), 1)
            self.assertEqual(deletes[0]['RecoveryWindowInDays'], 7)
            self.assertEqual(cli.apply(args, reexec=False)['status'], 'pending-provider')
            self.assertEqual(construct.call_args.args[0]['MAVEN_DEPLOY_LOGIN_CACHE'],
                             str(self.base / 'operation/authentication/deploy'))
            self.assertFalse(self.home.exists())
            self.assertEqual(len(deletes), 1)
            present = False
            self.assertEqual(cli.apply(args, reexec=False)['status'], 'completed')
            load.assert_not_called()
        self.assertEqual(read_private(planned['plan_path'])['operation_id'], planned['plan']['operation_id'])

    def test_staged_dependency_isolated_from_removed_source_and_manifest_tampering(self):
        package = self.home / 'installed-site'
        package.mkdir()
        module = package / 'recovery_fixture.py'
        module.write_text('VALUE = 73\n')
        distribution = Mock(version='1.0', files=[Path(module.name)])
        distribution.locate_file.side_effect = lambda path: package / path
        destination = self.base / 'protected'
        with patch.object(runtime, '_distributions', return_value={'fixture': distribution}):
            site = runtime._stage_dependencies(destination, plan_digest='fixture', ui=False)
        module.unlink()
        child = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                                'import sys; sys.path.insert(0,sys.argv[1]); '
                                'import recovery_fixture; print(recovery_fixture.VALUE)', str(site)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual((child.returncode, child.stdout.strip()), (0, '73'), child.stderr)
        runtime._verify_runtime(destination, 'fixture')
        (site / 'recovery_fixture.py').write_text('VALUE = 0\n')
        with self.assertRaisesRegex(UninstallError, 'file changed'):
            runtime._verify_runtime(destination, 'fixture')

    def test_workspace_kernel_lock_blocks_only_held_lock_and_keeps_legacy_evidence(self):
        root = self.base / 'project'
        root.mkdir()
        lock = root / '.maven/runtime/cloud-workspace-v2.lock'
        with local._lock(lock):
            with self.assertRaises(UninstallError):
                with local._execution_locks(self.home, str(root)):
                    self.fail('held workspace lock was ignored')
        with local._execution_locks(self.home, str(root)):
            pass
        legacy = root / '.maven/runtime/cloud-workspace.lock'
        save_private(legacy, {'original_owner': 'unknown'})
        with self.assertRaisesRegex(UninstallError, 'Legacy workspace'):
            with local._execution_locks(self.home, str(root)):
                self.fail('ambiguous legacy writer evidence was adopted')
        self.assertTrue(legacy.exists())


    def test_failed_external_write_does_not_invalidate_registration(self):
        root = self.base / 'project'
        root.mkdir()
        original = {'schema': 1, 'sessions': {str(UUID(int=9)): {
            'root': str(root), 'writers': ['stopped-writer'], 'state': 'active', 'caches': []}},
            'reservations': {}}
        save_private(self.home / 'runtime/sessions.json', original)
        planned = self.plan('--offline')
        write = local.save_private

        def fail_evidence(path, value):
            if 'evidence' in Path(path).parts:
                raise OSError('injected evidence write failure')
            write(path, value)

        with patch.object(local, 'save_private', side_effect=fail_evidence):
            self.assertEqual(cli.apply(self.approval(planned), reexec=False)['status'], 'blocked')
        self.assertEqual(read_private(self.home / 'runtime/sessions.json'), original)
        self.assertEqual(cli.apply(self.approval(planned), reexec=False)['status'], 'completed')


    def test_unfinished_installation_intent_survives_purge_without_adoption(self):
        identifier = str(UUID(int=15))
        receipt = {'schema': 1, 'home': str(self.home), 'inputs': {'source': 'reviewed'},
                   'owned': [], 'inventory': {}, 'applying': 'pip', 'reconciliations': [],
                   'completed': False}
        original = self.home / 'runtime/runtime-installations' / (identifier + '.json')
        save_private(original, receipt)
        planned = self.plan('--offline')
        self.assertTrue(any('not completed ownership' in item for item in planned['plan']['warnings']))
        self.assertEqual(cli.apply(self.approval(planned), reexec=False)['status'], 'completed')
        self.assertFalse(self.home.exists())
        preserved = list((self.base / 'operation/evidence').glob('installation-*.json'))
        self.assertEqual(len(preserved), 1)
        value = read_private(preserved[0])
        self.assertEqual(value['receipt'], receipt)
        self.assertFalse(value['installation_completed_claimed'])


class InventoryAndWaitTests(unittest.TestCase):
    def test_large_work_inventory_collects_all_pages_and_rejects_repeating_cursor(self):
        records = [{'id': str(UUID(int=index))} for index in range(1, 1254)]
        receipts = [{'request_key': str(UUID(int=index))} for index in range(1, 1502)]
        db = Mock()

        def page(expression, values, tx):
            key, source = ('request_key', receipts) if 'o.request_key' in expression else ('id', records)
            selected = [item for item in source if values['cursor'] is None or item[key] > values['cursor']]
            return json.dumps(selected[:lifecycle.EVIDENCE_PAGE_SIZE])

        db.json_scalar.side_effect = page
        self.assertEqual(lifecycle._work(db, 'transaction'), {'records': records, 'receipts': receipts})
        db.json_scalar.side_effect = lambda *args: json.dumps(records[:lifecycle.EVIDENCE_PAGE_SIZE])
        with self.assertRaisesRegex(UninstallError, 'cursor did not advance'):
            lifecycle._work(db, 'transaction')

    def test_provider_wait_is_bounded_and_cancellable_without_resubmission(self):
        now = [10.0]
        with patch.object(cloud.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(cloud.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            with cloud.provider_wait(3):
                observations = list(cloud._poll())
            self.assertEqual(len(observations), 3)
            self.assertAlmostEqual(now[0], 13.0)
            with cloud.provider_wait(3, lambda: True), self.assertRaises(PauseRequested):
                next(cloud._poll())
        with cloud.provider_wait():
            self.assertEqual(len(list(cloud._poll())), 1)
        with self.assertRaises(UninstallError):
            with cloud.provider_wait(301):
                self.fail('unbounded provider wait admitted')
