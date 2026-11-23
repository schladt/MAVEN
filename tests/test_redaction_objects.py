"""Physical-provider contract. All objects, credentials and retained copies are synthetic."""
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from botocore.exceptions import ClientError

from tools.aws.secret_store import SecretStore
from tools.memory import redaction_objects as obj, skills, store
from tools.memory.client import MemoryClientError


SALT, INSTANCE, EPOCH = (str(uuid4()) for _ in range(3))
ACCOUNT = '123456789012'
CONFIG = {'admin_role_arn': f'arn:aws:iam::{ACCOUNT}:role/synthetic-admin', 'instance_id': INSTANCE,
          'recovery_epoch': EPOCH, 'journal_bucket': 'private-journal', 'skill_bucket': 'private-bundle'}
VALUES = {'MAVEN_ACCOUNT_ID': ACCOUNT, 'MAVEN_AWS_REGION': 'us-east-1',
          'MAVEN_MEMORY_ADMIN_ROLE_ARN': CONFIG['admin_role_arn'],
          'MAVEN_SECRETS_BUCKET': 'synthetic-vault', 'MAVEN_SECRETS_KEY_ID': '0123456789abcdef',
          'MAVEN_SECRETS_PARAMETER_PREFIX': '/test/values/', 'MAVEN_SECRETS_FILE_KEY_PREFIX': '/test/keys/',
          'MAVEN_SECRETS_OBJECT_PREFIX': 'ciphertext/'}
SHA = hashlib.sha256(b'synthetic archive').hexdigest()
KEY = f'skills/{INSTANCE}/bundles/{SHA}.zip'
NOW = datetime.now(timezone.utc)


def error(code):
    return ClientError({'Error': {'Code': code, 'Message': 'private-provider-prose'}}, 'PrivateOperation')


class S3:
    def __init__(self):
        self.rows, self.uploads, self.deleted, self.replication = {}, {}, [], False
        self.partial = self.locked = False
        self.hold, self.race = set(), None
        self.counter = 0
        self.session = Mock()
        self.session.client.return_value = self

    def add(self, key, raw, *, bucket='private-bundle', marker=False, metadata=None, tags=None):
        self.counter += 1
        version = 'private-v' + str(self.counter)
        self.rows.setdefault((bucket, key), []).insert(0, (version, raw, marker, metadata or {}, tags or []))
        return version

    def get_bucket_versioning(self, **args):
        assert args['ExpectedBucketOwner'] == ACCOUNT
        return {'Status': 'Enabled'}

    def get_bucket_tagging(self, **args):
        return {'TagSet': [{'Key': k, 'Value': v} for k, v in {
            'MavenInstanceId': INSTANCE, 'DataScope': 'owner_memory', 'MavenExclusive': 'true'}.items()]}

    def get_bucket_replication(self, **args):
        if self.replication:
            return {'ReplicationConfiguration': {'Rules': [{}]}}
        raise error('ReplicationConfigurationNotFoundError')

    def get_object_lock_configuration(self, **args):
        if self.locked:
            return {'ObjectLockConfiguration': {'ObjectLockEnabled': 'Enabled'}}
        raise error('ObjectLockConfigurationNotFoundError')

    def get_object_legal_hold(self, VersionId, **args):
        return {'LegalHold': {'Status': 'ON' if VersionId in self.hold else 'OFF'}}

    def get_object_retention(self, **args):
        return {'Retention': {}}

    def list_object_versions(self, Bucket, ExpectedBucketOwner, Prefix, KeyMarker=None, VersionIdMarker=None):
        assert ExpectedBucketOwner == ACCOUNT
        # Exercise a key+version continuation, including entries sharing the same key.
        rows = [(key, idx, row) for (bucket, key), versions in sorted(self.rows.items())
                if bucket == Bucket and key.startswith(Prefix) for idx, row in enumerate(versions)]
        if KeyMarker:
            start = next(i + 1 for i, (key, _, row) in enumerate(rows) if (key, row[0]) == (KeyMarker, VersionIdMarker))
            rows = rows[start:]
        result = {'IsTruncated': len(rows) > 2}
        for key, idx, (version, raw, marker, _, _) in rows[:2]:
            item = {'Key': key, 'VersionId': version, 'LastModified': NOW, 'IsLatest': idx == 0}
            if not marker:
                item.update(ETag='"' + hashlib.sha256(raw).hexdigest() + '"', Size=len(raw))
            result.setdefault('DeleteMarkers' if marker else 'Versions', []).append(item)
            if result['IsTruncated']:
                result.update(NextKeyMarker=key, NextVersionIdMarker=version)
        return result

    def list_multipart_uploads(self, Bucket, Prefix, **args):
        return {'IsTruncated': False, 'Uploads': [{'Key': key, 'UploadId': upload, 'Initiated': NOW}
                for (bucket, key), upload in self.uploads.items() if bucket == Bucket and key.startswith(Prefix)]}

    def abort_multipart_upload(self, Bucket, Key, UploadId, **args):
        assert self.uploads.pop((Bucket, Key)) == UploadId

    def list_parts(self, Bucket, Key, UploadId, **args):
        if self.uploads.get((Bucket, Key)) == UploadId:
            return {'IsTruncated': False, 'Parts': []}
        raise error('NoSuchUpload')

    def head_object(self, Bucket, Key, VersionId=None, **args):
        rows = self.rows[Bucket, Key]
        version, raw, marker, meta, tags = next(row for row in rows if VersionId is None or row[0] == VersionId)
        assert not marker
        return {'VersionId': version, 'ContentLength': len(raw), 'ETag': '"' + hashlib.sha256(raw).hexdigest() + '"', 'Metadata': meta}

    def get_object(self, Bucket, Key, VersionId, **args):
        row = next(row for row in self.rows[Bucket, Key] if row[0] == VersionId)
        return {'Body': BytesIO(row[1]), 'VersionId': VersionId}

    def get_object_tagging(self, Bucket, Key, VersionId, **args):
        return {'TagSet': next(row[4] for row in self.rows[Bucket, Key] if row[0] == VersionId)}

    def put_object(self, Bucket, Key, ExpectedBucketOwner, Body, IfMatch=None, IfNoneMatch=None, **args):
        assert ExpectedBucketOwner == ACCOUNT
        rows = self.rows.get((Bucket, Key), [])
        if IfMatch is not None:
            if not rows or rows[0][2] or IfMatch != self.head_object(Bucket, Key)['ETag']:
                raise error('PreconditionFailed')
        elif IfNoneMatch != '*' or rows and not rows[0][2]:
            raise error('PreconditionFailed')
        assert Body == b'' and args['Tagging'] == 'maven-redacted=true'
        self.add(Key, Body, bucket=Bucket, metadata=args['Metadata'], tags=[{'Key': 'maven-redacted', 'Value': 'true'}])

    def delete_object(self, Bucket, Key, ExpectedBucketOwner, VersionId, IfMatch=None):
        assert ExpectedBucketOwner == ACCOUNT
        if VersionId == 'null':
            assert IfMatch == self.head_object(Bucket, Key, VersionId)['ETag']
        self.rows[Bucket, Key] = [r for r in self.rows[Bucket, Key] if r[0] != VersionId]
        self.deleted.append(VersionId)
        return {}

    def delete_objects(self, Bucket, ExpectedBucketOwner, Delete):
        assert ExpectedBucketOwner == ACCOUNT
        removed = []
        for row in Delete['Objects']:
            key, version = row['Key'], row['VersionId']
            self.rows[Bucket, key] = [r for r in self.rows[Bucket, key] if r[0] != version]
            removed.append(row)
            self.deleted.append(version)
            if self.partial:
                self.partial = False
                return {'Deleted': removed, 'Errors': [{'Message': 'private-provider-prose'}]}
        if self.race:
            self.add(self.race, b'new unreviewed version', bucket=Bucket)
            self.race = None
        return {'Deleted': removed}


class ObjectPurgeTests(unittest.TestCase):
    def setUp(self):
        self.s3 = S3()
        self.session = self.s3.session
        self.target = obj.target(CONFIG, SALT, 'skill_bundle', SHA)

    def review(self, target=None):
        return obj.review(CONFIG, self.session, SALT, [target or self.target])

    def apply(self, plan):
        return obj.apply(CONFIG, self.session, SALT, plan)

    def test_pinned_orphan_permanent_purge_and_resume_after_sql_or_partial_delete(self):
        originals = [self.s3.add(KEY, b'synthetic-sensitive') for _ in range(3)]
        originals.append(self.s3.add(KEY, b'', marker=True))
        self.s3.add(KEY + '.other', b'shared unrelated artifact')
        self.s3.uploads['private-bundle', KEY] = 'private-upload-id'
        self.assertIn(self.target, obj.candidates(CONFIG, self.session, SALT))
        plan = self.review()
        # Snapshot a content-free job, then discard all SQL locators as if the SQL
        # commit and JVM/process death occurred before a single S3 mutation.
        saved = json.dumps(plan)
        for private in [SHA, KEY, 'synthetic-sensitive', *originals, 'private-upload-id']:
            self.assertNotIn(private, saved)
        self.s3.partial = True
        with self.assertRaisesRegex(obj.ObjectRedactionError, '^partial_provider_deletion$'):
            self.apply(json.loads(saved))
        receipt = self.apply(json.loads(saved))
        self.assertEqual(receipt['objects'][0]['versions'], 5)
        self.assertEqual(self.apply(json.loads(saved)), receipt)
        self.assertCountEqual(self.s3.deleted, originals)
        self.assertEqual(len(self.s3.rows['private-bundle', KEY]), 1)
        self.assertEqual(self.s3.rows['private-bundle', KEY][0][1], b'')
        self.assertEqual(self.s3.rows['private-bundle', KEY + '.other'][0][1], b'shared unrelated artifact')
        with self.assertRaises(ClientError):
            self.s3.put_object(Bucket='private-bundle', Key=KEY, ExpectedBucketOwner=ACCOUNT,
                               Body=b'synthetic-sensitive', IfNoneMatch='*')

    def test_never_uploaded_reference_gets_fence_while_locator_still_available(self):
        private = {self.target['id']: SHA}
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'object_not_found'):
            self.review()
        plan = obj.review(CONFIG, self.session, SALT, [self.target], missing_keys=private)
        self.assertNotIn(SHA, json.dumps(plan))
        self.assertFalse(self.s3.rows)
        obj.prepare_missing(CONFIG, self.session, SALT, plan, private)
        # No locator required after SQL removal, even when guard write reply is lost.
        self.assertEqual(self.apply(plan)['status'], 'objects_verified_purged')
        obj.prepare_missing(CONFIG, self.session, SALT, plan, private)
        self.assertEqual(len(self.s3.rows['private-bundle', KEY]), 1)

    def test_shared_reference_closure_yields_one_physical_job(self):
        self.s3.add(KEY, b'shared bytes')
        # Both SQL refs derive the identical opaque ID; duplicates must not be
        # interpreted as separate independent deletion approvals.
        same = obj.target(CONFIG, SALT, 'skill_bundle', SHA)
        self.assertEqual(same, self.target)
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'invalid_redaction_scope'):
            obj.review(CONFIG, self.session, SALT, [self.target, same])
        self.assertFalse(self.s3.deleted)

    def test_new_version_after_review_and_during_purge_cannot_be_deleted(self):
        self.s3.add(KEY, b'sensitive')
        plan = self.review()
        new = self.s3.add(KEY, b'new')
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'version_scope_changed'):
            self.apply(plan)
        self.assertNotIn(new, self.s3.deleted)
        plan = self.review()
        self.s3.race = KEY
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'version_scope_changed'):
            self.apply(plan)
        self.assertEqual(self.s3.rows['private-bundle', KEY][0][1], b'new unreviewed version')

    def test_pagination_and_provider_scope_fail_without_partial_inventory(self):
        for page in ({'IsTruncated': True, 'NextKeyMarker': KEY, 'NextVersionIdMarker': 'repeat'},
                     {'IsTruncated': False, 'Versions': [{'Key': 'foreign-prefix/private'}]}):
            with self.subTest(page=page), patch.object(self.s3, 'list_object_versions', return_value=page):
                with self.assertRaisesRegex(obj.ObjectRedactionError, '^inventory_unverified$'):
                    self.review()
        with patch.object(self.s3, 'get_bucket_replication', side_effect=error('AccessDenied')):
            self.s3.add(KEY, b'sensitive')
            with self.assertRaisesRegex(obj.ObjectRedactionError, '^provider_unverified$'):
                self.review()
        self.assertFalse(self.s3.deleted)

    def test_held_versions_receive_read_write_fence_then_resume_only_after_release(self):
        version = self.s3.add(KEY, b'sensitive')
        self.s3.locked = True
        self.s3.hold.add(version)
        plan = self.review()
        self.assertEqual(plan['objects'][0]['blocked'], 'object_retained')
        with self.assertRaisesRegex(obj.ObjectRedactionError, '^object_retained$'):
            self.apply(plan)
        self.assertFalse(self.s3.deleted)
        self.assertEqual(self.s3.rows['private-bundle', KEY][0][1], b'')
        self.s3.hold.clear()
        self.apply(plan)
        self.assertEqual(self.s3.deleted, [version])

    def test_disabled_replication_rule_is_not_evidence_of_replica_deletion(self):
        self.s3.add(KEY, b'sensitive')
        self.s3.replication = True
        plan = self.review()
        self.assertEqual(plan['objects'][0]['blocked'], 'replication_unverified')
        self.s3.replication = False
        for action in (obj.apply, obj.restore):
            with self.assertRaisesRegex(obj.ObjectRedactionError, 'replication_unverified'):
                action(CONFIG, self.session, SALT, plan)
        self.assertFalse(self.s3.deleted)

    def test_recovery_preserves_fence_and_ledger_and_validates_exclusive_owner(self):
        bucket = CONFIG['journal_bucket']
        raw = json.dumps(store.control_document(CONFIG, 'maintenance')).encode()
        old = self.s3.add('control.json', b'synthetic-contamination', bucket=bucket)
        good = self.s3.add('control.json', raw, bucket=bucket)
        forever = self.s3.add('redaction-ledger/private', b'control', bucket=bucket)
        bad_key = 'private sensitive name'
        self.s3.add(bad_key, b'synthetic-secret', bucket=bucket)
        for candidate in ('redaction-ledger/private', 'narrow-erases/private', 'uninstall/foreign/manifest'):
            with self.assertRaisesRegex(obj.ObjectRedactionError, 'unmanaged_or_protected_object'):
                obj.target(CONFIG, SALT, 'recovery_object', candidate)
        plan = self.review(obj.target(CONFIG, SALT, 'recovery_object', 'control.json'))
        self.s3.add('control.json', raw, bucket=bucket)  # New valid isolation fence.
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'version_scope_changed'):
            self.apply(plan)
        obj.restore(CONFIG, self.session, SALT, plan)
        obj.restore(CONFIG, self.session, SALT, plan)
        self.assertEqual(self.s3.deleted, [old])
        orphan = self.review(obj.target(CONFIG, SALT, 'recovery_object', bad_key))
        self.assertNotIn(bad_key, json.dumps(orphan))
        with patch.object(self.s3, 'get_bucket_tagging', return_value={'TagSet': []}):
            with self.assertRaisesRegex(obj.ObjectRedactionError, 'unmanaged_or_protected_object'):
                self.apply(orphan)
        self.apply(orphan)
        self.assertNotIn(forever, self.s3.deleted)
        self.assertNotIn(good, self.s3.deleted)

    def test_skill_download_never_reads_terminal_body(self):
        body, s3 = Mock(), Mock()
        s3.get_object.return_value = {'Body': body, 'ContentLength': 0, 'Metadata': {'maven-redacted': 'true'}}
        with patch.object(skills, '_storage', return_value=(s3, 'bucket', 'key')):
            with self.assertRaises(MemoryClientError) as ctx:
                skills._download(Mock(), {'size_bytes': 1})
        self.assertEqual(ctx.exception.code, 'skill_bundle_redacted')
        body.read.assert_not_called()
        body.close.assert_called_once()


class SecretProviderTests(unittest.TestCase):
    def setUp(self):
        self.s3, self.ssm = S3(), Mock()
        self.session = Mock()
        sts = Mock()
        sts.get_caller_identity.return_value = {
            'Account': ACCOUNT, 'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/synthetic-admin/test'}
        self.session.region_name = VALUES['MAVEN_AWS_REGION']
        self.session.client.side_effect = {'ssm': self.ssm, 's3': self.s3, 'sts': sts}.__getitem__
        self.store = SecretStore(VALUES, self.session)
        self.name = 'private-name'
        self.param = self.store.parameter(self.name)
        self.versions = [1, 2]
        def item(v):
            return {'Name': self.param, 'Type': 'SecureString', 'LastModifiedDate': NOW, 'Version': v,
                    'Value': 'private-ciphertext-never-persist'}
        self.ssm.describe_parameters.side_effect = lambda **_: {'Parameters': [item(max(self.versions))] if self.versions else []}
        def history(Name, WithDecryption, MaxResults, NextToken=None):
            assert Name == self.param and not WithDecryption
            if not self.versions:
                raise error('ParameterNotFound')
            i = int(NextToken or 0)
            return {'Parameters': [item(self.versions[i])], **({'NextToken': str(i+1)} if i+1 < len(self.versions) else {})}
        def current(Name, WithDecryption):
            assert Name == self.param and not WithDecryption
            if not self.versions:
                raise error('ParameterNotFound')
            return {'Parameter': item(max(self.versions))}
        self.ssm.get_parameter_history.side_effect = history
        self.ssm.get_parameter.side_effect = current
        self.ssm.delete_parameter.side_effect = lambda **args: self.versions.clear()
        self.attested = {'writers_quiesced': True, 'issuer_revoked': True}

    def test_wrong_principal_or_region_cannot_inspect_or_delete(self):
        target = self.store.redaction_target('ssm', self.name, SALT)
        plan = self.store.review_redaction(target, SALT)
        self.session.client('sts').get_caller_identity.return_value = {
            'Account': ACCOUNT, 'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/daily/test'}
        with self.assertRaises(obj.ObjectRedactionError):
            self.store.apply_redaction(plan, SALT, **self.attested)
        self.session.region_name = 'us-west-2'
        with self.assertRaises(obj.ObjectRedactionError):
            self.store.redaction_candidates(SALT)
        self.ssm.delete_parameter.assert_not_called()

    def test_exact_ssm_approval_and_post_delete_reconciliation(self):
        target = self.store.redaction_target('ssm', self.name, SALT)
        plan = self.store.review_redaction(target, SALT)
        self.assertNotIn(self.name, json.dumps(plan))
        self.assertNotIn('ciphertext', json.dumps(plan))
        for flags in ({}, {'issuer_revoked': True}):
            with self.assertRaises(obj.ObjectRedactionError):
                self.store.apply_redaction(plan, SALT, **flags)
        self.versions.append(3)
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'version_scope_changed'):
            self.store.apply_redaction(plan, SALT, **self.attested)
        self.ssm.delete_parameter.assert_not_called()
        plan = self.store.review_redaction(target, SALT)
        receipt = self.store.apply_redaction(plan, SALT, **self.attested)
        self.assertEqual(receipt['versions'], 3)
        with self.assertRaisesRegex(obj.ObjectRedactionError, 'provider_locator_required'):
            self.store.apply_redaction(plan, SALT, **self.attested)
        self.assertEqual(self.store.apply_redaction(plan, SALT, private_name=self.name, **self.attested), receipt)
        self.ssm.delete_parameter.assert_called_once_with(Name=self.param)

    def test_unversioned_encrypted_file_uses_exact_null_version_and_etag(self):
        bucket = self.store.binding['bucket']
        key = self.store.binding['object_prefix'] + self.name
        self.s3.rows[bucket, key] = [('null', b'sensitive encrypted bytes', False, {}, [])]
        target = self.store.redaction_target('encrypted-file', self.name, SALT)
        with patch.object(self.s3, 'get_bucket_versioning', return_value={}):
            plan = self.store.review_redaction(target, SALT)
            self.store.apply_redaction(plan, SALT, **self.attested)
        self.assertEqual(self.s3.deleted, ['null'])

    def test_encrypted_file_all_versions_not_shared_wrapping_key(self):
        bucket = self.store.binding['bucket']
        key = self.store.binding['object_prefix'] + self.name
        for raw in (b'ciphertext old', b'ciphertext new'):
            self.s3.add(key, raw, bucket=bucket)
        self.s3.add(key, b'', bucket=bucket, marker=True)
        target = self.store.redaction_target('encrypted-file', self.name, SALT)
        plan = self.store.review_redaction(target, SALT)
        self.assertNotIn(key, json.dumps(plan))
        self.assertEqual(self.store.apply_redaction(plan, SALT, **self.attested)['versions'], 3)
        self.store.apply_redaction(plan, SALT, **self.attested)
        self.assertEqual(self.s3.rows[bucket, key], [])
        self.ssm.delete_parameter.assert_not_called()
