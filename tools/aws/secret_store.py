"""Configured secret storage; receipts contain locators, never credential values."""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import uuid
import tempfile
from types import MappingProxyType

from botocore.exceptions import ClientError

from tools.aws.settings import prefix, required
from tools.aws.secrets import KEY_ID, NAME, StorageRejected, StoreError, file_keyring
from tools.memory.secrets_contract import validate_consumer_ref
from tools.memory import redaction_objects as redact
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
            raise StorageRejected('file byte limit must be positive and at most 5 TiB')
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
        self.values = MappingProxyType(dict(values))
        binding = store_binding(self.values)
        self.binding = MappingProxyType(binding)
        self.store_id = hashlib.sha256(json.dumps(binding, sort_keys=True,
                                                 separators=(',', ':')).encode()).hexdigest()
        self._explicit_session = session is not None
        self.session = session if session is not None else selected_aws_session(self.values, region=self.binding['region'])

    def _redaction_admin(self):
        from tools.memory.validation import authorized_actor
        if not self._explicit_session or self.session.region_name != self.binding['region']:
            redact.refuse('invalid_redaction_scope')
        with closing(self.session.client('sts')) as sts:
            identity = sts.get_caller_identity()
        if identity.get('Account') != self.binding['account']:
            redact.refuse('invalid_redaction_scope')
        authorized_actor({'requestContext': {'authorizer': {'iam': {'userArn': identity.get('Arn')}}}},
                         self.values.get('MAVEN_MEMORY_ADMIN_ROLE_ARN'))

    @redact.guarded
    def redaction_target(self, kind, name, salt):
        """Private owner-terminal lookup. Names are never part of the returned review."""
        if kind not in ('ssm', 'encrypted-file'):
            redact.refuse('invalid_redaction_scope')
        return {'kind': kind, 'id': redact.handle(salt, kind, self.store_id + ':' + valid_name(name))}

    def _redaction_names(self, kind):
        if kind == 'encrypted-file':
            prefix = self.binding['object_prefix']
            keys = redact.secret_file_keys(self.session.client('s3'), self.binding['bucket'], self.binding['account'], prefix)
            return {key[len(prefix):] for key in keys if NAME.fullmatch(key[len(prefix):])}
        if kind != 'ssm':
            redact.refuse('invalid_redaction_scope')
        prefix = self.binding['parameter_prefix']
        ssm = self.session.client('ssm')
        names, seen, args = set(), set(), {}
        while True:
            page = ssm.describe_parameters(ParameterFilters=[
                {'Key': 'Name', 'Option': 'BeginsWith', 'Values': [prefix]}], MaxResults=50, **args)
            for item in page['Parameters']:
                name = item['Name']
                if not name.startswith(prefix):
                    redact.refuse('inventory_unverified')
                if item.get('Type') == 'SecureString' and NAME.fullmatch(name[len(prefix):]):
                    names.add(name[len(prefix):])
            token = page.get('NextToken')
            if token is None:
                return names
            if not isinstance(token, str) or not token or token in seen:
                redact.refuse('inventory_unverified')
            seen.add(token)
            args = {'NextToken': token}

    @redact.guarded
    def redaction_candidates(self, salt):
        self._redaction_admin()
        return sorted((self.redaction_target(kind, name, salt) for kind in ('ssm', 'encrypted-file')
                       for name in self._redaction_names(kind)), key=lambda item: (item['kind'], item['id']))

    def _redaction_name(self, target, salt):
        if (not isinstance(target, dict) or set(target) != {'kind', 'id'} or
                str(uuid.UUID(target['id'])) != target['id']):
            redact.refuse('invalid_redaction_scope')
        kind = target['kind']
        matches = [name for name in self._redaction_names(kind) if self.redaction_target(kind, name, salt) == target]
        if len(matches) > 1:
            redact.refuse('ambiguous_redaction_target')
        return matches[0] if matches else None

    def _ssm_versions(self, name, salt, ident):
        ssm, args, seen, versions = self.session.client('ssm'), {}, set(), {}
        parameter = self.parameter(name)
        while True:
            page = ssm.get_parameter_history(Name=parameter, WithDecryption=False, MaxResults=50, **args)
            for item in page['Parameters']:
                item.pop('Value', None)  # Ciphertext is not input to a receipt.
                version, modified = item['Version'], item['LastModifiedDate']
                if (item['Name'] != parameter or item['Type'] != 'SecureString' or type(version) is not int
                        or version < 1 or version in versions or not isinstance(modified, datetime)
                        or modified.tzinfo is None):
                    redact.refuse('inventory_unverified')
                versions[version] = redact.pin(salt, ident, 'parameter-version', version,
                                              modified.astimezone(timezone.utc).isoformat())
            token = page.get('NextToken')
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen:
                redact.refuse('inventory_unverified')
            seen.add(token)
            args = {'NextToken': token}
        current = ssm.get_parameter(Name=parameter, WithDecryption=False)['Parameter']
        current.pop('Value', None)
        if (not versions or current.get('Name') != parameter or current.get('Type') != 'SecureString'
                or current.get('Version') != max(versions) or
                redact.pin(salt, ident, 'parameter-version', current['Version'],
                           current['LastModifiedDate'].astimezone(timezone.utc).isoformat()) != versions[current['Version']]):
            redact.refuse('version_scope_changed')
        return sorted(versions.values())

    @redact.guarded
    def review_redaction(self, target, salt):
        self._redaction_admin()
        name = self._redaction_name(target, salt)
        if name is None:
            redact.refuse('object_not_found')
        kind, ident = target['kind'], target['id']
        if kind == 'ssm':
            versions = self._ssm_versions(name, salt, ident)
        else:
            versions = redact.secret_file_versions(self.session.client('s3'), self.binding['bucket'],
                self.binding['account'], self.binding['object_prefix'] + name, salt, ident)
        if not versions:
            redact.refuse('object_not_found')
        return {'format': 1, 'scope': redact.handle(salt, 'secret_store', self.store_id),
                'kind': kind, 'id': ident, 'versions': versions}

    @redact.guarded
    def apply_redaction(self, plan, salt, *, issuer_revoked=False, writers_quiesced=False, private_name=None):
        """Separate exact approval only. SSM has no conditional delete: isolate ALL writers first."""
        if issuer_revoked is not True:
            redact.refuse('issuer_revocation_required')
        if writers_quiesced is not True:
            redact.refuse('writer_isolation_required')
        if (not isinstance(plan, dict) or set(plan) != {'format', 'scope', 'kind', 'id', 'versions'}
                or type(plan['format']) is not int or plan['format'] != 1
                or plan['scope'] != redact.handle(salt, 'secret_store', self.store_id)):
            redact.refuse('invalid_redaction_scope')
        redact.validate_pins(plan['versions'])
        if not plan['versions']:
            redact.refuse('invalid_redaction_scope')
        self._redaction_admin()
        kind, ident = plan['kind'], plan['id']
        target = {'kind': kind, 'id': ident}
        name = self._redaction_name(target, salt)
        if private_name is not None:
            if self.redaction_target(kind, private_name, salt) != target:
                redact.refuse('invalid_redaction_scope')
            name = private_name
        if kind == 'ssm':
            if name is None:
                # DescribeParameters is not authoritative negative evidence; never claim
                # an uncertain deletion solely because the discovery index omitted it.
                redact.refuse('provider_locator_required')
            ssm, parameter = self.session.client('ssm'), self.parameter(name)
            try:
                versions = self._ssm_versions(name, salt, ident)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'ParameterNotFound':
                    raise
            else:
                if versions != plan['versions']:
                    redact.refuse('version_scope_changed')
                ssm.delete_parameter(Name=parameter)  # Entire parameter, not a single catalog revision.
            try:
                ssm.get_parameter(Name=parameter, WithDecryption=False)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'ParameterNotFound':
                    raise
            else:
                redact.refuse('provider_deletion_unverified')
        elif name is not None:
            redact.purge_secret_file(self.session.client('s3'), self.binding['bucket'], self.binding['account'],
                                     self.binding['object_prefix'] + name, salt, ident, plan['versions'])
        else:
            redact.verify_absent_secret_file(self.session.client('s3'), self.binding['bucket'], self.binding['account'])
        return {'status': 'provider_deletion_verified', 'kind': kind, 'id': ident,
                'versions': len(plan['versions']), 'issuer_revocation': 'owner_attested'}

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
            raise StorageRejected('SSM input must contain 1 to 4096 UTF-8 bytes')
        try:
            text = value.decode('utf-8')
        except UnicodeDecodeError:
            raise StorageRejected('SSM input must be UTF-8') from None
        ssm = self.session.client('ssm')
        if replace:
            existing = ssm.get_parameter(Name=parameter, WithDecryption=False)['Parameter']
            if existing.get('Type') != 'SecureString':
                raise StorageRejected('refusing to replace a parameter that is not SecureString')
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
            ssm, token, match, seen = self.session.client('ssm'), None, None, set()
            while True:
                request = {'Name': self.parameter(name), 'WithDecryption': False, 'MaxResults': 50}
                if token:
                    request['NextToken'] = token
                try:
                    response = ssm.get_parameter_history(**request)
                except ClientError as error:
                    if error.response.get('Error', {}).get('Code') == 'ParameterNotFound' and token is None:
                        return None
                    raise
                for entry in response.get('Parameters', []):
                    if (entry.get('Description') != 'MAVEN storage operation ' + operation_id
                            or entry.get('Type') != 'SecureString'):
                        continue
                    if match is not None:
                        raise StoreError('storage operation has ambiguous versions')
                    version = entry.get('Version')
                    if type(version) is not int or version < 1:
                        raise StoreError('storage operation has invalid version metadata')
                    match = self._receipt(name, operation_id, version)
                token = response.get('NextToken')
                if not token:
                    return match
                if not isinstance(token, str) or token in seen:
                    raise StoreError('storage history pagination did not advance')
                seen.add(token)
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
                raise StorageRejected('file input exceeds its reviewed byte limit')
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
            # Rely on the bucket default inspected during setup, including its exact KMS key.
            upload = s3.create_multipart_upload(
                Bucket=bucket, Key=object_key, ExpectedBucketOwner=self.binding['account'],
                Metadata={'maven-key-id': key_id, 'maven-operation': operation_id})
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

    def validate_reference(self, reference):
        """Preflight supported immutable references without reading bytes or spending grants."""
        from tools.memory.adapter_contracts import AdapterError
        from tools.memory.validation import MemoryError
        try:
            validate_consumer_ref(reference)
        except AdapterError as error:
            raise StoreError('unsupported provider; migrate the catalog to this store before approval'
                             if error.code == 'unsupported_secret_provider' else
                             'SSM reference must pin an exact version; explicitly replace the legacy catalog entry before approval') from None
        except MemoryError:
            raise StoreError('secret reference is invalid') from None
        if reference['provider'] == 'ssm':
            if not reference['name'].startswith(self.binding['parameter_prefix']):
                raise StoreError('secret reference belongs to another store')
            name, version = reference['name'][len(self.binding['parameter_prefix']):].split(':')
            valid_name(name)
            return
        if (reference['store_id'] != self.store_id or reference['bucket'] != self.binding['bucket']
                or not reference['name'].startswith(self.binding['object_prefix'])):
            raise StoreError('encrypted file reference belongs to another store')
        valid_name(reference['name'][len(self.binding['object_prefix']):])

    @contextmanager
    def _decrypt_file(self, name, *, reference=None):
        """One authenticated streaming path for both bounded consumers and explicit restore."""
        import aws_encryption_sdk
        from aws_encryption_sdk import CommitmentPolicy
        valid_name(name)
        query = {'Bucket': self.binding['bucket'], 'Key': self.binding['object_prefix'] + name,
                 'ExpectedBucketOwner': self.binding['account']}
        if reference is not None and reference.get('version_id'):
            query['VersionId'] = reference['version_id']
        response = self.session.client('s3').get_object(**query)
        with closing(response['Body']) as body:
            key_id = response.get('Metadata', {}).get('maven-key-id', '')
            if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
                raise StoreError('encrypted file key identifier is invalid')
            if reference is not None and key_id != reference['key_id']:
                raise StoreError('encrypted file key metadata changed')
            keyring = file_keyring(self.session.client('ssm'), key_id, values=self.values)
            client = aws_encryption_sdk.EncryptionSDKClient(
                commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT)
            decrypted = client.stream(mode='d', source=body, source_length=response['ContentLength'],
                                      keyring=keyring,
                                      encryption_context={'maven-object': name, 'maven-key-id': key_id})
            try:
                yield decrypted
                decrypted.close()  # Authentication/finalization must succeed before publication.
            finally:
                # On failed authentication, abort the IO stream without the SDK context
                # manager logging a second footer error or retrying it at finalization.
                io.IOBase.close(decrypted)

    def restore_file(self, name, destination):
        """Stream to a private temporary file; publish only authenticated plaintext, never overwrite."""
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise StoreError('destination exists; choose a new path')
        temp_path = None
        try:
            with self._decrypt_file(name) as decrypted:
                descriptor, temp_path = tempfile.mkstemp(prefix='.maven-restore-', dir=destination.parent)
                with os.fdopen(descriptor, 'wb') as output:
                    while chunk := decrypted.read(1024 ** 2):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                raise StoreError('destination exists; choose a new path') from None
        finally:
            if temp_path is not None:
                os.unlink(temp_path)

    def read_secret(self, reference):
        """Private runner input only. Never return this method's result through MCP."""
        self.validate_reference(reference)
        if reference['provider'] == 'ssm':
            item = self.session.client('ssm').get_parameter(Name=reference['name'], WithDecryption=True)['Parameter']
            if item.get('Type') != 'SecureString' or item.get('Version') != int(reference['name'].rsplit(':', 1)[1]):
                raise StoreError('secret reference version or type changed')
            result = item['Value'].encode('utf-8')
            if not 0 < len(result) <= MAX_VALUE_BYTES:
                raise StoreError('secret exceeds the consumer byte limit')
            return result
        name = reference['name'][len(self.binding['object_prefix']):]
        with self._decrypt_file(name, reference=reference) as decrypted:
            value = decrypted.read(MAX_CONSUMER_BYTES + 1)
            if len(value) > MAX_CONSUMER_BYTES or decrypted.read(1):
                raise StoreError('encrypted file exceeds the consumer byte limit')
            return value
