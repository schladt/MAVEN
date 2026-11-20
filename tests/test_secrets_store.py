"""Offline regressions for secret boundaries, encrypted streams and catalog recovery."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from botocore.exceptions import ClientError

from tools.aws.secret_store import SecretStore, StoreError
from tools.memory.client import MemoryClientError
from tools.memory.validation import MemoryError, validate, validate_secret_ref
from tools.secret_catalog import SecretCatalog

HOME_ID = '10000000-0000-4000-8000-000000000001'
INSTANCE_ID = '20000000-0000-4000-8000-000000000002'
EPOCH = '30000000-0000-4000-8000-000000000003'
VALUES = {
    'MAVEN_ACCOUNT_ID': '123456789012', 'MAVEN_AWS_REGION': 'us-east-1',
    'MAVEN_SECRETS_PARAMETER_PREFIX': '/synthetic/secrets/',
    'MAVEN_SECRETS_FILE_KEY_PREFIX': '/synthetic/keys/',
    'MAVEN_SECRETS_OBJECT_PREFIX': 'synthetic/files/', 'MAVEN_SECRETS_BUCKET': 'synthetic-private-files',
    'MAVEN_SECRETS_KEY_ID': '0123456789abcdef', 'MAVEN_MEMORY_INSTANCE_ID': INSTANCE_ID,
    'MAVEN_MEMORY_RECOVERY_EPOCH': EPOCH,
}
SOURCE = {'origin': 'offline-owner-request', 'excerpt': 'Store and catalog owner.api.'}


def provider_error(code):
    return ClientError({'Error': {'Code': code, 'Message': 'provider detail must not escape'}}, 'synthetic')


class Parameters:
    def __init__(self):
        self.versions = {}
        self.pages = []

    def put_parameter(self, **request):
        versions = self.versions.setdefault(request['Name'], [])
        if versions and not request['Overwrite']:
            raise provider_error('ParameterAlreadyExists')
        entry = {'Name': request['Name'], 'Version': len(versions) + 1, 'Type': request['Type'],
                 'Value': request['Value'], 'Description': request['Description']}
        versions.append(entry)
        return {'Version': entry['Version']}

    def get_parameter(self, *, Name, WithDecryption):
        if Name == '/synthetic/keys/0123456789abcdef':
            return {'Parameter': {'Type': 'SecureString', 'Value': base64.b64encode(b'k' * 32).decode()}}
        key, _, version = Name.partition(':')
        entries = self.versions.get(key)
        if not entries:
            raise provider_error('ParameterNotFound')
        return {'Parameter': deepcopy(entries[int(version) - 1] if version else entries[-1])}

    def get_parameter_history(self, **request):
        return {'Parameters': deepcopy(self.versions.get(request['Name'], []))}

    def describe_parameters(self, **request):
        if self.pages:
            return self.pages.pop(0)
        return {'Parameters': [entry[-1] for entry in self.versions.values()]}


class Files:
    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.aborted = []
        self.pages = []

    def create_multipart_upload(self, **request):
        identifier = str(uuid.uuid4())
        self.uploads[identifier] = {'metadata': request['Metadata'], 'parts': {}}
        return {'UploadId': identifier}

    def upload_part(self, **request):
        self.uploads[request['UploadId']]['parts'][request['PartNumber']] = bytes(request['Body'])
        return {'ETag': str(request['PartNumber'])}

    def complete_multipart_upload(self, **request):
        if request['Key'] in self.objects:
            raise provider_error('PreconditionFailed')
        upload = self.uploads.pop(request['UploadId'])
        self.objects[request['Key']] = {'Metadata': upload['metadata'],
            'value': b''.join(upload['parts'][part['PartNumber']] for part in request['MultipartUpload']['Parts']),
            'VersionId': 'version-1'}
        return {'VersionId': 'version-1'}

    def abort_multipart_upload(self, **request):
        self.aborted.append(request['UploadId'])
        self.uploads.pop(request['UploadId'], None)

    def head_object(self, **request):
        if request['Key'] not in self.objects:
            raise provider_error('404')
        obj = self.objects[request['Key']]
        return {key: value for key, value in obj.items() if key != 'value'}

    def get_object(self, **request):
        obj = self.objects[request['Key']]
        return {'Body': io.BytesIO(obj['value']), 'ContentLength': len(obj['value']),
                'Metadata': obj['Metadata'], 'VersionId': obj['VersionId']}

    def list_objects_v2(self, **request):
        if self.pages:
            return self.pages.pop(0)
        return {'Contents': [{'Key': key, 'Size': len(obj['value'])} for key, obj in self.objects.items()]}


class Session:
    def __init__(self):
        self.parameters, self.files = Parameters(), Files()

    def client(self, service):
        return {'ssm': self.parameters, 's3': self.files}[service]


class CatalogCloud:
    def __init__(self):
        self.records, self.history, self.receipts = {}, {}, {}
        self.generation = 1
        self.fail_before = self.fail_after = None

    def load_instruction(self, slug, *, context):
        if slug != 'maven-secrets' or context != {'project_id': HOME_ID, 'instructions_only': True}:
            raise AssertionError('catalog must load only governing secret instructions')
        return {'context': {'authority_generation': self.generation}}

    def invoke(self, route, request):
        validate(route, request)
        if route == '/v1/memory/secret_lookup':
            record = self.records.get(request['name'])
            if record and 'revision' in request:
                record = self.history.get((record['record_id'], request['revision']))
            return {'status': 'ready', 'data': {'record': deepcopy(record)}}
        if route != '/v1/memory/secret_catalog':
            raise AssertionError('no ordinary memory search, session or capture is authorized')
        if self.fail_before:
            code, self.fail_before = self.fail_before, None
            raise MemoryClientError(code)
        if request['request_key'] in self.receipts:
            prior, result = self.receipts[request['request_key']]
            if request != prior:
                raise MemoryClientError('idempotency_conflict')
            return {'status': 'ready', 'data': deepcopy(result), 'reconciled': True}
        name = request['data']['key'][7:]
        old = self.records.get(name)
        if request['expected_revision'] != (old['revision'] if old else 0):
            raise MemoryClientError('revision_conflict')
        record = {key: deepcopy(request[key]) for key in ('record_id', 'kind', 'data_scope', 'project_id', 'data', 'tags')}
        record['revision'] = request['expected_revision'] + 1
        record['project_archived'] = False
        self.records[name] = record
        self.history[(record['record_id'], record['revision'])] = deepcopy(record)
        result = {key: record[key] for key in ('record_id', 'revision')}
        result['request_key'] = request['request_key']
        self.receipts[request['request_key']] = (deepcopy(request), result)
        if self.fail_after:
            code, self.fail_after = self.fail_after, None
            raise MemoryClientError(code, request_key=request['request_key'])
        return {'status': 'ready', 'data': result}


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.store = SecretStore(VALUES, self.session)

    def test_cli_encrypts_binary_stdin_and_lists_metadata(self):
        from contextlib import redirect_stdout
        from tools.aws import secrets as cli
        payload = bytes(range(256)) * 3
        output = io.StringIO()
        with patch.object(cli, 'load_settings', return_value=VALUES), \
                patch.object(cli, 'aws_session', return_value=self.session), \
                patch('tools.aws.secret_store.selected_aws_session', return_value=self.session), \
                patch.object(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(payload))), redirect_stdout(output):
            self.assertEqual(cli.main(['file', 'put', '--stdin', 'owner.binary', '--json',
                                       '--max-bytes', str(len(payload))]), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(self.store.read_secret(receipt['secret_ref']), payload)
            output.seek(0)
            output.truncate()
            self.assertEqual(cli.main(['list', '--domain', 'owner']), 0)
            self.assertEqual([item['name'] for item in json.loads(output.getvalue())['items']], ['owner.binary'])

    def test_create_never_overwrites_and_replace_pins_readable_version(self):
        first = self.store.put('owner.api', b'first synthetic value')
        with self.assertRaises(ClientError):
            self.store.put('owner.api', b'accidental overwrite')
        second = self.store.put('owner.api', b'second synthetic value', replace=True)
        self.assertEqual(self.store.read_secret(first['secret_ref']), b'first synthetic value')
        self.assertEqual(self.store.read_secret(second['secret_ref']), b'second synthetic value')
        self.assertNotIn('synthetic value', json.dumps(second))

    def test_list_pages_metadata_for_both_providers_without_secret_values(self):
        self.session.parameters.pages = [
            {'Parameters': [{'Name': '/synthetic/secrets/owner.api', 'Version': 1, 'Value': 'NEVER-RETURN'}], 'NextToken': 'next'},
            {'Parameters': [{'Name': '/synthetic/secrets/owner.token', 'Version': 2}]}]
        self.session.files.pages = [{'Contents': [{'Key': 'synthetic/files/owner.cert', 'Size': 80}]}]
        first = self.store.list(domain='owner')
        second = self.store.list(domain='owner', cursor=first['next_cursor'])
        self.assertEqual([item['name'] for item in first['items']], ['owner.api', 'owner.cert'])
        self.assertEqual([item['name'] for item in second['items']], ['owner.token'])
        self.assertIsNone(second['next_cursor'])
        self.assertNotIn('NEVER-RETURN', json.dumps(first))
        with self.assertRaises(StoreError):
            self.store.list(domain='different', cursor=first['next_cursor'])

    def test_encrypted_binary_pipe_roundtrip_and_immutable_object(self):
        payload = bytes(range(256)) * 40
        receipt = self.store.put_file('owner.key', io.BytesIO(payload), max_bytes=len(payload))
        self.assertNotIn(payload, self.session.files.objects[receipt['object_key']]['value'])
        self.assertEqual(self.store.read_secret(receipt['secret_ref']), payload)
        self.assertEqual(self.store.reconcile('owner.key', 'encrypted-file', receipt['operation_id']), receipt)
        with self.assertRaises(ClientError):
            self.store.put_file('owner.key', io.BytesIO(b'changed'), max_bytes=100)
        self.assertEqual(self.store.read_secret(receipt['secret_ref']), payload)

    def test_stream_limit_aborts_without_publishing_partial_file(self):
        with self.assertRaises(StoreError):
            self.store.put_file('owner.large', io.BytesIO(b'x' * 101), max_bytes=100)
        self.assertFalse(self.session.files.objects)
        self.assertFalse(self.session.files.uploads)

    def test_consumer_cannot_read_wrapping_key_or_another_store(self):
        with self.assertRaises(StoreError):
            self.store.read_secret({'provider': 'ssm', 'name': '/synthetic/keys/0123456789abcdef'})
        with self.assertRaises(StoreError):
            SecretStore(dict(VALUES, MAVEN_SECRETS_FILE_KEY_PREFIX='/synthetic/secrets/keys/'), self.session)
        receipt = self.store.put('owner.api', b'synthetic')
        self.session.parameters.versions[receipt['parameter']][0]['Type'] = 'String'
        with self.assertRaises(StoreError):
            self.store.read_secret(receipt['secret_ref'])

    def test_typed_file_reference_rejects_wrong_shapes(self):
        reference = {'provider': 'encrypted-file', 'name': 'synthetic/files/owner.key',
                     'store_id': self.store.store_id, 'bucket': VALUES['MAVEN_SECRETS_BUCKET'],
                     'key_id': VALUES['MAVEN_SECRETS_KEY_ID']}
        validate_secret_ref(reference)
        for change in ({'name': 'synthetic/../owner.key'}, {'key_id': 'plaintext'}, {'store_id': 'other'},
                       {'bucket': '/arbitrary/path'}, {'value': 'credential'}):
            with self.subTest(change=change), self.assertRaises(MemoryError):
                validate_secret_ref(dict(reference, **change))


    def test_upload_uses_bucket_default_including_kms_instead_of_forcing_aes256(self):
        original = self.session.files.create_multipart_upload
        key = 'arn:aws:kms:us-east-1:123456789012:key/synthetic-key'
        applied = []
        def enforcing_bucket(**request):
            algorithm = request.get('ServerSideEncryption', 'aws:kms')
            selected = request.get('SSEKMSKeyId', key)
            if algorithm != 'aws:kms' or selected != key:
                raise provider_error('AccessDenied')
            applied.append((algorithm, selected))
            self.assertEqual(request['ExpectedBucketOwner'], VALUES['MAVEN_ACCOUNT_ID'])
            return original(**request)
        with patch.object(self.session.files, 'create_multipart_upload', side_effect=enforcing_bucket) as create:
            receipt = self.store.put_file('owner.kms', io.BytesIO(b'synthetic KMS payload'), max_bytes=100)
        self.assertEqual(applied, [('aws:kms', key)])
        self.assertNotIn('ServerSideEncryption', create.call_args.kwargs)
        self.assertEqual(self.store.read_secret(receipt['secret_ref']), b'synthetic KMS payload')

    def test_store_snapshot_and_cli_restore_do_not_mix_changed_settings(self):
        from contextlib import redirect_stdout
        from tools.aws import secrets as cli
        values = dict(VALUES)
        selected = SecretStore(values, self.session)
        values['MAVEN_SECRETS_BUCKET'] = 'different-bucket'
        self.assertEqual(selected.binding['bucket'], VALUES['MAVEN_SECRETS_BUCKET'])
        for mapping in (selected.values, selected.binding):
            with self.assertRaises(TypeError):
                mapping['bucket'] = 'different'
        payload = b'synthetic streaming restore' * 100
        receipt = selected.put_file('owner.restore', io.BytesIO(payload), max_bytes=len(payload))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output'
            with patch.object(cli, 'load_settings', side_effect=[dict(VALUES), values]) as settings, \
                    patch.object(cli, 'aws_session', return_value=self.session), \
                    patch('tools.aws.secret_store.MAX_CONSUMER_BYTES', 1), \
                    patch.object(self.session.files, 'get_object', wraps=self.session.files.get_object) as get, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['file', 'get', 'owner.restore', str(destination)]), 0)
            settings.assert_called_once()
            self.assertEqual(get.call_args.kwargs, {
                'Bucket': VALUES['MAVEN_SECRETS_BUCKET'], 'Key': receipt['object_key'],
                'ExpectedBucketOwner': VALUES['MAVEN_ACCOUNT_ID']})
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(Path(directory).glob('.maven-restore-*')))

    def test_restore_never_overwrites_a_destination_created_at_publication(self):
        self.store.put_file('owner.restore', io.BytesIO(b'plaintext'), max_bytes=100)
        link = os.link
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output'
            def race(source, target):
                destination.write_bytes(b'existing owner file')
                link(source, target)
            with patch('tools.aws.secret_store.os.link', side_effect=race), \
                    self.assertRaisesRegex(StoreError, 'destination exists'):
                self.store.restore_file('owner.restore', destination)
            self.assertEqual(destination.read_bytes(), b'existing owner file')
            self.assertFalse(list(Path(directory).glob('.maven-restore-*')))
            with patch.object(self.session.files, 'get_object') as get, self.assertRaises(StoreError):
                self.store.restore_file('owner.restore', destination)
            get.assert_not_called()

    def test_unauthenticated_restore_closes_body_and_removes_plaintext_temporary(self):
        from aws_encryption_sdk.exceptions import AWSEncryptionSDKClientError
        from cryptography.exceptions import InvalidSignature
        receipt = self.store.put_file('owner.restore', io.BytesIO(b'plaintext'), max_bytes=100)
        ciphertext = bytearray(self.session.files.objects[receipt['object_key']]['value'])
        ciphertext[-1] ^= 1
        response = self.session.files.get_object(Key=receipt['object_key'])
        response['Body'] = io.BytesIO(ciphertext)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output'
            with patch.object(self.session.files, 'get_object', return_value=response), \
                    self.assertRaises((AWSEncryptionSDKClientError, InvalidSignature)):
                self.store.restore_file('owner.restore', destination)
            self.assertTrue(response['Body'].closed)
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(directory).glob('.maven-restore-*')))
            from contextlib import redirect_stderr
            from tools.aws import secrets as cli
            response['Body'] = io.BytesIO(ciphertext)
            diagnostic = io.StringIO()
            with patch.object(cli, 'load_settings', return_value=VALUES), \
                    patch.object(cli, 'aws_session', return_value=self.session), \
                    patch.object(self.session.files, 'get_object', return_value=response), \
                    redirect_stderr(diagnostic):
                self.assertEqual(cli.main(['file', 'get', 'owner.restore', str(destination)]), 2)
            self.assertEqual(diagnostic.getvalue().strip(), 'file encryption or authentication failed')
            self.assertTrue(response['Body'].closed)
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(directory).glob('.maven-restore-*')))

    def test_operation_reconciliation_scans_all_pages_and_refuses_ambiguity_or_cycles(self):
        operation = str(uuid.uuid4())
        entry = {'Type': 'SecureString', 'Description': 'MAVEN storage operation ' + operation, 'Version': 1}
        for pages, error in (
                ([{'Parameters': [entry], 'NextToken': 'second'}, {'Parameters': []}], None),
                ([{'Parameters': [entry], 'NextToken': 'second'},
                  {'Parameters': [{**entry, 'Version': 2}]}], 'ambiguous'),
                ([{'Parameters': [entry], 'NextToken': 'same'},
                  {'Parameters': [], 'NextToken': 'same'}], 'pagination'),
                ([{'Parameters': [], 'NextToken': 'one'}, {'Parameters': [], 'NextToken': 'two'},
                  {'Parameters': [], 'NextToken': 'one'}], 'pagination')):
            with self.subTest(error=error), patch.object(self.session.parameters, 'get_parameter_history', side_effect=pages) as history:
                if error:
                    with self.assertRaisesRegex(StoreError, error):
                        self.store.reconcile('owner.api', 'ssm', operation)
                else:
                    self.assertEqual(self.store.reconcile('owner.api', 'ssm', operation)['version'], 1)
                self.assertEqual(history.call_count, len(pages))
                self.assertTrue(all(call.kwargs['WithDecryption'] is False for call in history.call_args_list))

    def test_consumers_reject_unsupported_or_mutable_selectors_without_provider_reads(self):
        for reference in (
                {'provider': 'secretsmanager', 'name': 'legacy-secret'},
                {'provider': 'ssm', 'name': '/synthetic/secrets/owner.api'},
                {'provider': 'ssm', 'name': '/synthetic/secrets/owner.api:latest'},
                {'provider': 'ssm', 'name': '/synthetic/secrets/owner.api:0'},
                {'provider': 'ssm', 'name': '/synthetic/secrets/owner.api:01'}):
            with self.subTest(reference=reference), patch.object(self.session, 'client') as client, self.assertRaises(StoreError):
                self.store.read_secret(reference)
            client.assert_not_called()

    def test_retained_file_key_remains_readable_after_current_key_rotation(self):
        receipt = self.store.put_file('owner.old', io.BytesIO(b'old encrypted file'), max_bytes=100)
        rotated = SecretStore(dict(VALUES, MAVEN_SECRETS_KEY_ID='fedcba9876543210'), self.session)
        self.assertEqual(rotated.store_id, self.store.store_id)
        self.assertEqual(rotated.read_secret(receipt['secret_ref']), b'old encrypted file')

    def test_special_input_is_rejected_before_opening_and_regular_descriptor_is_nonblocking(self):
        from tools.aws.secrets import open_input
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory).resolve() / 'fifo'
            os.mkfifo(fifo, 0o600)
            for path in (fifo, fifo.parent):
                with self.subTest(path=path), patch('tools.aws.secrets.os.open') as opened, self.assertRaises(StoreError):
                    open_input(path, private=True)
                opened.assert_not_called()
            regular = fifo.parent / 'file'
            regular.write_bytes(b'synthetic')
            regular.chmod(0o600)
            with patch('tools.aws.secrets.os.open', wraps=os.open) as opened:
                with open_input(regular, private=True) as stream:
                    self.assertEqual(stream.read(), b'synthetic')
            self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)

    def test_input_replaced_by_fifo_after_precheck_cannot_block_on_open(self):
        from tools.aws.secrets import open_input
        opened = os.open
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'input'
            path.write_bytes(b'synthetic')
            path.chmod(0o600)
            def replace_with_fifo(selected, flags):
                path.unlink()
                os.mkfifo(path, 0o600)
                self.assertTrue(flags & os.O_NONBLOCK)
                return opened(selected, flags)
            with patch('tools.aws.secrets.os.open', side_effect=replace_with_fifo), self.assertRaises(StoreError):
                open_input(path, private=True)

    def test_restore_publishes_only_after_decryptor_finalization(self):
        from contextlib import contextmanager
        @contextmanager
        def incomplete_authentication(name):
            yield io.BytesIO(b'unauthenticated plaintext')
            raise StoreError('authentication did not finish')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output'
            with patch.object(self.store, '_decrypt_file', incomplete_authentication), \
                    self.assertRaisesRegex(StoreError, 'authentication did not finish'):
                self.store.restore_file('owner.restore', destination)
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(directory).glob('.maven-restore-*')))

class SecretCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.input = self.home / 'input'
        self.input.write_bytes(b'synthetic credential')
        self.input.chmod(0o600)
        self.session = Session()
        self.store = SecretStore(VALUES, self.session)
        self.cloud = CatalogCloud()
        environment = patch('tools.secret_catalog.load_environment', return_value=VALUES)
        home = patch('tools.secret_catalog.load_home', return_value={
            'project_id': HOME_ID, 'instance_id': INSTANCE_ID, 'recovery_epoch': EPOCH})
        environment.start()
        home.start()
        self.addCleanup(environment.stop)
        self.addCleanup(home.stop)
        self.catalog = SecretCatalog(self.home, client=self.cloud, store=self.store)

    def create(self, **options):
        return self.catalog.store_and_catalog('owner.api', source_path=self.input, source=SOURCE, **options)

    def test_restored_instance_cannot_resume_prior_epoch_secret_write(self):
        self.cloud.fail_before = 'temporarily_unavailable'
        pending = self.create()
        new_epoch = str(uuid.uuid4())
        binding = {'project_id': HOME_ID, 'instance_id': INSTANCE_ID, 'recovery_epoch': new_epoch}
        with patch('tools.secret_catalog.load_home', return_value=binding):
            with self.assertRaisesRegex(StoreError, 'recovery epoch'):
                SecretCatalog(self.home, client=self.cloud, store=self.store)
            with patch('tools.secret_catalog.load_environment', return_value={**VALUES, 'MAVEN_MEMORY_RECOVERY_EPOCH': new_epoch}):
                restored = SecretCatalog(self.home, client=self.cloud, store=self.store)
                with self.assertRaisesRegex(StoreError, 'different Home, instance or store'):
                    restored.retry_catalog(pending['operation_id'])
        self.assertFalse(self.cloud.records)

    def test_catalog_failure_retries_only_metadata_and_never_stores_plaintext_locally(self):
        self.cloud.fail_before = 'temporarily_unavailable'
        pending = self.create(domain='owner', service='api')
        self.assertEqual((pending['storage'], pending['catalog']), ('stored', 'pending'))
        completed = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 1)
        self.assertEqual(self.cloud.records['owner.api']['kind'], 'setting')
        for path in (self.home / 'runtime').rglob('*.json'):
            self.assertNotIn('synthetic credential', path.read_text())

    def test_lost_catalog_receipt_reuses_original_request_without_duplicate_revision(self):
        self.cloud.fail_after = 'outcome_unknown'
        pending = self.create()
        self.assertEqual(pending['catalog'], 'pending')
        saved = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(self.cloud.records['owner.api']['revision'], 1)
        self.assertEqual(len(self.cloud.receipts), 1)

    def test_context_refresh_keeps_original_pointer_cas_and_storage(self):
        self.cloud.fail_before = 'context_stale'
        pending = self.create()
        self.cloud.generation = 2
        saved = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 1)
        request, _ = next(iter(self.cloud.receipts.values()))
        self.assertEqual((request['authority_generation'], request['expected_revision']), (2, 0))

    def test_concurrent_catalog_change_is_not_overwritten_by_rotation_retry(self):
        self.create()
        self.cloud.fail_before = 'temporarily_unavailable'
        pending = self.create(replace=True)
        concurrent = deepcopy(self.cloud.records['owner.api'])
        concurrent['revision'] += 1
        concurrent['data']['value']['previous_metadata'] = 'concurrent owner change'
        self.cloud.records['owner.api'] = concurrent
        result = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(result['error'], 'revision_conflict')
        self.assertEqual(self.cloud.records['owner.api'], concurrent)
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 2)

    def test_file_rotation_keeps_original_and_moves_exact_catalog_pointer(self):
        first = self.create(provider='encrypted-file')
        old = deepcopy(self.cloud.records['owner.api']['data']['secret_ref'])
        self.input.write_bytes(b'rotated synthetic key')
        second = self.create(provider='encrypted-file', replace=True)
        new = self.cloud.records['owner.api']['data']['secret_ref']
        self.assertEqual((first['state'], second['state']), ('completed', 'completed'))
        self.assertNotEqual(old['name'], new['name'])
        self.assertEqual(self.store.read_secret(old), b'synthetic credential')
        self.assertEqual(self.store.read_secret(new), b'rotated synthetic key')

    def test_unknown_storage_is_reconciled_by_metadata_not_uploaded_again(self):
        original = self.store.put
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise MemoryClientError('outcome_unknown')
        with patch.object(self.store, 'put', side_effect=lost_response):
            pending = self.create()
        self.assertEqual(pending['storage'], 'outcome_unknown')
        result = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 1)

    def test_private_input_is_required_before_any_storage_effect(self):
        self.input.chmod(0o644)
        with self.assertRaises(StoreError):
            self.create()
        self.assertFalse(self.session.parameters.versions)
        self.assertFalse(self.cloud.records)

    def test_narrow_catalog_endpoint_refuses_unrelated_setting_or_universal_write(self):
        self.create()
        request, _ = next(iter(self.cloud.receipts.values()))
        unrelated = deepcopy(request)
        unrelated['data']['key'] = 'unrelated-setting'
        with self.assertRaises(MemoryError):
            validate('/v1/memory/secret_catalog', unrelated)
        universal = deepcopy(request)
        universal.pop('project_id')
        universal['data']['scope'] = 'global'
        with self.assertRaises(MemoryError):
            validate('/v1/memory/secret_catalog', universal)

    def test_invalid_local_ssm_input_is_definitive_and_never_reconciled_or_uploaded(self):
        for value in (b'', b'x' * 4097, b'\xff'):
            self.input.write_bytes(value)
            with self.subTest(length=len(value)), patch.object(self.store.session, 'client') as provider:
                result = self.create()
            self.assertEqual((result['state'], result['storage'], result['error']),
                             ('storage_failed', 'failed', 'storage_rejected'))
            provider.assert_not_called()
            with patch.object(self.store, 'reconcile') as reconcile:
                self.assertEqual(self.catalog.retry_catalog(result['operation_id']), result)
            reconcile.assert_not_called()
        self.assertFalse(self.cloud.records)

    def test_missing_storage_receipt_is_still_uncertain_after_possible_submission(self):
        original = self.session.parameters.put_parameter
        def missing_receipt(**request):
            original(**request)
            return {}
        with patch.object(self.session.parameters, 'put_parameter', side_effect=missing_receipt):
            result = self.create()
        self.assertEqual(result['storage'], 'outcome_unknown')
        self.assertEqual(self.catalog.retry_catalog(result['operation_id'])['state'], 'completed')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 1)

    def test_conflict_resolution_requires_exact_review_and_reuses_original_storage_receipt(self):
        self.create()
        self.cloud.fail_before = 'temporarily_unavailable'
        pending = self.create(replace=True)
        original = deepcopy(self.catalog._load(pending['operation_id'])['catalog_request'])
        concurrent = deepcopy(self.cloud.records['owner.api'])
        concurrent['revision'] = 2
        concurrent['data']['value']['previous_metadata'] = 'owner concurrent metadata'
        self.cloud.records['owner.api'] = concurrent
        conflict = self.catalog.retry_catalog(pending['operation_id'])
        self.assertEqual(conflict['state'], 'catalog_conflict')
        self.assertEqual(conflict['conflict']['current'], concurrent)
        with self.assertRaises(StoreError):
            self.catalog.resolve_catalog(pending['operation_id'], expected_revision=1, source=SOURCE)
        approval = {'origin': 'offline-owner-resolution', 'excerpt': 'Use the original stored version at reviewed revision 2.'}
        self.cloud.fail_after = 'outcome_unknown'
        with patch.object(self.store, 'put') as put:
            uncertain = self.catalog.resolve_catalog(pending['operation_id'], expected_revision=2, source=approval)
            self.assertEqual(uncertain['catalog'], 'pending')
            saved = self.catalog.resolve_catalog(pending['operation_id'], expected_revision=2, source=approval)
        put.assert_not_called()
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['storage_receipt'], pending['storage_receipt'])
        journal = self.catalog._load(pending['operation_id'])
        self.assertEqual(journal['rejected_catalog_requests'], [original])
        self.assertEqual(journal['catalog_request']['source'], approval)
        self.assertEqual(journal['catalog_request']['expected_revision'], 2)
        self.assertNotEqual(journal['catalog_request']['request_key'], original['request_key'])
        record = self.cloud.records['owner.api']
        self.assertEqual(record['revision'], 3)
        self.assertEqual(record['data']['secret_ref'], pending['storage_receipt']['secret_ref'])
        self.assertEqual(record['data']['value']['previous_metadata'], 'owner concurrent metadata')
        self.assertEqual(len(self.session.parameters.versions['/synthetic/secrets/owner.api']), 2)

    def test_conflict_resolution_cannot_retarget_a_changed_record_without_new_review(self):
        self.create()
        self.cloud.fail_before = 'revision_conflict'
        pending = self.create(replace=True)
        self.cloud.records['owner.api']['revision'] += 1
        with patch.object(self.store, 'put') as put, self.assertRaisesRegex(StoreError, 'changed since conflict review'):
            self.catalog.resolve_catalog(pending['operation_id'], expected_revision=1, source=SOURCE)
        put.assert_not_called()
        self.assertEqual(self.catalog._load(pending['operation_id'])['receipt'], pending['storage_receipt'])

    def test_explicit_legacy_catalog_replacement_publishes_a_new_pinned_revision(self):
        self.create()
        record = self.cloud.records['owner.api']
        record['data']['secret_ref']['name'] = '/synthetic/secrets/owner.api'
        record['data']['value'] = 'legacy owner metadata'
        with self.assertRaises(StoreError):
            self.store.read_secret(record['data']['secret_ref'])
        migrated = self.create(replace=True)
        self.assertEqual(migrated['state'], 'completed')
        new = self.cloud.records['owner.api']
        self.assertEqual(new['revision'], 2)
        self.assertEqual(new['data']['secret_ref']['name'], '/synthetic/secrets/owner.api:2')
        self.assertEqual(new['data']['value']['previous_metadata'], 'legacy owner metadata')


if __name__ == '__main__':
    unittest.main()
