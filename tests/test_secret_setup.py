"""Required secrets setup, ownership and recovery boundaries; offline AWS fixtures only."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from botocore.exceptions import ClientError

from tools import secret_setup as secrets
from tools.administration import AdminOperations
from tools.setup import Wizard
from tools.setup_common import SetupPending

ACCOUNT = '123456789012'
REGION = 'us-east-1'
SETTINGS = {
    'MAVEN_SECRETS_BUCKET': 'fixture-secret-files',
    'MAVEN_SECRETS_KEY_ID': '0123456789abcdef',
    'MAVEN_SECRETS_PARAMETER_PREFIX': '/fixture/values/',
    'MAVEN_SECRETS_FILE_KEY_PREFIX': '/fixture/keys/',
    'MAVEN_SECRETS_OBJECT_PREFIX': 'encrypted-files/',
}
VALUES = {**SETTINGS, 'MAVEN_ACCOUNT_ID': ACCOUNT, 'MAVEN_AWS_REGION': REGION,
          'MAVEN_AWS_EXPECTED_PRINCIPAL': f'arn:aws:iam::{ACCOUNT}:user/daily',
          'MAVEN_DEPLOY_EXPECTED_PRINCIPAL': f'arn:aws:iam::{ACCOUNT}:user/maintenance'}
NAME = SETTINGS['MAVEN_SECRETS_FILE_KEY_PREFIX'] + SETTINGS['MAVEN_SECRETS_KEY_ID']
METADATA = {'Name': NAME, 'Type': 'SecureString', 'KeyId': 'alias/aws/ssm', 'Version': 1, 'DataType': 'text'}
ACTUAL = {name: {'Account': ACCOUNT, 'Arn': VALUES[prefix + 'EXPECTED_PRINCIPAL']}
          for name, prefix in (('daily', 'MAVEN_AWS_'), ('maintenance', 'MAVEN_DEPLOY_'))}


def aws_error(code):
    return ClientError({'Error': {'Code': code, 'Message': 'must not be persisted'}}, 'Fixture')


class SecretSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / 'home'
        self.wizard = Wizard(self.home)
        self.wizard.settings(VALUES)
        self.controller = AdminOperations(self.home)
        self.clients = {name: Mock() for name in ('s3', 'ssm', 'cloudformation')}
        self.client_patch = patch('tools.secret_setup._client', side_effect=lambda session, name: self.clients[name])
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.parameter_fixture()
        s3 = self.clients['s3']
        s3.get_bucket_location.return_value = {'LocationConstraint': None}
        s3.get_public_access_block.return_value = {'PublicAccessBlockConfiguration': {
            key: True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}}
        s3.get_bucket_ownership_controls.return_value = {'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]}}
        s3.get_bucket_policy_status.return_value = {'PolicyStatus': {'IsPublic': False}}
        s3.get_bucket_encryption.return_value = {'ServerSideEncryptionConfiguration': {'Rules': [
            {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}}

    def parameter_fixture(self):
        self.clients['ssm'].get_parameter.side_effect = lambda **kwargs: {
            'Parameter': {**METADATA, 'Value': base64.b64encode(b'x' * 32).decode() if kwargs['WithDecryption'] else 'ciphertext'}}
        self.clients['ssm'].get_parameter_history.side_effect = lambda **kwargs: {
            'Parameters': [{**METADATA, 'Value': 'ciphertext'}]}

    def respond(self, result, response=None):
        question = result['pending_interaction']
        if question['kind'] == 'review':
            response = {'approved': True, 'review_digest': question['review_digest'],
                        'source': {'origin': 'offline owner fixture', 'excerpt': 'Approve exact review.'}}
        return self.controller.respond(result['operation_id'], question['interaction_id'], question['revision'], response)

    def to_review(self, kind='repair'):
        result = self.controller.begin(kind, {})
        result = self.respond(result, {'mode': 'saved'})
        result = self.controller.advance(result['operation_id'])
        result = self.controller.advance(result['operation_id'])
        result = self.respond(result, {'account': ACCOUNT, 'region': REGION, 'daily_profile': 'daily',
            'daily_cache': str(self.home / 'runtime/daily'), 'maintenance_profile': 'maintenance',
            'maintenance_cache': str(self.home / 'runtime/maintenance')})
        return self.controller.advance(result['operation_id'])

    def identity_patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch('tools.secret_setup.inspect_login_identity',
            side_effect=lambda values, deploy=False: ACTUAL['maintenance' if deploy else 'daily']))
        stack.enter_context(patch('tools.secret_setup.identities', return_value=(ACTUAL, {'daily': Mock(), 'maintenance': Mock()})))
        return stack

    def test_all_five_settings_required_and_prefixes_are_nonoverlapping_namespaces(self):
        self.assertEqual(secrets.validate_settings(VALUES), SETTINGS)
        for key in SETTINGS:
            with self.subTest(key=key), self.assertRaises(SetupPending):
                secrets.validate_settings({name: value for name, value in SETTINGS.items() if name != key})
        for key, value in (
                ('MAVEN_SECRETS_FILE_KEY_PREFIX', '/fixture/values/keys/'),
                ('MAVEN_SECRETS_PARAMETER_PREFIX', '/aws/values/'),
                ('MAVEN_SECRETS_OBJECT_PREFIX', '/absolute/'),
                ('MAVEN_SECRETS_OBJECT_PREFIX', '../relative/'),
                ('MAVEN_SECRETS_KEY_ID', 'not-a-key'), ('MAVEN_SECRETS_BUCKET', '127.0.0.1')):
            with self.subTest(key=key, value=value), self.assertRaises(SetupPending):
                secrets.validate_settings({**SETTINGS, key: value})

    def test_import_merges_only_integration_locators_not_host_login_or_memory(self):
        path = Path(self.temporary.name) / 'source.env'
        imported = {**VALUES, 'MAVEN_AWS_PROFILE': 'other-host', 'MAVEN_AWS_LOGIN_CACHE': '/other/private/cache',
                    'MAVEN_MEMORY_ENDPOINT': 'https://other.example', 'MAVEN_SECRETS_OBJECT_PREFIX': 'changed/'}
        path.write_text(''.join(key + '=' + json.dumps(value) + '\n' for key, value in imported.items()))
        path.chmod(0o600)
        state = {'answers': {'secrets_source': {'mode': 'import'}, 'secrets_details': {'path': str(path)}}}
        settings, source = secrets._selection(self.wizard, state)
        self.assertEqual(settings, {**SETTINGS, 'MAVEN_SECRETS_OBJECT_PREFIX': 'changed/'})
        self.assertEqual(source, {'origin': str(path)})
        self.assertEqual(self.wizard.values, VALUES)

    def test_policies_use_exact_resources_and_no_deletion_or_iam_grants(self):
        for policy in (secrets.required_policy(VALUES), secrets.maintenance_policy(VALUES, str(uuid4()), create=True)):
            for statement in policy['Statement']:
                if statement['Resource'] == '*':
                    self.assertEqual(statement['Action'], ['ssm:DescribeParameters'])
            text = json.dumps(policy)
            self.assertNotIn('iam:', text)
            self.assertNotIn('Delete', text)
        policy = secrets.required_policy(VALUES)
        self.assertEqual(policy['Statement'][1]['Resource'], f'arn:aws:ssm:{REGION}:{ACCOUNT}:parameter' + NAME)
        self.assertEqual(policy['Statement'][2]['Condition']['StringLike']['s3:prefix'], 'encrypted-files/*')
        self.assertIn({'Effect': 'Allow', 'Action': ['ssm:DescribeParameters'], 'Resource': '*'},
                      policy['Statement'])

    def test_metadata_checks_account_region_privacy_encryption_and_never_returns_values(self):
        metadata = secrets.inspect_store(VALUES, Mock())
        self.assertEqual(metadata['wrapping_parameter'], METADATA)
        self.assertNotIn('ciphertext', json.dumps(metadata))
        self.assertNotIn('Value', metadata['wrapping_parameter'])
        self.clients['s3'].head_bucket.assert_called_once_with(Bucket=SETTINGS['MAVEN_SECRETS_BUCKET'], ExpectedBucketOwner=ACCOUNT)
        self.clients['ssm'].get_parameter_history.assert_called_once_with(Name=NAME, WithDecryption=False, MaxResults=50)
        for method, response in (
                ('get_bucket_location', {'LocationConstraint': 'us-west-2'}),
                ('get_public_access_block', {'PublicAccessBlockConfiguration': {}}),
                ('get_bucket_ownership_controls', {'OwnershipControls': {'Rules': []}}),
                ('get_bucket_policy_status', {'PolicyStatus': {'IsPublic': True}})):
            original = getattr(self.clients['s3'], method).return_value
            getattr(self.clients['s3'], method).return_value = response
            with self.subTest(method=method), self.assertRaises(SetupPending):
                secrets.inspect_store(VALUES, Mock())
            getattr(self.clients['s3'], method).return_value = original

    def test_use_verification_is_read_only_and_does_not_claim_write_access(self):
        result = secrets.verify_use(VALUES, Mock(), {'wrapping_parameter': METADATA})
        self.assertTrue(result['wrapping_key_decryption'])
        self.assertFalse(result['write_access_verified'])
        self.assertNotIn(base64.b64encode(b'x' * 32).decode(), json.dumps(result))
        self.clients['ssm'].put_parameter.assert_not_called()
        self.clients['ssm'].delete_parameter.assert_not_called()
        self.clients['s3'].put_object.assert_not_called()
        self.clients['s3'].get_object.assert_not_called()
        self.clients['s3'].delete_object.assert_not_called()

    def test_invalid_wrapping_value_is_safe_and_never_echoed(self):
        self.clients['ssm'].get_parameter.side_effect = None
        self.clients['ssm'].get_parameter.return_value = {'Parameter': {'Type': 'SecureString', 'Value': 'sensitive-invalid-value'}}
        with self.assertRaises(SetupPending) as caught:
            secrets.verify_use(VALUES, Mock(), {'wrapping_parameter': METADATA})
        self.assertNotIn('sensitive-invalid-value', str(caught.exception))

    def test_partial_existing_binding_offers_repair_not_creation(self):
        empty = Wizard(Path(self.temporary.name) / 'partial')
        empty.settings({'MAVEN_SECRETS_BUCKET': SETTINGS['MAVEN_SECRETS_BUCKET']})
        result = AdminOperations(empty.home).begin('secrets_setup', {})
        choices = result['pending_interaction']['fields'][0]['choices']
        self.assertNotIn('create', [item[1] for item in choices])
        self.assertIn('manual', [item[1] for item in choices])

    def test_missing_all_existing_locators_restores_exact_receipt_suggestions_not_creation(self):
        from tools.setup_common import atomic_private
        with self.identity_patches():
            result = self.to_review()
            result = self.controller.advance(self.respond(result)['operation_id'])
        self.assertEqual(result['state'], 'completed')
        wizard = Wizard(self.home)
        remaining = {key: value for key, value in wizard.values.items() if key not in secrets.SECRETS_KEYS}
        atomic_private(wizard.env, ''.join(key + '=' + json.dumps(value) + '\n' for key, value in remaining.items()))
        repair = self.controller.begin('repair', {})
        choices = repair['pending_interaction']['fields'][0]['choices']
        self.assertNotIn('create', [item[1] for item in choices])
        repair = self.respond(repair, {'mode': 'manual'})
        repair = self.controller.advance(repair['operation_id'])
        self.assertEqual({field['name']: field['default'] for field in repair['pending_interaction']['fields']}, SETTINGS)
        self.assertFalse(secrets.configured(Wizard(self.home).values))

    def test_unfinished_standalone_operation_cannot_be_replaced_by_another(self):
        first = self.controller.begin('secrets_setup', {})
        second = self.controller.begin('repair', {})
        self.assertEqual(second['state'], 'blocked')
        self.assertIn(first['operation_id'], second['error'])
        self.assertEqual(Wizard(self.home).state['secrets_operation'], first['operation_id'])

    def test_selected_identities_must_match_account_without_fallback(self):
        wrong = {**ACTUAL['maintenance'], 'Account': '999999999999',
                 'Arn': 'arn:aws:iam::999999999999:user/maintenance'}
        with patch('tools.secret_setup.inspect_login_identity', side_effect=[ACTUAL['daily'], wrong]), \
                patch('tools.memory.environment.selected_aws_session') as selected:
            with self.assertRaises(SetupPending):
                secrets.identities(VALUES)
        selected.assert_called_once_with(VALUES, deploy=False, region=REGION)

    def test_read_only_failure_allows_revising_choices_without_discarding_approval(self):
        with self.identity_patches():
            result = self.to_review()
            result = self.respond(result)
            self.clients['s3'].get_bucket_location.side_effect = aws_error('AccessDenied')
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('back', result['next_actions'])
        self.assertEqual(len(result['approvals']), 1)
        self.clients['ssm'].put_parameter.assert_not_called()
        self.clients['s3'].put_object.assert_not_called()

    def test_repair_requires_exact_owner_review_then_read_only_verification(self):
        with self.identity_patches(), patch('tools.secret_setup.provision') as provision:
            result = self.to_review()
            self.assertEqual(result['pending_interaction']['key'], 'secrets_review')
            self.assertEqual(result['pending_interaction']['review']['previous_settings'], SETTINGS)
            self.assertEqual(result['pending_interaction']['review']['policy_principal'], ACTUAL['daily']['Arn'])
            self.clients['ssm'].get_parameter.assert_called_with(Name=NAME, WithDecryption=False)
            self.assertNotIn('secrets_ready', result)
            result = self.respond(result)
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(result['outcome']['secrets_ready'])
        self.assertFalse(result['outcome']['access']['write_access_verified'])
        provision.assert_not_called()
        journal = self.controller._path(result['operation_id']).read_text()
        self.assertNotIn(base64.b64encode(b'x' * 32).decode(), journal)
        self.assertNotIn('ciphertext', journal)
        self.clients['ssm'].put_parameter.assert_not_called()
        self.clients['s3'].put_object.assert_not_called()

    def test_missing_metadata_permission_returns_exact_external_policy_handoff(self):
        self.clients['s3'].get_bucket_location.side_effect = aws_error('AccessDenied')
        with self.identity_patches():
            result = self.to_review()
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(result['handoff']['completion_claimed'])
        self.assertEqual(result['handoff']['principal'], ACTUAL['maintenance']['Arn'])
        self.assertTrue(all(item['Resource'] != '*' for item in result['handoff']['required_policy']['Statement']))
        self.assertNotIn('must not be persisted', json.dumps(result))

    def test_setup_verify_cannot_bypass_required_stage(self):
        result = self.controller.begin('setup', {'mode': 'existing'})
        state = self.controller._load(result['operation_id'])
        state.update(step='verify', state='pending', pending_interaction=None)
        self.controller._save(state)
        with patch.object(Wizard, 'verify_authority') as authority:
            result = self.controller.advance(result['operation_id'])
        self.assertEqual(result['step'], 'secrets_start')
        self.assertNotEqual(result['state'], 'completed')
        authority.assert_not_called()

    def create_fixture(self):
        operation = str(uuid4())
        stack_name = 'maven-secrets-' + operation
        stack_id = f'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{stack_name}/original'
        cloud, ssm, s3 = (self.clients[name] for name in ('cloudformation', 'ssm', 's3'))
        cloud.create_stack.return_value = {'StackId': stack_id}
        cloud.describe_stacks.return_value = {'Stacks': [{'StackId': stack_id, 'StackStatus': 'CREATE_COMPLETE',
            'Tags': [{'Key': secrets.TAG, 'Value': operation}]}]}
        cloud.get_template.return_value = {'TemplateBody': secrets.bucket_template(SETTINGS, operation)}
        cloud.describe_stack_resource.return_value = {'StackResourceDetail': {
            'PhysicalResourceId': SETTINGS['MAVEN_SECRETS_BUCKET'], 'ResourceType': 'AWS::S3::Bucket'}}
        s3.head_bucket.side_effect = aws_error('404')
        calls = []
        def get_parameter(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise aws_error('ParameterNotFound')
            return {'Parameter': {**METADATA, 'Value': 'ciphertext'}}
        ssm.get_parameter.side_effect = get_parameter
        ssm.put_parameter.return_value = {'Version': 1}
        ssm.list_tags_for_resource.return_value = {'TagList': [{'Key': secrets.TAG, 'Value': operation}]}
        state, saved = {'operation_id': operation}, []
        return state, saved, lambda: saved.append(deepcopy(state))

    def test_creation_uses_retained_private_stack_and_one_unpersisted_random_key(self):
        state, saved, persist = self.create_fixture()
        with patch('tools.secret_setup.token_bytes', return_value=b'k' * 32) as random:
            result = secrets.provision(VALUES, Mock(), state, persist)
        self.assertTrue(result['retained_independently'])
        random.assert_called_once_with(32)
        create = self.clients['cloudformation'].create_stack.call_args.kwargs
        self.assertEqual(create['ClientRequestToken'], state['operation_id'])
        resource = json.loads(create['TemplateBody'])['Resources']['SecretFiles']
        self.assertEqual(resource['DeletionPolicy'], 'Retain')
        self.assertEqual(resource['UpdateReplacePolicy'], 'Retain')
        put = self.clients['ssm'].put_parameter.call_args.kwargs
        self.assertFalse(put['Overwrite'])
        self.assertEqual(put['Type'], 'SecureString')
        self.assertEqual(base64.b64decode(put['Value']), b'k' * 32)
        self.assertNotIn(put['Value'], json.dumps(saved))
        self.assertTrue(any(item['secrets_resources'].get('key_submitted') for item in saved))

    def test_definitive_key_access_denial_can_retry_after_external_grant(self):
        state, saved, persist = self.create_fixture()
        self.clients['ssm'].put_parameter.side_effect = [aws_error('AccessDeniedException'), {'Version': 1}]
        self.clients['ssm'].get_parameter.side_effect = [
            aws_error('ParameterNotFound'), aws_error('ParameterNotFound'),
            {'Parameter': {**METADATA, 'Value': 'ciphertext'}}]
        with patch('tools.secret_setup.token_bytes', side_effect=[b'a' * 32, b'b' * 32]) as random:
            with self.assertRaises(ClientError):
                secrets.provision(VALUES, Mock(), state, persist)
            self.assertEqual(state['secrets_resources']['key_rejected'], 'AccessDeniedException')
            result = secrets.provision(VALUES, Mock(), state, persist)
        self.assertEqual(random.call_count, 2)
        self.assertEqual(result['key_version'], 1)
        self.assertNotIn('key_rejected', result)
        self.assertNotIn(base64.b64encode(b'a' * 32).decode(), json.dumps(saved))
        self.assertNotIn(base64.b64encode(b'b' * 32).decode(), json.dumps(saved))
        self.clients['cloudformation'].create_stack.assert_called_once()

    def test_definitive_stack_access_denial_retries_same_original_request(self):
        state, saved, persist = self.create_fixture()
        cloud = self.clients['cloudformation']
        cloud.create_stack.side_effect = [aws_error('AccessDenied'), cloud.create_stack.return_value]
        self.clients['ssm'].get_parameter.side_effect = [
            aws_error('ParameterNotFound'), aws_error('ParameterNotFound'),
            {'Parameter': {**METADATA, 'Value': 'ciphertext'}}]
        with patch('tools.secret_setup.token_bytes', return_value=b'k' * 32) as random:
            with self.assertRaises(ClientError):
                secrets.provision(VALUES, Mock(), state, persist)
            result = secrets.provision(VALUES, Mock(), state, persist)
        self.assertEqual(cloud.create_stack.call_args_list[0], cloud.create_stack.call_args_list[1])
        self.assertTrue(result['retained_independently'])
        random.assert_called_once()

    def test_uncertain_key_write_reconciles_exact_ownership_without_regeneration(self):
        state, saved, persist = self.create_fixture()
        self.clients['ssm'].put_parameter.side_effect = TimeoutError('unknown original outcome')
        with patch('tools.secret_setup.token_bytes', return_value=b'k' * 32) as random:
            with self.assertRaises(TimeoutError):
                secrets.provision(VALUES, Mock(), state, persist)
            result = secrets.provision(VALUES, Mock(), state, persist)
        random.assert_called_once_with(32)
        self.clients['ssm'].put_parameter.assert_called_once()
        self.clients['cloudformation'].create_stack.assert_called_once()
        self.assertEqual(result['key_version'], 1)

    def test_uncertain_missing_key_is_not_regenerated_and_wrong_ownership_is_refused(self):
        state, saved, persist = self.create_fixture()
        self.clients['ssm'].put_parameter.side_effect = TimeoutError()
        with patch('tools.secret_setup.token_bytes', return_value=b'k' * 32) as random:
            with self.assertRaises(TimeoutError):
                secrets.provision(VALUES, Mock(), state, persist)
            self.clients['ssm'].get_parameter.side_effect = aws_error('ParameterNotFound')
            with self.assertRaisesRegex(SetupPending, 'uncertain original write'):
                secrets.provision(VALUES, Mock(), state, persist)
            self.parameter_fixture()
            self.clients['ssm'].list_tags_for_resource.return_value = {'TagList': []}
            with self.assertRaisesRegex(SetupPending, 'ownership/version'):
                secrets.provision(VALUES, Mock(), state, persist)
        random.assert_called_once()
        self.clients['ssm'].put_parameter.assert_called_once()

    def test_existing_resource_collision_never_adopts_or_generates_key(self):
        state, saved, persist = self.create_fixture()
        self.clients['s3'].head_bucket.side_effect = None
        with patch('tools.secret_setup.token_bytes') as random, self.assertRaisesRegex(SetupPending, 'cannot adopt'):
            secrets.provision(VALUES, Mock(), state, persist)
        random.assert_not_called()
        self.clients['cloudformation'].create_stack.assert_not_called()
        self.assertFalse(saved)

    def test_cloud_binding_is_exact_sourced_reference_not_name_search_or_home_requirement(self):
        identifier = str(uuid4())
        client = Mock()
        client.invoke.return_value = {'data': {'record_id': identifier, 'revision': 4, 'kind': 'setting',
            'data_scope': 'owner', 'source': {'origin': 'owner store review', 'excerpt': 'Selected this exact store.'},
            'data': {'value': {'type': 'maven-secrets-binding', 'account': ACCOUNT, 'region': REGION, 'settings': SETTINGS}}}}
        with patch('tools.memory.environment.configured_client', return_value=client):
            settings, provenance = secrets._cloud_binding(self.wizard, identifier)
        self.assertEqual(settings, SETTINGS)
        self.assertEqual(provenance['revision'], 4)
        client.invoke.assert_called_once_with('/v1/memory/read', {'record_id': identifier})
        client.close.assert_called_once()
        self.assertFalse((self.home / 'home.json').exists())

    def test_start_over_preserves_submitted_store_locators_before_binding(self):
        from tools.setup_restart import _retained_integrations
        operation = {'secrets_resources': {'key_submitted': True},
                     'secrets_candidate': {'settings': {**SETTINGS, 'MAVEN_SECRETS_BUCKET': 'submitted-new-store'}}}
        retained = _retained_integrations(self.wizard, operation)
        self.assertEqual(retained['MAVEN_SECRETS_BUCKET'], 'submitted-new-store')
        self.assertNotIn('MAVEN_AWS_EXPECTED_PRINCIPAL', retained)


if __name__ == '__main__':
    unittest.main()
