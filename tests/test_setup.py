"""Guided setup safety transitions, with no real login or cloud operations."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.memory.client import MemoryClientError
from tools.setup import SetupPending, Wizard, read_private, save_json


class GuidedSetupTests(unittest.TestCase):
    def wizard(self, directory):
        root = Path(directory).resolve()
        return Wizard(root / 'home', root)

    def test_existing_connection_cannot_enter_deployment_or_declare_ready_early(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': str(uuid4())})
            output = io.StringIO()
            with patch.object(wizard, 'existing'), patch.object(wizard, 'fresh') as fresh, \
                    patch.object(wizard, 'verify_authority', side_effect=MemoryClientError('not_authorized')), \
                    patch.object(wizard, 'project') as project, redirect_stdout(output):
                with self.assertRaises(MemoryClientError):
                    wizard.run()
            fresh.assert_not_called()
            project.assert_not_called()
            self.assertEqual(read_private(wizard.state_path)['mode'], 'existing')
            self.assertNotIn('ready_project', read_private(wizard.state_path))

    def test_root_routine_identity_is_rejected_before_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            wizard.settings({'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_AWS_PROFILE': 'selected'})
            session = Mock()
            session.get_credentials.return_value.method = 'login'
            session.client.return_value.get_caller_identity.return_value = {
                'Arn': 'arn:aws:iam::123456789012:root', 'Account': '123456789012'}
            with patch('boto3.Session', return_value=session), patch('tools.setup.approve') as approve, \
                    patch('builtins.input', return_value=''):
                with self.assertRaises(SetupPending):
                    wizard.identity()
            approve.assert_not_called()
            self.assertNotIn('MAVEN_AWS_EXPECTED_PRINCIPAL', wizard.values)
            cache = Path(wizard.values['MAVEN_AWS_LOGIN_CACHE'])
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(wizard.env.stat().st_mode & 0o777, 0o600)

    def test_saved_static_credentials_are_rejected_before_any_aws_request(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            wizard.settings({'MAVEN_AWS_REGION': 'us-east-1', 'MAVEN_AWS_PROFILE': 'selected'})
            session = Mock()
            session.get_credentials.return_value.method = 'shared-credentials-file'
            with patch('boto3.Session', return_value=session), patch('builtins.input', return_value=''):
                with self.assertRaises(SetupPending):
                    wizard.identity()
            session.client.assert_not_called()
            self.assertNotIn('MAVEN_AWS_EXPECTED_PRINCIPAL', wizard.values)

    def test_exact_review_required_before_apply_and_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            instance = str(uuid4())
            wizard.state.update(mode='new', prepared=True, instance_id=instance)
            wizard.settings({'MAVEN_STACK_NAME': 'selected-stack'})
            save_json(wizard.directory / 'change-set.json', {'review_sha256': 'exact-reviewed-digest'})
            manifest = {'deployment_inputs': {'InstanceId': instance}}
            with patch.object(wizard, 'region'), patch.object(wizard, 'identity', return_value=(Mock(), {})), \
                    patch('tools.memory.bootstrap._package', return_value=(manifest, '', [])), \
                    patch('tools.memory.bootstrap.plan') as plan, \
                    patch('tools.memory.bootstrap.apply') as apply, \
                    patch('tools.memory.bootstrap.initialize') as initialize, \
                    patch('builtins.input', return_value='some-other-digest'):
                with self.assertRaises(SetupPending):
                    wizard.fresh()
            plan.assert_not_called()
            apply.assert_not_called()
            initialize.assert_not_called()
            self.assertNotIn('applied', wizard.state)

    def test_unknown_apply_resumes_same_review_without_new_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            instance = str(uuid4())
            wizard.state.update(mode='new', prepared=True, instance_id=instance)
            wizard.save()
            wizard.settings({'MAVEN_STACK_NAME': 'selected-stack'})
            review = wizard.directory / 'change-set.json'
            save_json(review, {'review_sha256': 'unchanged-review'})
            before = review.read_bytes()
            manifest = {'deployment_inputs': {'InstanceId': instance}}
            with patch.object(Wizard, 'region'), patch.object(Wizard, 'identity', return_value=(Mock(), {})), \
                    patch('tools.memory.bootstrap._package', return_value=(manifest, '', [])), \
                    patch('tools.memory.bootstrap.plan') as plan, \
                    patch('tools.memory.bootstrap.apply', side_effect=MemoryClientError('stack_execution_outcome_unknown')) as apply, \
                    patch('tools.setup.approve'):
                for attempt in range(2):
                    current = wizard if attempt == 0 else self.wizard(directory)
                    with self.assertRaises(MemoryClientError):
                        current.fresh()
            plan.assert_not_called()
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(apply.call_args_list[0], apply.call_args_list[1])
            self.assertEqual(review.read_bytes(), before)
            self.assertNotIn('applied', read_private(wizard.state_path))

    def test_project_selection_is_optional_after_authority_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = self.wizard(directory)
            wizard.settings({'MAVEN_MEMORY_INSTANCE_ID': str(uuid4())})
            saved = {'root': str(Path(directory).resolve()), 'project_id': str(uuid4())}
            wizard.state['project'] = saved
            wizard.save()
            with patch.object(wizard, 'existing'), \
                    patch.object(wizard, 'verify_authority', return_value=7) as authority, \
                    patch.object(wizard, 'project') as project, \
                    patch('tools.setup.confirm', return_value=False), redirect_stdout(io.StringIO()):
                self.assertEqual(wizard.run(), 0)
            authority.assert_called_once_with()
            project.assert_not_called()
            state = read_private(wizard.state_path)
            self.assertEqual(state['project'], saved)
            self.assertNotIn('ready_project', state)


if __name__ == '__main__':
    unittest.main()
