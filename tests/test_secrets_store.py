"""Offline regressions for secret boundaries, encrypted streams and catalog recovery."""
import base64
from copy import deepcopy
import io
import json
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
VALUES = {
    'MAVEN_ACCOUNT_ID': '123456789012', 'MAVEN_AWS_REGION': 'us-east-1',
    'MAVEN_SECRETS_PARAMETER_PREFIX': '/synthetic/secrets/',
    'MAVEN_SECRETS_FILE_KEY_PREFIX': '/synthetic/keys/',
    'MAVEN_SECRETS_OBJECT_PREFIX': 'synthetic/files/', 'MAVEN_SECRETS_BUCKET': 'synthetic-private-files',
    'MAVEN_SECRETS_KEY_ID': '0123456789abcdef', 'MAVEN_MEMORY_INSTANCE_ID': INSTANCE_ID,
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


class SecretCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.input = self.home / 'input'
        self.input.write_bytes(b'synthetic credential')
        self.input.chmod(0o600)
        self.session = Session()
        self.store = SecretStore(VALUES, self.session)
        self.cloud = CatalogCloud()
        environment = patch('tools.secret_catalog.load_environment', return_value=VALUES)
        home = patch('tools.secret_catalog.load_home', return_value={'project_id': HOME_ID, 'instance_id': INSTANCE_ID})
        environment.start()
        home.start()
        self.addCleanup(environment.stop)
        self.addCleanup(home.stop)
        self.catalog = SecretCatalog(self.home, client=self.cloud, store=self.store)

    def create(self, **options):
        return self.catalog.store_and_catalog('owner.api', source_path=self.input, source=SOURCE, **options)

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


if __name__ == '__main__':
    unittest.main()
