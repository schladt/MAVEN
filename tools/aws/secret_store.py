"""Configured secret storage; receipts contain locators, never credential values."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
import math
import re
import uuid

from botocore.exceptions import ClientError

from tools.aws.settings import prefix, required
from tools.aws.secrets import KEY_ID, NAME, StoreError, file_keyring
from tools.memory.environment import selected_aws_session

MAX_VALUE_BYTES = 4096
MAX_FILE_BYTES = 5 * 1024 ** 4
DEFAULT_STREAM_LIMIT = 64 * 1024 ** 3
MAX_CONSUMER_BYTES = 16 * 1024 ** 2
INTEGRATION_KEYS = frozenset({
    'MAVEN_SECRETS_PARAMETER_PREFIX', 'MAVEN_SECRETS_BUCKET',
    'MAVEN_SECRETS_OBJECT_PREFIX', 'MAVEN_SECRETS_FILE_KEY_PREFIX', 'MAVEN_SECRETS_KEY_ID',
})


def valid_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise StoreError('name must be one short lowercase segment (letters, digits, _, ., -)')
    return value


def _operation(value=None):
    value = str(uuid.uuid4()) if value is None else value
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise StoreError('operation_id must be a canonical UUID') from None
    return value


def store_binding(values):
    """Stable store identity excludes host-specific profiles and the current wrapping key."""
    account = values.get('MAVEN_ACCOUNT_ID')
    if not account:
        principal = values.get('MAVEN_AWS_EXPECTED_PRINCIPAL', '')
        role = values.get('MAVEN_MEMORY_CLIENT_ROLE_ARN', '')
        match = re.fullmatch(r'arn:aws:(?:iam|sts)::([0-9]{12}):.+', principal or role)
        account = match[1] if match else None
    if not isinstance(account, str) or not re.fullmatch(r'[0-9]{12}', account):
        raise StoreError('verified AWS account is required for the secret store binding')
    region = required(values, 'MAVEN_AWS_REGION')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region):
        raise StoreError('invalid secret store region')
    bucket = required(values, 'MAVEN_SECRETS_BUCKET')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise StoreError('invalid secret store bucket')
    key_id = required(values, 'MAVEN_SECRETS_KEY_ID')
    if not KEY_ID.fullmatch(key_id):
        raise StoreError('invalid wrapping key identifier')
    parameter_prefix = prefix(values, 'MAVEN_SECRETS_PARAMETER_PREFIX')
    key_prefix = prefix(values, 'MAVEN_SECRETS_FILE_KEY_PREFIX')
    if parameter_prefix.startswith(key_prefix) or key_prefix.startswith(parameter_prefix):
        raise StoreError('secret and wrapping-key namespaces must be separate')
    binding = {'account': account, 'region': region, 'bucket': bucket,
               'parameter_prefix': parameter_prefix, 'key_prefix': key_prefix,
               'object_prefix': prefix(values, 'MAVEN_SECRETS_OBJECT_PREFIX', parameter=False)}
    return binding


class BoundedReader(io.RawIOBase):
    """Limit unknown-length plaintext without spooling it to disk or reading it all."""
    def __init__(self, source, maximum):
        if type(maximum) is not int or not 0 < maximum <= MAX_FILE_BYTES:
            raise StoreError('file byte limit must be positive and at most 5 TiB')
        self.source, self.maximum, self.count = source, maximum, 0

    def readable(self):
        return True

    def tell(self):
        return self.count

    def read(self, size=-1):
        size = 1024 ** 2 if size is None or size < 0 else size
        if not size:
            return b''
        data = self.source.read(min(size, self.maximum - self.count + 1))
        if not isinstance(data, bytes):
            raise StoreError('file input must be a binary stream')
        self.count += len(data)
        if self.count > self.maximum:
            raise StoreError('file input exceeds its reviewed byte limit')
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def _date(value):
    return value.isoformat() if isinstance(value, datetime) else value


class SecretStore:
    def __init__(self, values, session=None):
        self.values = dict(values)
        self.binding = store_binding(self.values)
        self.store_id = hashlib.sha256(json.dumps(self.binding, sort_keys=True,
                                                 separators=(',', ':')).encode()).hexdigest()
        self.session = session or selected_aws_session(self.values, region=self.binding['region'])

    def parameter(self, name):
        return self.binding['parameter_prefix'] + valid_name(name)

    def _receipt(self, name, operation_id, version):
        parameter = self.parameter(name)
        return {'provider': 'ssm', 'store_id': self.store_id, 'name': name,
                'parameter': parameter, 'version': version, 'operation_id': operation_id,
                'secret_ref': {'provider': 'ssm', 'name': parameter + ':' + str(version)}}

    def put(self, name, value, *, replace=False, operation_id=None):
        parameter, operation_id = self.parameter(name), _operation(operation_id)
        if not isinstance(value, bytes) or not 0 < len(value) <= MAX_VALUE_BYTES:
            raise StoreError('SSM input must contain 1 to 4096 UTF-8 bytes')
        try:
            text = value.decode('utf-8')
        except UnicodeDecodeError:
            raise StoreError('SSM input must be UTF-8') from None
        ssm = self.session.client('ssm')
        if replace:
            existing = ssm.get_parameter(Name=parameter, WithDecryption=False)['Parameter']
            if existing.get('Type') != 'SecureString':
                raise StoreError('refusing to replace a parameter that is not SecureString')
        # SSM has no compare-and-swap. Catalog references pin the returned version;
        # concurrent catalog pointer changes are rejected separately, never overwritten.
        response = ssm.put_parameter(Name=parameter, Value=text, Type='SecureString',
                                     KeyId='alias/aws/ssm', Tier='Standard', Overwrite=replace,
                                     Description='MAVEN storage operation ' + operation_id)
        version = response.get('Version')
        if type(version) is not int or version < 1:
            raise StoreError('storage outcome is unknown; reconcile the original operation')
        return self._receipt(name, operation_id, version)

    def reconcile(self, name, provider, operation_id):
        """Read storage metadata only; absence never authorizes an uncertain re-upload."""
        valid_name(name)
        _operation(operation_id)
        if provider == 'ssm':
            ssm, token = self.session.client('ssm'), None
            while True:
                request = {'Name': self.parameter(name), 'WithDecryption': False, 'MaxResults': 50}
                if token:
                    request['NextToken'] = token
                try:
                    response = ssm.get_parameter_history(**request)
                except ClientError as error:
                    if error.response.get('Error', {}).get('Code') == 'ParameterNotFound':
                        return None
                    raise
                matches = [entry for entry in response.get('Parameters', [])
                           if entry.get('Description') == 'MAVEN storage operation ' + operation_id
                           and entry.get('Type') == 'SecureString']
                if matches:
                    if len(matches) != 1:
                        raise StoreError('storage operation has ambiguous versions')
                    return self._receipt(name, operation_id, matches[0]['Version'])
                token = response.get('NextToken')
                if not token:
                    return None
        if provider != 'encrypted-file':
            raise StoreError('unsupported secret store provider')
        try:
            result = self.session.client('s3').head_object(
                Bucket=self.binding['bucket'], Key=self.binding['object_prefix'] + name,
                ExpectedBucketOwner=self.binding['account'])
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise
        if result.get('Metadata', {}).get('maven-operation') != operation_id:
            raise StoreError('file belongs to a different storage operation')
        return self._file_receipt(name, operation_id, result.get('VersionId'),
                                  result.get('Metadata', {}).get('maven-key-id'))

    def _file_receipt(self, name, operation_id, version_id, key_id):
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            raise StoreError('file wrapping key metadata is invalid')
        reference = {'provider': 'encrypted-file', 'store_id': self.store_id,
                     'name': self.binding['object_prefix'] + name,
                     'bucket': self.binding['bucket'], 'key_id': key_id}
        if version_id and version_id != 'null':
            reference['version_id'] = version_id
        return {'provider': 'encrypted-file', 'store_id': self.store_id, 'name': name,
                'bucket': reference['bucket'], 'object_key': reference['name'], 'key_id': key_id,
                'operation_id': operation_id, 'secret_ref': reference,
                **({'version_id': version_id} if 'version_id' in reference else {})}

    def put_file(self, name, stream, *, source_size=None, max_bytes=DEFAULT_STREAM_LIMIT, operation_id=None):
        import aws_encryption_sdk
        from aws_encryption_sdk import CommitmentPolicy
        valid_name(name)
        operation_id = _operation(operation_id)
        if source_size is not None:
            if type(source_size) is not int or not 0 <= source_size <= max_bytes:
                raise StoreError('file input exceeds its reviewed byte limit')
        bounded = BoundedReader(stream, max_bytes)
        key_id = self.values['MAVEN_SECRETS_KEY_ID']
        keyring = file_keyring(self.session.client('ssm'), key_id, values=self.values)
        client = aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT)
        s3, bucket = self.session.client('s3'), self.binding['bucket']
        object_key = self.binding['object_prefix'] + name
        part_size = max(8 * 1024 ** 2, math.ceil(((source_size if source_size is not None else max_bytes)
                                               * 1.01 + 1024 ** 2) / 9500))
        options = {'mode': 'e', 'source': bounded, 'keyring': keyring,
                   'encryption_context': {'maven-object': name, 'maven-key-id': key_id}}
        if source_size is not None:
            options['source_length'] = source_size
        with client.stream(**options) as encryptor:
            upload = s3.create_multipart_upload(
                Bucket=bucket, Key=object_key, ExpectedBucketOwner=self.binding['account'],
                Metadata={'maven-key-id': key_id, 'maven-operation': operation_id},
                ServerSideEncryption='AES256')
            upload_id, parts = upload['UploadId'], []
            try:
                while True:
                    chunk = encryptor.read(part_size)
                    if not chunk:
                        break
                    if len(chunk) < part_size:
                        partial = bytearray(chunk)
                        while len(partial) < part_size:
                            extra = encryptor.read(part_size - len(partial))
                            if not extra:
                                break
                            partial.extend(extra)
                        chunk = bytes(partial)
                    if len(parts) >= 10000:
                        raise StoreError('file input exceeds the S3 multipart limit')
                    result = s3.upload_part(Bucket=bucket, Key=object_key, UploadId=upload_id,
                                            PartNumber=len(parts) + 1, Body=chunk,
                                            ExpectedBucketOwner=self.binding['account'])
                    parts.append({'PartNumber': len(parts) + 1, 'ETag': result['ETag']})
                if source_size is not None and bounded.count != source_size:
                    raise StoreError('file input size changed during encryption')
                result = s3.complete_multipart_upload(
                    Bucket=bucket, Key=object_key, UploadId=upload_id, MultipartUpload={'Parts': parts},
                    ExpectedBucketOwner=self.binding['account'], IfNoneMatch='*')
            except BaseException:
                try:
                    s3.abort_multipart_upload(Bucket=bucket, Key=object_key, UploadId=upload_id,
                                              ExpectedBucketOwner=self.binding['account'])
                except Exception:
                    pass  # Never expose provider errors; original operation remains recoverable.
                raise
        return self._file_receipt(name, operation_id, result.get('VersionId'), key_id)

    def list(self, domain=None, cursor=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise StoreError('list limit must be from 1 to 50 per provider')
        if domain is not None:
            valid_name(domain)
        tokens = {}
        if cursor:
            try:
                tokens = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if (not isinstance(tokens, dict) or tokens.get('store_id') != self.store_id
                        or tokens.get('domain') != domain or set(tokens) - {'store_id', 'domain', 'ssm', 'file'}
                        or any(not isinstance(tokens.get(key, ''), str) for key in ('ssm', 'file'))):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise StoreError('list cursor does not match the selected store/domain') from None
        result, next_tokens = [], {'store_id': self.store_id, 'domain': domain}
        suffix = domain + '.' if domain else ''
        if tokens.get('ssm') != 'done':
            query = {'ParameterFilters': [
                {'Key': 'Name', 'Option': 'BeginsWith', 'Values': [self.binding['parameter_prefix'] + suffix]},
                {'Key': 'Type', 'Option': 'Equals', 'Values': ['SecureString']}], 'MaxResults': limit}
            if tokens.get('ssm'):
                query['NextToken'] = tokens['ssm']
            page = self.session.client('ssm').describe_parameters(**query)
            for item in page.get('Parameters', []):
                full_name = item.get('Name', '')
                if not full_name.startswith(self.binding['parameter_prefix']):
                    raise StoreError('provider returned a parameter outside the selected namespace')
                name = full_name[len(self.binding['parameter_prefix']):]
                if NAME.fullmatch(name) and (not domain or name.startswith(suffix)):
                    result.append({'provider': 'ssm', 'name': name, 'version': item.get('Version'),
                                   'modified_at': _date(item.get('LastModifiedDate'))})
            next_tokens['ssm'] = page.get('NextToken') or 'done'
        else:
            next_tokens['ssm'] = 'done'
        if tokens.get('file') != 'done':
            query = {'Bucket': self.binding['bucket'], 'Prefix': self.binding['object_prefix'] + suffix,
                     'MaxKeys': limit, 'ExpectedBucketOwner': self.binding['account']}
            if tokens.get('file'):
                query['ContinuationToken'] = tokens['file']
            page = self.session.client('s3').list_objects_v2(**query)
            for item in page.get('Contents', []):
                key = item.get('Key', '')
                if not key.startswith(self.binding['object_prefix']):
                    raise StoreError('provider returned an object outside the selected namespace')
                name = key[len(self.binding['object_prefix']):]
                if NAME.fullmatch(name) and (not domain or name.startswith(suffix)):
                    result.append({'provider': 'encrypted-file', 'name': name, 'size_bytes': item.get('Size'),
                                   'modified_at': _date(item.get('LastModified'))})
            next_tokens['file'] = page.get('NextContinuationToken') or 'done'
        else:
            next_tokens['file'] = 'done'
        more = any(next_tokens[key] != 'done' for key in ('ssm', 'file'))
        next_cursor = base64.urlsafe_b64encode(json.dumps(next_tokens).encode()).decode() if more else None
        return {'store_id': self.store_id, 'items': result, 'next_cursor': next_cursor}

    def read_secret(self, reference):
        """Private runner input only. Never return this method's result through MCP."""
        if not isinstance(reference, dict):
            raise StoreError('secret reference is invalid')
        if reference.get('provider') == 'ssm':
            name = reference.get('name', '')
            if not name.startswith(self.binding['parameter_prefix']):
                raise StoreError('secret reference belongs to another store')
            selector = name[len(self.binding['parameter_prefix']):].split(':')
            valid_name(selector[0])
            if len(selector) > 2 or len(selector) == 2 and not re.fullmatch(r'[1-9][0-9]*', selector[1]):
                raise StoreError('secret version must be an exact positive integer')
            item = self.session.client('ssm').get_parameter(Name=name, WithDecryption=True)['Parameter']
            if item.get('Type') != 'SecureString' or (len(selector) == 2 and item.get('Version') != int(selector[1])):
                raise StoreError('secret reference version or type changed')
            result = item['Value'].encode('utf-8')
            if len(result) > MAX_VALUE_BYTES:
                raise StoreError('secret exceeds the consumer byte limit')
            return result
        if (reference.get('provider') != 'encrypted-file' or reference.get('store_id') != self.store_id
                or reference.get('bucket') != self.binding['bucket']
                or not reference.get('name', '').startswith(self.binding['object_prefix'])):
            raise StoreError('encrypted file reference belongs to another store')
        name = reference['name'][len(self.binding['object_prefix']):]
        valid_name(name)
        key_id = reference.get('key_id', '')
        if not KEY_ID.fullmatch(key_id):
            raise StoreError('encrypted file key identifier is invalid')
        query = {'Bucket': reference['bucket'], 'Key': reference['name'],
                 'ExpectedBucketOwner': self.binding['account']}
        if reference.get('version_id'):
            query['VersionId'] = reference['version_id']
        response = self.session.client('s3').get_object(**query)
        with closing(response['Body']) as body:
            if response.get('Metadata', {}).get('maven-key-id') != key_id:
                raise StoreError('encrypted file key metadata changed')
            import aws_encryption_sdk
            from aws_encryption_sdk import CommitmentPolicy
            keyring = file_keyring(self.session.client('ssm'), key_id, values=self.values)
            client = aws_encryption_sdk.EncryptionSDKClient(
                commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT)
            with client.stream(mode='d', source=body, source_length=response['ContentLength'],
                               keyring=keyring, encryption_context={'maven-object': name, 'maven-key-id': key_id}) as decrypted:
                value = decrypted.read(MAX_CONSUMER_BYTES + 1)
                if len(value) > MAX_CONSUMER_BYTES or decrypted.read(1):
                    raise StoreError('encrypted file exceeds the consumer byte limit')
                return value
