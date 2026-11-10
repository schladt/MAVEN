"""Consumer-visible configuration and uncertain-write behavior, no AWS calls."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.memory.client import MemoryClient, MemoryClientError, read_json, selected_session, validate_config
from tools.memory import cli


CONFIG = {
    'region': 'us-east-1', 'database': 'maven', 'schema_version': 2,
    'instance_id': '00000000-0000-4000-8000-000000000001',
    'recovery_epoch': '00000000-0000-4000-8000-000000000002',
    'endpoint': 'https://example123.execute-api.us-east-1.amazonaws.com',
    'client_role_arn': 'arn:aws:iam::123456789012:role/memory-client',
    'admin_role_arn': 'arn:aws:iam::123456789012:role/memory-admin',
    'approval_role_arn': 'arn:aws:iam::123456789012:role/memory-approval',
    'cluster_arn': 'arn:aws:rds:us-east-1:123456789012:cluster:memory',
    'app_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:memory-app-example',
    'admin_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:memory-admin-example',
    'journal_bucket': 'synthetic-memory-recovery',
    'skill_bucket': 'synthetic-memory-skills',
}
KEY = '00000000-0000-4000-8000-000000000003'


class MemoryClientBoundaries(unittest.TestCase):
    def test_configuration_cannot_redirect_signed_requests_or_cross_accounts(self):
        for changed in [
            {'endpoint': 'https://example.invalid'},
            {'endpoint': CONFIG['endpoint'] + '/redirect'},
            {'endpoint': CONFIG['endpoint'] + '@example.invalid'},
            {'admin_role_arn': 'arn:aws:iam::999999999999:role/memory-admin'},
            {'admin_role_arn': CONFIG['client_role_arn']},
            {'approval_role_arn': CONFIG['client_role_arn']},
            {'approval_role_arn': CONFIG['admin_role_arn']},
            {'approval_role_arn': 'arn:aws:iam::999999999999:role/memory-approval'},
            {'admin_secret_arn': CONFIG['app_secret_arn']},
            {'region': 'us-west-2'},
            {'schema_version': True},
        ]:
            with self.subTest(changed=changed), self.assertRaises(MemoryClientError):
                validate_config({**CONFIG, **changed})

    def test_skill_storage_is_optional_but_never_accepts_unknown_configuration(self):
        from tools.memory.environment import memory_config
        legacy = {key: value for key, value in CONFIG.items() if key != 'skill_bucket'}
        self.assertEqual(validate_config(legacy), legacy)
        self.assertEqual(validate_config(CONFIG), CONFIG)
        values = {'MAVEN_MEMORY_' + key.upper(): str(value) for key, value in legacy.items()}
        self.assertEqual(memory_config(values), legacy)
        values['MAVEN_MEMORY_SKILL_BUCKET'] = CONFIG['skill_bucket']
        self.assertEqual(memory_config(values), CONFIG)
        for changed in ({'skill_bucket': ''}, {'skill_bucket': None},
                        {'skill_bucket': 'https://not-a-bucket'}, {'unknown': 'value'}):
            with self.subTest(changed=changed), self.assertRaises(MemoryClientError):
                validate_config({**legacy, **changed})

    def test_ambiguous_or_nonfinite_json_never_becomes_a_write_request(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'request.json'
            for value in ['{"value":1,"value":2}', '{"value":NaN}', '{"value":Infinity}']:
                path.write_text(value)
                with self.assertRaisesRegex(MemoryClientError, 'invalid_json_file'):
                    read_json(path)

    def test_lost_write_and_missing_receipt_remain_unknown_not_failed(self):
        client = MemoryClient(CONFIG, None)
        self.addCleanup(client.close)
        with patch.object(client, '_post', side_effect=[MemoryClientError('transport_unavailable'),
                                                       (404, {'status': 'operation_unavailable'})]), \
             patch('tools.memory.client.time.sleep'):
            with self.assertRaises(MemoryClientError) as failure:
                client.invoke('/v1/records/change', {'request_key': KEY})
        self.assertEqual(failure.exception.code, 'outcome_unknown')
        self.assertEqual(failure.exception.request_key, KEY)

    def test_server_failure_after_commit_is_reconciled_without_reissuing_change(self):
        client = MemoryClient(CONFIG, None)
        self.addCleanup(client.close)
        committed = False
        dispatched = 0
        def provider(route, body, remaining):
            nonlocal committed, dispatched
            if route == '/v1/records/change':
                dispatched += 1
                if committed:
                    self.fail('uncertain mutation was reissued instead of reconciled')
                committed = True
                return 500, {'status': 'untrusted provider error body'}
            self.assertTrue(committed)
            return 200, {'status': 'ready', 'data': {'request_key': KEY, 'revision': 1}}
        with patch.object(client, '_post', side_effect=provider), patch('tools.memory.client.time.sleep'):
            result = client.invoke('/v1/records/change', {'request_key': KEY})
        self.assertEqual(result['data']['revision'], 1)
        self.assertEqual(dispatched, 1)

    def test_unknown_provider_error_content_is_not_returned(self):
        client = MemoryClient(CONFIG, None)
        self.addCleanup(client.close)
        with patch.object(client, '_post', return_value=(400, {'status': 'private provider details'})):
            with self.assertRaises(MemoryClientError) as failure:
                client.invoke('/v1/records/query', {'record_id': KEY})
        self.assertEqual(str(failure.exception), 'service_failure')

    def test_definitive_memory_rejections_preserve_the_request_key_without_retry(self):
        client = MemoryClient(CONFIG, None)
        self.addCleanup(client.close)
        for status in ('setting_conflict', 'reference_unavailable', 'session_unavailable'):
            with self.subTest(status=status), \
                    patch.object(client, '_post', return_value=(409, {'status': status})) as post:
                with self.assertRaises(MemoryClientError) as failure:
                    client.invoke('/v1/memory/write', {'request_key': KEY})
                self.assertEqual((failure.exception.code, failure.exception.request_key), (status, KEY))
                post.assert_called_once()


class RefreshableSessionBoundaries(unittest.TestCase):
    def setUp(self):
        import boto3
        self.real_session = boto3.Session
        self.clock = datetime(2030, 1, 1, tzinfo=timezone.utc)
        clock = patch('tools.memory.client._credential_time', side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.values = {
            'MAVEN_AWS_PROFILE': 'selected-profile',
            'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/selected',
            'MAVEN_AWS_LOGIN_CACHE': self.directory.name,
        }
        self.source = MagicMock()
        self.sts = self.source.client.return_value
        self.sts.get_caller_identity.return_value = {'Arn': self.values['MAVEN_AWS_EXPECTED_PRINCIPAL']}
        self.sts.assume_role.side_effect = lambda **kwargs: self.temporary(
            f'synthetic-{self.sts.assume_role.call_count}', self.clock + timedelta(hours=1))
        factory = patch('tools.memory.environment.boto3.Session', side_effect=self.session_factory)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    @staticmethod
    def temporary(key, expiration):
        return {'Credentials': {'AccessKeyId': key, 'SecretAccessKey': 'synthetic-secret',
                                'SessionToken': 'synthetic-token', 'Expiration': expiration}}

    def session_factory(self, **kwargs):
        if 'botocore_session' in kwargs:
            return self.real_session(**kwargs)
        self.assertEqual(kwargs, {'profile_name': 'selected-profile', 'region_name': CONFIG['region']})
        return self.source

    def test_expiring_credentials_refresh_through_the_original_selected_identity_and_role(self):
        for purpose in ('client', 'admin', 'approval'):
            with self.subTest(purpose=purpose):
                config, values = dict(CONFIG), dict(self.values)
                session = selected_session(config, admin=purpose == 'admin',
                                           approval=purpose == 'approval', environment=values)
                credentials = session.get_credentials()
                first = credentials.get_frozen_credentials().access_key
                config[purpose + '_role_arn'] = 'arn:aws:iam::999999999999:role/changed'
                values.update(MAVEN_AWS_PROFILE='changed', MAVEN_AWS_EXPECTED_PRINCIPAL='changed')
                validations = self.sts.get_caller_identity.call_count
                assumptions = self.sts.assume_role.call_count
                closes = self.sts.close.call_count
                self.clock += timedelta(minutes=46)
                renewed = credentials.get_frozen_credentials().access_key
                self.assertNotEqual(first, renewed)
                self.assertEqual(self.sts.get_caller_identity.call_count, validations + 1)
                self.assertEqual(self.sts.assume_role.call_count, assumptions + 1)
                self.assertEqual(self.sts.close.call_count, closes + 2)
                self.sts.assume_role.assert_called_with(
                    RoleArn=CONFIG[purpose + '_role_arn'],
                    RoleSessionName='maven-memory-' + purpose, DurationSeconds=3600)
                self.clock += timedelta(minutes=16)
                self.assertEqual(credentials.get_frozen_credentials().access_key, renewed)
                self.assertEqual(self.sts.assume_role.call_count, assumptions + 1)

    def test_identity_change_rejects_early_refresh_and_never_signs_with_expired_credentials(self):
        session = selected_session(CONFIG, environment=self.values)
        credentials = session.get_credentials()
        self.assertEqual(credentials.get_frozen_credentials().access_key, 'synthetic-1')
        self.sts.get_caller_identity.return_value = {'Arn': 'arn:aws:iam::123456789012:root'}
        self.clock += timedelta(minutes=46)
        with self.assertRaisesRegex(MemoryClientError, '^unexpected_login_identity$'):
            credentials.get_frozen_credentials()
        self.clock += timedelta(minutes=16)
        client = MemoryClient(CONFIG, session)
        self.addCleanup(client.close)
        with patch.object(client.http, 'post') as post:
            with self.assertRaisesRegex(MemoryClientError, '^unexpected_login_identity$'):
                client._post('/v1/context/load', {}, 10)
            post.assert_not_called()
        self.assertEqual(self.sts.assume_role.call_count, 1)
        self.assertEqual(self.sts.close.call_count, 4)

    def test_failed_or_expired_sts_refresh_is_sanitized_and_closed_without_sending(self):
        from botocore.exceptions import ClientError
        for mode in ('denied', 'expired'):
            with self.subTest(mode=mode):
                self.sts.assume_role.side_effect = None
                self.sts.assume_role.return_value = self.temporary('original', self.clock + timedelta(hours=1))
                session = selected_session(CONFIG, environment=self.values)
                client = MemoryClient(CONFIG, session)
                self.addCleanup(client.close)
                self.clock += timedelta(hours=2)
                if mode == 'denied':
                    self.sts.assume_role.side_effect = ClientError(
                        {'Error': {'Code': 'AccessDenied', 'Message': 'PRIVATE PROVIDER DETAILS'}}, 'AssumeRole')
                else:
                    self.sts.assume_role.return_value = self.temporary('expired', self.clock - timedelta(seconds=1))
                closes = self.sts.close.call_count
                with patch.object(client.http, 'post') as post:
                    with self.assertRaisesRegex(MemoryClientError, '^selected_login_or_role_unavailable$'):
                        client._post('/v1/context/load', {}, 10)
                    post.assert_not_called()
                self.assertEqual(self.sts.close.call_count, closes + 2)


class InstructionClientBoundaries(unittest.TestCase):
    def setUp(self):
        self.client = MemoryClient(CONFIG, None)
        self.addCleanup(self.client.close)

    @staticmethod
    def page(body='synthetic instruction', *, offset=0, next_offset=None, full=None, **changes):
        full = body if full is None else full
        candidate = {'slug': 'synthetic-skill', 'page_kind': 'skill', 'title': 'Synthetic skill',
                     'scope': {}, 'authority_state': 'active', 'body_markdown': full}
        from tools.memory.validation import digest
        data = {
            'page_id': KEY, 'proposal_id': KEY, 'slug': 'synthetic-skill', 'page_kind': 'skill',
            'title': 'Synthetic skill', 'scope': {}, 'authority_state': 'active',
            'revision': 1, 'content_hash': digest(candidate), 'body_markdown': body,
            'candidate_authority_state': 'active',
            'offset': offset, 'next_offset': next_offset, 'total_characters': len(full),
            'body_sha256': hashlib.sha256(full.encode()).hexdigest(),
            'source': {'origin': 'synthetic fixture', 'excerpt': ''},
            'authority_generation': 1, 'operative': True,
        }
        return {'status': 'ready', 'data': {**data, **changes}}

    def test_new_configuration_requires_explicit_approval_role(self):
        old = dict(CONFIG)
        del old['approval_role_arn']
        with self.assertRaisesRegex(MemoryClientError, 'invalid_configuration'):
            validate_config(old)

    def test_role_selection_is_separate_and_conflicts_fail_before_login(self):
        with patch('tools.memory.environment.selected_aws_session') as login:
            with self.assertRaisesRegex(MemoryClientError, 'invalid_role_selection'):
                selected_session(CONFIG, admin=True, approval=True)
            login.assert_not_called()
        source = MagicMock()
        sts = source.client.return_value
        sts.assume_role.return_value = {'Credentials': {'AccessKeyId': 'synthetic', 'SecretAccessKey': 'synthetic',
                                                       'SessionToken': 'synthetic',
                                                       'Expiration': datetime.now(timezone.utc) + timedelta(hours=1)}}
        with patch('tools.memory.environment.selected_aws_session', return_value=source), patch('tools.memory.client.boto3.Session'):
            selected_session(CONFIG, approval=True, environment={})
        sts.assume_role.assert_called_once_with(RoleArn=CONFIG['approval_role_arn'],
                                               RoleSessionName='maven-memory-approval', DurationSeconds=3600)

    def test_all_keyed_instruction_writes_reconcile_without_resubmission(self):
        for route in ('/v1/proposals/create', '/v1/proposals/resolve'):
            for failure in (MemoryClientError('transport_unavailable'), (500, {'status': 'private failure'})):
                with self.subTest(route=route, failure=type(failure).__name__):
                    receipt = {'status': 'ready', 'data': {'request_key': KEY, 'revision': 2}}
                    with patch.object(self.client, '_post', side_effect=[failure, (200, receipt)]) as post, \
                         patch('tools.memory.client.time.sleep'):
                        self.assertEqual(self.client.invoke(route, {'request_key': KEY}), receipt)
                    self.assertEqual([call.args[:2] for call in post.call_args_list],
                                     [(route, {'request_key': KEY}), ('/v1/operations/lookup', {'request_key': KEY})])

    def test_lost_activation_retains_original_key_when_receipt_missing(self):
        with patch.object(self.client, '_post', side_effect=[MemoryClientError('transport_unavailable'),
                                                            (404, {'status': 'operation_unavailable'})]), \
             patch('tools.memory.client.time.sleep'):
            with self.assertRaises(MemoryClientError) as failure:
                self.client.invoke('/v1/proposals/resolve', {'request_key': KEY})
        self.assertEqual(failure.exception.code, 'outcome_unknown')
        self.assertEqual(failure.exception.request_key, KEY)

    def test_instruction_domain_errors_are_sanitized_and_preserved(self):
        for code in ('proposal_unavailable', 'page_unavailable', 'proposal_conflict', 'approval_invalid',
                     'revision_conflict', 'idempotency_conflict'):
            with self.subTest(code=code), patch.object(self.client, '_post', return_value=(409, {'status': code})):
                with self.assertRaises(MemoryClientError) as failure:
                    self.client.invoke('/v1/proposals/resolve', {'request_key': KEY})
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(failure.exception.request_key, KEY)

    def test_full_unicode_body_pins_current_revision_without_mutating_selector(self):
        full = 'é' * 4096 + '\ncomplete'
        responses = [self.page(full[:4096], next_offset=4096, full=full),
                     self.page(full[4096:], offset=4096, full=full, authority_generation=2)]
        selector = {'slug': 'synthetic-skill'}
        with patch.object(self.client, 'invoke', side_effect=responses) as invoke:
            result = self.client.read_full(selector)
        self.assertEqual(result['data']['body_markdown'], full)
        self.assertIsNone(result['data']['next_offset'])
        self.assertEqual(selector, {'slug': 'synthetic-skill'})
        self.assertEqual(invoke.call_args_list[1].args,
                         ('/v1/pages/read', {'slug': 'synthetic-skill', 'offset': 4096, 'expected_revision': 1}))

    def test_historical_and_draft_reads_remain_nonoperative_without_current_pin(self):
        for selector, metadata in [
            ({'slug': 'synthetic-skill', 'revision': 1}, {'operative': False}),
            ({'proposal_id': KEY}, {'proposal_id': KEY, 'revision': 0, 'authority_state': 'draft', 'operative': False}),
        ]:
            with self.subTest(selector=selector):
                responses = [self.page('a', next_offset=1, full='ab', **metadata),
                             self.page('b', offset=1, full='ab', **metadata)]
                with patch.object(self.client, 'invoke', side_effect=responses) as invoke:
                    result = self.client.read_full(selector)
                self.assertFalse(result['data']['operative'])
                self.assertEqual(invoke.call_args_list[1].args[1], {**selector, 'offset': 1})

    def test_full_read_rejects_truncation_bad_offsets_hashes_and_oversized_chunks(self):
        for response in [
            self.page('a', full='ab'),
            self.page('a', next_offset=0, full='ab'),
            self.page('a', next_offset=True, full='ab'),
            self.page('a', offset=1),
            self.page('a', body_sha256='0' * 64),
            self.page('a', total_characters=True),
            self.page('x' * 4097),
            self.page('a', slug='different'),
            self.page('a', candidate_authority_state='revoked'),
            self.page('a', content_hash='0' * 64),
        ]:
            with self.subTest(data=response['data']), patch.object(self.client, 'invoke', return_value=response):
                with self.assertRaisesRegex(MemoryClientError, 'invalid_service_response'):
                    self.client.read_full({'slug': 'synthetic-skill'})

    def test_full_read_rejects_changed_revision_or_metadata(self):
        for change, code in [({'revision': 2}, 'revision_conflict'),
                             ({'content_hash': 'b' * 64}, 'invalid_service_response'),
                             ({'source': {'origin': 'changed', 'excerpt': ''}}, 'invalid_service_response')]:
            responses = [self.page('a', next_offset=1, full='ab'), self.page('b', offset=1, full='ab', **change)]
            with self.subTest(change=change), patch.object(self.client, 'invoke', side_effect=responses):
                with self.assertRaisesRegex(MemoryClientError, code):
                    self.client.read_full({'slug': 'synthetic-skill'})

    def test_full_read_propagates_server_stale_pin_rejection(self):
        with patch.object(self.client, 'invoke', side_effect=[
                self.page('a', next_offset=1, full='ab'), MemoryClientError('revision_conflict')]):
            with self.assertRaisesRegex(MemoryClientError, 'revision_conflict'):
                self.client.read_full({'slug': 'synthetic-skill'})

    @staticmethod
    def context(*pages, generation=1):
        keys = ('page_id', 'slug', 'page_kind', 'title', 'scope', 'revision', 'content_hash', 'data_scope', 'requires', 'bundle')
        return {'status': 'ready', 'data': {'authority_generation': generation, 'minimum_client_protocol': 3,
                'pages': [{key: page['data'][key] for key in keys if key in page['data']} for page in pages],
                'tasks': [], 'next_task_after_id': None}}

    def test_load_owner_skill_returns_verified_full_context_and_rechecks_generation(self):
        full = 'a' * 4096 + 'complete'
        first = self.page(full[:4096], next_offset=4096, full=full, data_scope='owner')
        last = self.page(full[4096:], offset=4096, full=full, data_scope='owner')
        context = self.context(first)
        with patch.object(self.client, 'invoke', side_effect=[context, first, last, context]):
            result = self.client.load_instruction('synthetic-skill')
        self.assertEqual(result['load_mode'], 'owner_skill')
        self.assertEqual(result['data']['body_markdown'], full)
        self.assertEqual(result['data']['body_sha256'], hashlib.sha256(full.encode()).hexdigest())
        self.assertEqual(result['context']['instructions'], [result['data']])

    def test_synthetic_loader_never_accepts_owner(self):
        for scope, accepted in [('synthetic', True), ('owner', False)]:
            with self.subTest(scope=scope), patch.object(self.client, 'invoke', return_value=self.page(data_scope=scope)):
                if accepted:
                    result = self.client.load_instruction('synthetic-skill', synthetic=True)
                    self.assertEqual(result['load_mode'], 'synthetic_test_only')
                else:
                    with self.assertRaisesRegex(MemoryClientError, 'instruction_not_loadable'):
                        self.client.load_instruction('synthetic-skill', synthetic=True)

    def test_context_rejects_ineligible_metadata_and_changed_generation(self):
        original = self.page(data_scope='owner')
        context = self.context(original)
        for changes, code in [({'authority_state': 'revoked'}, 'instruction_not_loadable'),
                              ({'operative': False}, 'instruction_not_loadable'),
                              ({'authority_generation': 2}, 'context_stale')]:
            with self.subTest(changes=changes), patch.object(self.client, 'invoke', return_value=context), \
                 patch.object(self.client, 'read_full', return_value=self.page(data_scope='owner', **changes)):
                with self.assertRaisesRegex(MemoryClientError, code):
                    self.client.load_context()
        with patch.object(self.client, 'invoke', side_effect=[context, self.context(original, generation=2)]), \
             patch.object(self.client, 'read_full', return_value=original):
            with self.assertRaisesRegex(MemoryClientError, 'context_stale'):
                self.client.load_context()

    def test_context_bundle_metadata_matches_exactly_including_presence(self):
        bundle = {'version': '1', 'sha256': 'a' * 64, 'manifest_sha256': 'b' * 64,
                  'size_bytes': 100, 'load_policy': 'always'}
        page = self.page(data_scope='owner', bundle=bundle)
        context = self.context(page)
        with patch.object(self.client, 'invoke', return_value=context), \
             patch.object(self.client, 'read_full', return_value=page):
            self.assertEqual(self.client.load_context()['data']['instructions'][0]['bundle'], bundle)
        for replacement in ({**bundle, 'load_policy': 'on-demand'}, None):
            changed = {**page, 'data': {**page['data'], 'bundle': replacement}}
            with self.subTest(bundle=replacement), patch.object(self.client, 'invoke', return_value=context), \
                 patch.object(self.client, 'read_full', return_value=changed):
                with self.assertRaisesRegex(MemoryClientError, 'context_stale'):
                    self.client.load_context()
        del context['data']['pages'][0]['bundle']
        with patch.object(self.client, 'invoke', return_value=context), \
             patch.object(self.client, 'read_full', return_value=page):
            with self.assertRaisesRegex(MemoryClientError, 'context_stale'):
                self.client.load_context()

    def test_context_dependencies_are_loaded_first_and_missing_or_cycles_fail(self):
        root = self.page(data_scope='owner', requires=['dependency'])
        dependency = self.page(data_scope='owner', slug='dependency', page_kind='procedure')
        context = self.context(root, dependency)
        with patch.object(self.client, 'invoke', return_value=context), \
             patch.object(self.client, 'read_full', side_effect=[dependency, root]) as read:
            result = self.client.load_context()
        self.assertEqual([page['slug'] for page in result['data']['instructions']], ['dependency', 'synthetic-skill'])
        self.assertEqual(read.call_args_list[0].args, ({'slug': 'dependency', 'expected_revision': 1},))
        for context, code in [(self.context(root), 'dependency_unavailable'),
                              (self.context(root, self.page(data_scope='owner', slug='dependency',
                                                            requires=['synthetic-skill'])), 'dependency_conflict')]:
            with patch.object(self.client, 'invoke', return_value=context), \
                 patch.object(self.client, 'read_full') as read:
                with self.assertRaisesRegex(MemoryClientError, code):
                    self.client.load_context()
                read.assert_not_called()

    def test_owner_loader_rejects_synthetic_and_reference_pages(self):
        for changes in [{'data_scope': 'synthetic'}, {'page_kind': 'procedure'}]:
            page = self.page(**changes)['data']
            with patch.object(self.client, 'load_context', return_value={'data': {'instructions': [page]}}):
                with self.assertRaisesRegex(MemoryClientError, 'instruction_not_loadable'):
                    self.client.load_instruction('synthetic-skill')

    def test_load_keeps_full_body_hash_validation(self):
        page = self.page(data_scope='owner', body_sha256='0' * 64)
        with patch.object(self.client, 'invoke', side_effect=[self.context(page), page]) as invoke:
            with self.assertRaisesRegex(MemoryClientError, 'invalid_service_response'):
                self.client.load_instruction('synthetic-skill')
            self.assertEqual(invoke.call_count, 2)

    def test_candidate_dependency_field_is_included_in_full_hash_verification(self):
        from tools.memory.validation import digest
        candidate = {'slug': 'synthetic-skill', 'page_kind': 'skill', 'title': 'Synthetic skill',
                     'scope': {}, 'authority_state': 'active', 'body_markdown': 'complete',
                     'requires': ['required-procedure']}
        page = self.page('complete', requires=candidate['requires'], content_hash=digest(candidate))
        with patch.object(self.client, 'invoke', return_value=page):
            self.assertEqual(self.client.read_full({'slug': 'synthetic-skill'})['data']['requires'],
                             ['required-procedure'])
        page['data']['requires'] = ['substituted']
        with patch.object(self.client, 'invoke', return_value=page):
            with self.assertRaisesRegex(MemoryClientError, 'invalid_service_response'):
                self.client.read_full({'slug': 'synthetic-skill'})

    def test_bundle_metadata_is_included_in_hash_and_pagination_comparison(self):
        from tools.memory.validation import digest
        bundle = {'version': '1.0.0', 'sha256': 'a' * 64, 'size_bytes': 100,
                  'manifest_sha256': 'b' * 64, 'load_policy': 'always'}
        candidate = {'slug': 'synthetic-skill', 'page_kind': 'skill', 'title': 'Synthetic skill',
                     'scope': {}, 'authority_state': 'active', 'body_markdown': 'complete',
                     'bundle': bundle}
        page = self.page('complete', bundle=bundle, content_hash=digest(candidate))
        with patch.object(self.client, 'invoke', return_value=page):
            self.assertEqual(self.client.read_full({'slug': 'synthetic-skill'})['data']['bundle'], bundle)
        for changes in ({'bundle': {**bundle, 'load_policy': 'on-demand'}},
                        {'bundle': {**bundle, 'version': '2.0.0'}}, {'bundle': None}):
            with self.subTest(changes=changes), patch.object(
                    self.client, 'invoke', return_value={**page, 'data': {**page['data'], **changes}}):
                with self.assertRaisesRegex(MemoryClientError, 'invalid_service_response'):
                    self.client.read_full({'slug': 'synthetic-skill'})
        chunks = [self.page('comp', full='complete', offset=0, next_offset=4,
                            bundle=bundle, content_hash=digest(candidate)),
                  self.page('lete', full='complete', offset=4, next_offset=None,
                            bundle={**bundle, 'size_bytes': 101}, content_hash=digest(candidate))]
        with patch.object(self.client, 'invoke', side_effect=chunks):
            with self.assertRaisesRegex(MemoryClientError, 'invalid_service_response'):
                self.client.read_full({'slug': 'synthetic-skill'})

    def test_protocol_header_is_included_in_signed_request(self):
        from botocore.credentials import Credentials
        self.client.session = MagicMock()
        self.client.session.get_credentials.return_value = Credentials('synthetic', 'synthetic', 'synthetic')
        response = MagicMock()
        response.status_code = 200
        response.iter_content.return_value = [b'{"status":"ready","data":{}}']
        with patch.object(self.client.http, 'post') as post:
            post.return_value.__enter__.return_value = response
            self.client._post('/v1/memory/context', {}, 5)
        headers = post.call_args.kwargs['headers']
        self.assertEqual(headers['x-maven-memory-protocol'], '3')
        self.assertIn('x-maven-memory-protocol', headers['Authorization'])
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_nonservice_http_responses_are_distinct_from_instruction_integrity_failures(self):
        from botocore.credentials import Credentials
        self.client.session = MagicMock()
        self.client.session.get_credentials.return_value = Credentials('synthetic', 'synthetic', 'synthetic')
        for raw in (b'upstream unavailable', b'{"message":"upstream unavailable"}'):
            response = MagicMock()
            response.status_code = 502
            response.iter_content.return_value = [raw]
            with self.subTest(raw=raw), patch.object(self.client.http, 'post') as post:
                post.return_value.__enter__.return_value = response
                with self.assertRaisesRegex(MemoryClientError, '^service_response_unavailable$'):
                    self.client._post('/v1/memory/read', {}, 5)

    def test_mutation_integrity_failures_never_become_retryable_outcomes(self):
        with patch.object(self.client, '_post', side_effect=MemoryClientError('invalid_service_response')):
            with self.assertRaises(MemoryClientError) as failure:
                self.client.invoke('/v1/proposals/create', {'request_key': KEY}, budget=0.01)
        self.assertEqual(failure.exception.code, 'invalid_service_response')
        self.assertEqual(failure.exception.request_key, KEY)


class InstructionCliBoundaries(unittest.TestCase):
    def run_cli(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(arguments)
        return code, json.loads(output.getvalue())

    def test_draft_validation_is_offline_and_does_not_load_configuration(self):
        request = {
            'request_key': KEY, 'proposal_id': KEY, 'page_id': KEY, 'expected_revision': 0,
            'classification': 'Sensitive', 'data_scope': 'synthetic',
            'candidate': {'slug': 'synthetic-skill', 'page_kind': 'skill', 'title': 'Synthetic skill',
                          'body_markdown': 'Synthetic instructions.', 'scope': {}, 'authority_state': 'active'},
            'source': {'origin': 'synthetic fixture', 'excerpt': ''}, 'change_reason': 'Synthetic test',
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'draft.json'
            path.write_text(json.dumps(request))
            with patch('tools.memory.cli.load_environment') as config, patch('tools.memory.cli.selected_session') as login:
                code, result = self.run_cli(['instruction', 'draft', str(path), '--validate'])
            config.assert_not_called()
            login.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'valid')

    def test_activation_confirmation_mismatch_never_selects_credentials(self):
        request = {'request_key': KEY, 'proposal_id': KEY, 'challenge_id': KEY,
                   'candidate_hash': 'a' * 64, 'expected_revision': 0,
                   'approval_source': {'origin': 'synthetic owner review', 'excerpt': 'Approve exact candidate.'}}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'activation.json'
            path.write_text(json.dumps(request))
            with patch('tools.memory.cli.selected_session') as login:
                code, result = self.run_cli(['instruction', 'activate', str(path), '--confirm-candidate', 'b' * 64])
            login.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'candidate_confirmation_mismatch')

    def test_activation_reads_with_client_role_and_resolves_with_approval_role(self):
        request = {'request_key': KEY, 'proposal_id': KEY, 'challenge_id': KEY,
                   'candidate_hash': 'a' * 64, 'expected_revision': 0,
                   'approval_source': {'origin': 'fixture review', 'excerpt': 'Approve exact fixture.'}}
        approver, reader = MagicMock(), MagicMock()
        reader.read_full.return_value = {'data': {'content_hash': 'a' * 64}}
        approver.invoke.return_value = {'status': 'ready', 'data': {'revision': 1}}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'activation.json'
            path.write_text(json.dumps(request))
            with patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
                 patch('tools.memory.cli.selected_session') as select, \
                 patch('tools.memory.cli.MemoryClient', side_effect=[approver, reader]):
                code, _ = self.run_cli(['instruction', 'activate', str(path),
                                       '--confirm-candidate', 'a' * 64])
        self.assertEqual(code, 0)
        self.assertTrue(select.call_args_list[0].kwargs['approval'])
        self.assertNotIn('approval', select.call_args_list[1].kwargs)
        approver.read_full.assert_not_called()
        reader.invoke.assert_not_called()
        approver.invoke.assert_called_once_with('/v1/proposals/resolve', request)
        reader.close.assert_called_once()
        approver.close.assert_called_once()

    def test_migrate_rejects_wrong_instance_before_selecting_credentials(self):
        with patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
             patch('tools.memory.cli.selected_session', return_value=None) as login, \
             patch('tools.memory.admin.migrate', return_value={'status': 'migrated'}) as migrate:
            code, result = self.run_cli(['migrate', '--confirm-instance', KEY])
            self.assertEqual(code, 2)
            self.assertEqual(result['status'], 'instance_confirmation_mismatch')
            login.assert_not_called()
            migrate.assert_not_called()

    def test_both_migration_commands_accept_legacy_binding_without_rewriting_reviewed_configuration(self):
        from tools.memory.environment import load_environment, memory_config
        from tools.memory import bootstrap
        for command in (['migrate'], ['install', 'migrate']):
            for version in (1, 2):
                with self.subTest(command=command, version=version), tempfile.TemporaryDirectory() as root:
                    path = Path(root) / '.env'
                    values = {'MAVEN_MEMORY_' + key.upper(): str(value) for key, value in CONFIG.items()}
                    values['MAVEN_MEMORY_SCHEMA_VERSION'] = str(version)
                    original = ''.join(key + '=' + json.dumps(value) + '\n' for key, value in values.items())
                    path.write_text(original)
                    path.chmod(0o600)
                    session = MagicMock()
                    with patch('tools.memory.admin.migrate', return_value={
                            'status': 'ready', 'schema_version': 2, 'instance_id': CONFIG['instance_id']}) as migrate, \
                            patch.object(cli, 'selected_session', return_value=session) as direct_select, \
                            patch.object(bootstrap, 'selected_session', return_value=session) as install_select, \
                            patch.object(bootstrap, 'MemoryClient') as factory:
                        factory.return_value.load_context.return_value = {
                            'data': {'authority_generation': 3, 'minimum_client_protocol': 3}}
                        code, result = self.run_cli(
                            ['--env', str(path), *command, '--confirm-instance', CONFIG['instance_id']])
                    self.assertEqual((code, result['status'], result['schema_version']), (0, 'ready', 2))
                    migrate.assert_called_once_with(CONFIG, session)
                    selection = install_select if command[0] == 'install' else direct_select
                    self.assertTrue(selection.call_args_list[0].kwargs['admin'])
                    self.assertEqual(path.read_text(), original)
                    self.assertEqual(load_environment(path), values)
                    if version == 1:
                        with self.assertRaisesRegex(MemoryClientError, '^invalid_configuration$'):
                            memory_config(values)
                    else:
                        self.assertEqual(memory_config(values), CONFIG)

    def test_migration_failure_or_mismatched_confirmation_never_rewrites_legacy_binding(self):
        from tools.memory import bootstrap
        for command in (['migrate'], ['install', 'migrate']):
            for confirmed in (KEY, CONFIG['instance_id']):
                with self.subTest(command=command, confirmed=confirmed), tempfile.TemporaryDirectory() as root:
                    path = Path(root) / '.env'
                    values = {'MAVEN_MEMORY_' + key.upper(): str(value) for key, value in CONFIG.items()}
                    values['MAVEN_MEMORY_SCHEMA_VERSION'] = '1'
                    original = ''.join(key + '=' + json.dumps(value) + '\n' for key, value in values.items())
                    path.write_text(original)
                    path.chmod(0o600)
                    with patch('tools.memory.admin.migrate', side_effect=MemoryClientError('migration_refused')) as migrate, \
                            patch.object(cli, 'selected_session') as direct_select, \
                            patch.object(bootstrap, 'selected_session') as install_select, \
                            patch.object(bootstrap, 'MemoryClient') as factory:
                        code, result = self.run_cli(
                            ['--env', str(path), *command, '--confirm-instance', confirmed])
                    self.assertEqual(code, 2)
                    self.assertEqual(path.read_text(), original)
                    factory.assert_not_called()
                    if confirmed == KEY:
                        self.assertEqual(result['status'], 'instance_confirmation_mismatch')
                        direct_select.assert_not_called()
                        install_select.assert_not_called()
                        migrate.assert_not_called()
                    else:
                        self.assertEqual(result['status'], 'migration_refused')
                        migrate.assert_called_once()

    def test_migration_does_not_relax_other_configuration_guards(self):
        for changed in ({'schema_version': 3}, {'endpoint': 'https://example.invalid'}):
            values = {'MAVEN_MEMORY_' + key.upper(): str(value)
                      for key, value in {**CONFIG, 'schema_version': 1, **changed}.items()}
            with self.subTest(changed=changed), patch.object(cli, 'load_environment', return_value=values), \
                    patch.object(cli, 'selected_session') as select, \
                    patch('tools.memory.admin.migrate') as migrate:
                code, result = self.run_cli(['migrate', '--confirm-instance', CONFIG['instance_id']])
            self.assertEqual((code, result), (2, {'status': 'invalid_configuration'}))
            select.assert_not_called()
            migrate.assert_not_called()

    def test_load_dispatches_only_named_skill_and_explicit_test_mode(self):
        for synthetic in (False, True):
            expected = {'status': 'ready', 'data': {'body_markdown': 'verified'}, 'load_mode': 'synthetic_test_only'}
            with patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
                 patch('tools.memory.cli.selected_session', return_value=None), \
                 patch('tools.memory.cli.MemoryClient') as client_type:
                client = client_type.return_value
                client.load_instruction.return_value = expected
                arguments = ['instruction', 'load', '--slug', 'synthetic-skill']
                code, result = self.run_cli(arguments + (['--synthetic'] if synthetic else []))
                client.load_instruction.assert_called_once_with('synthetic-skill', synthetic=synthetic, context={})
                client.read_full.assert_not_called()
                client.invoke.assert_not_called()
                client.close.assert_called_once()
            self.assertEqual((code, result), (0, expected))

    def test_load_failures_never_emit_success_body_or_provider_details(self):
        for error in [MemoryClientError('instruction_not_loadable'), MemoryClientError('revision_conflict'),
                      MemoryClientError('transport_unavailable'), RuntimeError('PRIVATE AWS RESPONSE')]:
            with self.subTest(error=type(error).__name__), \
                 patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
                 patch('tools.memory.cli.selected_session', return_value=None), \
                 patch('tools.memory.cli.MemoryClient') as client_type:
                client_type.return_value.load_instruction.side_effect = error
                code, result = self.run_cli(['instruction', 'load', '--slug', 'synthetic-skill'])
                client_type.return_value.close.assert_called_once()
            self.assertEqual(code, 2)
            self.assertEqual(result, {'status': error.code if isinstance(error, MemoryClientError) else 'operation_failed'})

    def test_load_configuration_and_login_failures_are_safe(self):
        for target, status in [('_connection', 'invalid_configuration'),
                               ('selected_session', 'selected_login_or_role_unavailable')]:
            with patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
                 patch('tools.memory.cli.selected_session', return_value=None), \
                 patch('tools.memory.cli.' + target, side_effect=MemoryClientError(status)), \
                 patch('tools.memory.cli.MemoryClient') as client_type:
                code, result = self.run_cli(['instruction', 'load', '--slug', 'synthetic-skill'])
                client_type.assert_not_called()
            self.assertEqual((code, result), (2, {'status': status}))



class EnvironmentAndDisplayBoundaries(unittest.TestCase):
    def test_private_literal_environment_rejects_shell_secrets_and_duplicates(self):
        from tools.memory.environment import load_environment, memory_config
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '.env'
            path.write_text('\n'.join('MAVEN_MEMORY_' + key.upper() + '=' + json.dumps(str(value))
                                      for key, value in CONFIG.items()))
            path.chmod(0o600)
            self.assertEqual(memory_config(load_environment(path)), CONFIG)
            for text in ['AWS_SECRET_ACCESS_KEY=never\n', 'export MAVEN_AWS_PROFILE=profile\n',
                         'MAVEN_AWS_PROFILE=$(command)\n', 'MAVEN_AWS_PROFILE=a\nMAVEN_AWS_PROFILE=b\n']:
                path.write_text(text)
                with self.assertRaisesRegex(MemoryClientError, 'invalid_environment'):
                    load_environment(path)
            path.write_text('MAVEN_AWS_PROFILE=selected\n')
            path.chmod(0o644)
            with self.assertRaisesRegex(MemoryClientError, 'configuration_must_be_owner_private'):
                load_environment(path)
            path.chmod(0o600)
            link = Path(root) / 'link'
            link.symlink_to(path)
            with self.assertRaises(MemoryClientError):
                load_environment(link)

    def test_selected_identity_is_explicit_and_never_falls_back(self):
        from tools.memory.environment import selected_aws_session
        with tempfile.TemporaryDirectory() as root:
            values = {'MAVEN_AWS_PROFILE': 'selected-profile',
                      'MAVEN_AWS_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/selected',
                      'MAVEN_AWS_LOGIN_CACHE': root}
            with patch('tools.memory.environment.boto3.Session') as session:
                session.return_value.client.return_value.get_caller_identity.return_value = {
                    'Arn': 'arn:aws:iam::123456789012:root'}
                with self.assertRaisesRegex(MemoryClientError, 'unexpected_login_identity'):
                    selected_aws_session(values)
                session.assert_called_once_with(profile_name='selected-profile', region_name='us-east-1')
            with patch('tools.memory.environment.boto3.Session') as session:
                with self.assertRaisesRegex(MemoryClientError, 'selected_login_unavailable_or_expired'):
                    selected_aws_session({})
                session.assert_not_called()

    def test_lossless_agent_display_preserves_every_character_with_bounded_lines(self):
        from tools.memory.display import agent_display
        original = {'body_markdown': ('é\\\n"' * 3000) + '\n\n', 'nested': [None, True, 2, {'x': ''}]}
        def decode(node):
            if node['type'] == 'string':
                return ''.join(node['chunks'])
            if node['type'] == 'object':
                return {decode(key): decode(value) for key, value in node['entries']}
            if node['type'] == 'array':
                return [decode(value) for value in node['items']]
            return node['value']
        rendered = agent_display(original)
        self.assertEqual(decode(json.loads(rendered)['value']), original)
        self.assertLess(max(map(len, rendered.splitlines())), 1000)

    def test_context_cli_fetches_once_and_closes_client(self):
        with patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
             patch('tools.memory.cli.selected_session'), patch('tools.memory.cli.MemoryClient') as factory:
            factory.return_value.load_context.return_value = {'status': 'ready', 'data': {'instructions': []}}
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(['--format', 'agent', 'context', '--project-id', KEY, '--workflow', 'work'])
            self.assertEqual(code, 0)
            factory.return_value.load_context.assert_called_once_with({'project_id': KEY, 'workflow': 'work'})
            factory.return_value.close.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())['format'], 'maven-lossless-tree-v1')

    def test_raw_record_cannot_bypass_managed_session_closure(self):
        body = {'request_key': KEY, 'record_id': KEY, 'expected_revision': 1,
                'kind': 'session', 'classification': 'Sensitive', 'data_scope': 'owner',
                'project_id': KEY, 'data': {'state': 'closed', 'summary': 'Unverified closure'},
                'source': {'origin': 'Owner conversation', 'excerpt': 'Close the session'},
                'change_reason': 'Close the session'}
        with tempfile.TemporaryDirectory() as directory, \
                patch('tools.memory.cli._connection', return_value=({}, CONFIG)), \
                patch('tools.memory.cli.selected_session'), patch('tools.memory.cli.MemoryClient') as factory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps(body))
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(['record', 'put', str(path), '--root', directory, '--session', KEY])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'lifecycle_command_required')
            factory.return_value.invoke.assert_not_called()


class InstallationBoundaries(unittest.TestCase):
    def test_prepare_accepts_zone_whitespace_without_accepting_missing_or_duplicate_zones(self):
        from tools.memory import bootstrap
        values = {'MAVEN_NOTIFICATION_EMAIL': 'owner@example.com', 'MAVEN_ENGINE_VERSION': '18.6',
                  'MAVEN_INSTANCE_NAME': 'maven-test', 'MAVEN_ACCOUNT_ID': '123456789012',
                  'MAVEN_CALLER_PRINCIPAL_ARN': 'arn:aws:iam::123456789012:user/owner',
                  'MAVEN_AVAILABILITY_ZONES': ' us-east-1b, us-east-1c '}
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'package'
            manifest = bootstrap.prepare(values, package)
            parameters = json.loads((package / 'parameters.json').read_text())
            self.assertEqual(next(item['ParameterValue'] for item in parameters
                                  if item['ParameterKey'] == 'AvailabilityZones'), 'us-east-1b,us-east-1c')
            self.assertEqual(manifest['deployment_inputs']['AvailabilityZones'], 'us-east-1b,us-east-1c')
            for index, invalid in enumerate(('us-east-1b, us-east-1b', 'us-east-1b, ',
                                             'us-east-1b, ,us-east-1c')):
                with self.subTest(zones=invalid):
                    target = Path(directory) / str(index)
                    with self.assertRaises(MemoryClientError):
                        bootstrap.prepare({**values, 'MAVEN_AVAILABILITY_ZONES': invalid}, target)
                    self.assertFalse(target.exists())

    def test_exact_review_plan_apply_and_connect_use_real_command_path_without_credentials_export(self):
        for target in ('unit-stack', 'arn:aws:cloudformation:us-east-1:123456789012:stack/unit-stack/exact-id'):
            with self.subTest(target=target):
                self._reviewed_stack_connection(target)

    def _reviewed_stack_connection(self, target):
        from tools.memory import bootstrap
        from tools.memory.deployment import OUTPUT_CONFIG
        from tools.memory.environment import load_environment
        template = '{"Resources":{}}'
        manifest = {'capabilities_required': ['CAPABILITY_IAM'], 'deployment_inputs': {
            'InstanceId': CONFIG['instance_id'], 'RecoveryEpoch': CONFIG['recovery_epoch']}}
        change = {'ChangeSetId': 'reviewed-change',
                  'StackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/unit-stack/exact-id', 'StackName': 'unit-stack',
                  'ChangeSetName': 'change', 'Status': 'CREATE_COMPLETE', 'ExecutionStatus': 'AVAILABLE',
                  'Changes': [{'Type': 'Resource', 'ResourceChange': {'Action': 'Add', 'LogicalResourceId': 'Memory'}}],
                  'Parameters': [], 'Capabilities': ['CAPABILITY_IAM']}
        cloud = MagicMock()
        cloud.describe_change_set.side_effect = [change, change, change,
                                                 {**change, 'ExecutionStatus': 'EXECUTE_COMPLETE'}]
        cloud.get_template.return_value = {'TemplateBody': json.loads(template)}
        cloud.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE',
            'Outputs': [{'OutputKey': key, 'OutputValue': str(CONFIG[field])} for key, field in OUTPUT_CONFIG.items()]}]}
        values = {'MAVEN_STACK_NAME': target}
        with tempfile.TemporaryDirectory() as root:
            env_path, review_path = Path(root) / '.env', Path(root) / 'review.json'
            env_path.write_text('MAVEN_STACK_NAME=' + json.dumps(target) + '\n')
            env_path.chmod(0o600)
            with patch.object(bootstrap, '_cloud', return_value=cloud), \
                 patch.object(bootstrap, '_package', return_value=(manifest, template, [])):
                planned = bootstrap.plan(values, root, review_path, change_type='CREATE')
                repeated = bootstrap.plan(values, root, review_path, change_type='CREATE')
                self.assertEqual(repeated, planned)
                cloud.create_change_set.assert_called_once()
                cloud.execute_change_set.assert_not_called()
                with self.assertRaisesRegex(MemoryClientError, 'review_target_mismatch'):
                    bootstrap.apply({'MAVEN_STACK_NAME': 'another-stack'}, root, review_path, env_path,
                                    confirm_review=planned['review_sha256'])
                result = bootstrap.apply(values, root, review_path, env_path,
                                         confirm_review=planned['review_sha256'])
            self.assertEqual(result['status'], 'connected')
            cloud.execute_change_set.assert_called_once()
            self.assertEqual(cloud.execute_change_set.call_args.kwargs['ChangeSetName'], 'reviewed-change')
            configured = load_environment(env_path)
            self.assertEqual(configured['MAVEN_MEMORY_INSTANCE_ID'], CONFIG['instance_id'])
            self.assertEqual(configured['MAVEN_MEMORY_SKILL_BUCKET'], CONFIG['skill_bucket'])
            self.assertFalse(any('TOKEN' in key or 'ACCESS_KEY' in key for key in configured))

    def test_connect_accepts_legacy_outputs_without_inventing_skill_storage(self):
        from tools.memory import bootstrap
        from tools.memory.deployment import OUTPUT_CONFIG
        from tools.memory.environment import load_environment, memory_config
        cloud = MagicMock()
        legacy = {key: value for key, value in CONFIG.items() if key != 'skill_bucket'}
        cloud.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE',
            'Outputs': [{'OutputKey': key, 'OutputValue': str(legacy[field])}
                        for key, field in OUTPUT_CONFIG.items() if field in legacy]}]}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '.env'
            path.write_text('MAVEN_STACK_NAME=unit-stack\n')
            path.chmod(0o600)
            bootstrap.connect({'MAVEN_STACK_NAME': 'unit-stack'}, path,
                              confirm_instance=CONFIG['instance_id'], cloud=cloud)
            configured = load_environment(path)
            self.assertEqual(memory_config(configured), legacy)
            self.assertNotIn('MAVEN_MEMORY_SKILL_BUCKET', configured)

    def test_changed_review_never_executes_and_wrong_instance_never_initializes(self):
        from tools.memory import bootstrap
        with tempfile.TemporaryDirectory() as root:
            review_path = Path(root) / 'review.json'
            bootstrap._private_json(review_path, {'review': {}, 'review_sha256': '0' * 64})
            with patch.object(bootstrap, '_package', return_value=({}, '{}', [])), \
                 patch.object(bootstrap, '_cloud') as cloud:
                with self.assertRaisesRegex(MemoryClientError, 'review_confirmation_mismatch'):
                    bootstrap.apply({}, root, review_path, Path(root) / '.env', confirm_review='0' * 64)
                cloud.assert_not_called()
        with patch('tools.memory.environment.memory_config', return_value=CONFIG), \
             patch.object(bootstrap, 'selected_session') as select:
            with self.assertRaisesRegex(MemoryClientError, 'instance_confirmation_mismatch'):
                bootstrap.initialize({}, confirm_instance=KEY)
            select.assert_not_called()

    def test_changed_aws_change_set_cannot_execute_previously_approved_digest(self):
        from tools.memory import bootstrap
        template = '{"Resources":{}}'
        manifest = {'capabilities_required': ['CAPABILITY_IAM'], 'deployment_inputs': {
            'InstanceId': CONFIG['instance_id'], 'RecoveryEpoch': CONFIG['recovery_epoch']}}
        change = {'ChangeSetId': 'reviewed-change', 'StackId': 'reviewed-stack', 'StackName': 'unit-stack',
                  'Status': 'CREATE_COMPLETE', 'ExecutionStatus': 'AVAILABLE', 'Changes': []}
        cloud = MagicMock()
        cloud.describe_change_set.side_effect = [change, {**change, 'Changes': [{'Type': 'Resource'}]}]
        with tempfile.TemporaryDirectory() as root:
            review = Path(root) / 'review.json'
            with patch.object(bootstrap, '_cloud', return_value=cloud), \
                 patch.object(bootstrap, '_package', return_value=(manifest, template, [])):
                planned = bootstrap.plan({'MAVEN_STACK_NAME': 'unit-stack'}, root, review, change_type='CREATE')
                with self.assertRaisesRegex(MemoryClientError, 'change_set_changed'):
                    bootstrap.apply({'MAVEN_STACK_NAME': 'unit-stack'}, root, review, Path(root) / '.env',
                                    confirm_review=planned['review_sha256'])
            cloud.execute_change_set.assert_not_called()

    def test_initialization_verifies_routine_context_after_admin_operation(self):
        from tools.memory import bootstrap
        with patch('tools.memory.environment.memory_config', return_value=CONFIG), \
             patch('tools.memory.admin.migrate', return_value={'status': 'ready'}) as migrate, \
             patch.object(bootstrap, 'selected_session') as select, \
             patch.object(bootstrap, 'MemoryClient') as factory:
            factory.return_value.load_context.return_value = {
                'data': {'authority_generation': 3, 'minimum_client_protocol': 3}}
            result = bootstrap.initialize({}, confirm_instance=CONFIG['instance_id'], upgrade=True)
            migrate.assert_called_once()
            self.assertTrue(select.call_args_list[0].kwargs['admin'])
            factory.return_value.load_context.assert_called_once()
            factory.return_value.close.assert_called_once()
            self.assertEqual(result['authority_generation'], 3)


class StarterBoundaries(unittest.TestCase):
    def test_complete_starter_is_offline_and_refuses_overwrite(self):
        from tools.memory.onboarding import prepare
        from tools.memory.validation import validate_create
        from uuid import UUID
        with tempfile.TemporaryDirectory() as root, patch('tools.memory.cli.selected_session') as login:
            destination = Path(root) / 'starter'
            result = prepare(destination, source_origin='fixture owner request',
                             source_excerpt='Review the generic starter proposal.')
            manifest = read_json(destination / 'manifest.json')
            seen = set()
            ids = set()
            for entry in manifest['pages']:
                request = validate_create(read_json(destination / entry['file']))
                self.assertTrue(set(request['candidate']['requires']) <= seen)
                seen.add(entry['slug'])
                for key in ('page_id', 'proposal_id', 'request_key'):
                    self.assertEqual(UUID(request[key]).version, 4)
                    self.assertNotIn(request[key], ids)
                    ids.add(request[key])
                self.assertEqual((destination / entry['file']).stat().st_mode & 0o777, 0o600)
            self.assertIn('maven', seen)
            self.assertFalse(result['authority_activated'])
            with self.assertRaises(FileExistsError):
                prepare(destination, source_origin='fixture', source_excerpt='Fixture review')
            login.assert_not_called()

    def test_approval_binds_exact_real_challenge_and_never_activates(self):
        from datetime import datetime, timedelta, timezone
        from tools.memory.onboarding import prepare, approval
        from tools.memory.validation import digest, validate_resolve
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            prepare(directory / 'starter', source_origin='fixture', source_excerpt='Fixture request')
            draft_path = directory / 'starter' / 'maven.json'
            draft = read_json(draft_path)
            challenge = {'status': 'ready', 'data': {
                'proposal_id': draft['proposal_id'], 'page_id': draft['page_id'], 'challenge_id': KEY,
                'candidate_hash': digest(draft['candidate']), 'expected_revision': 0,
                'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}}
            challenge_path = directory / 'challenge.json'
            challenge_path.write_text(json.dumps(challenge))
            result = approval(draft_path, challenge_path, directory / 'approval.json',
                              approval_origin='fixture review', approval_excerpt='Approve exact fixture')
            request = validate_resolve(read_json(directory / 'approval.json'))
            self.assertEqual(request['candidate_hash'], challenge['data']['candidate_hash'])
            self.assertFalse(result['authority_activated'])
            challenge['data']['candidate_hash'] = '0' * 64
            challenge_path.write_text(json.dumps(challenge))
            with self.assertRaisesRegex(MemoryClientError, 'candidate_confirmation_mismatch'):
                approval(draft_path, challenge_path, directory / 'rejected.json',
                         approval_origin='fixture', approval_excerpt='Fixture approval')
            self.assertFalse((directory / 'rejected.json').exists())


if __name__ == '__main__':
    unittest.main()
