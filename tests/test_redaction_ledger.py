"""Synthetic S3 conditionals and physical cluster identities. No AWS sessions."""
import io
import unittest
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from tools.memory import redaction_ledger as ledger, redaction_recovery as recovery
from tools.memory.store import Store, control_document
from tools.memory.validation import MemoryError, canonical, digest
from test_memory_service import CONFIG


def uid():
    return str(uuid4())


class Objects:
    def __init__(self):
        self.bytes = {}
        self.fail_seal = False
        self.close = Mock()

    def put_object(self, *, Key, Body, IfNoneMatch=None, IfMatch=None, **kwargs):
        previous = self.bytes.get(Key)
        if (IfNoneMatch == '*' and previous is not None) or (IfMatch and self.etag(previous) != IfMatch):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        if self.fail_seal and '/seals/' in Key:
            raise RuntimeError('injected write failure')
        self.bytes[Key] = Body
        return {'ETag': self.etag(Body)}

    @staticmethod
    def etag(body):
        import hashlib
        return '"' + hashlib.sha256(body).hexdigest() + '"'

    def get_object(self, *, Key, **kwargs):
        return {'Body': io.BytesIO(self.bytes[Key]), 'ETag': self.etag(self.bytes[Key])}

    def list_objects_v2(self, *, Prefix, MaxKeys=1000, StartAfter='', **kwargs):
        keys = sorted(k for k in self.bytes if k.startswith(Prefix) and k > StartAfter)
        rows = [{'Key': k, 'Size': len(self.bytes[k])} for k in keys[:MaxKeys]]
        return {'Contents': rows, 'KeyCount': len(rows), 'IsTruncated': len(keys) > MaxKeys}


class CheckpointDB:
    def __init__(self):
        self.mark = None
        self.salt = uid()

    def scalar(self, sql, values=None, transaction=None):
        if sql.startswith('SELECT true FROM maven.service_state'):
            return self.mark is None
        if sql.startswith('SELECT redaction_checkpoint'):
            return self.mark
        if sql.startswith('SELECT maven.redaction_salt()'):
            return self.salt
        if sql.startswith('UPDATE maven.redaction_identity'):
            self.salt = values['salt']
            return True
        if sql.startswith('UPDATE maven.service_state SET redaction_checkpoint'):
            if self.mark != values['old']:
                return None
            self.mark = values['new']
            return True
        raise AssertionError(sql)


def rds(config, resource='cluster-ABCDEF123456'):
    client = Mock()
    client.describe_db_clusters.return_value = {'DBClusters': [{'DBClusterArn': config['cluster_arn'],
        'DbClusterResourceId': resource, 'Engine': 'aurora-postgresql', 'Status': 'available',
        'HttpEndpointEnabled': True, 'IAMDatabaseAuthenticationEnabled': False,
        'VpcSecurityGroups': [{'VpcSecurityGroupId': 'sg-fixture', 'Status': 'active'}]}]}
    return client


class IndependentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s3, self.job = CheckpointDB(), Objects(), uid()
        self.rds = rds(CONFIG)
        self.physical = ledger.cluster_identity(self.rds, CONFIG)
        self.root = ledger.genesis(self.db, 'tx', self.s3, CONFIG, self.physical)
        self.plan = {'format': 1, 'scope': uid(), 'objects': []}
        self.targets = [{'kind': 'project_record', 'id': uid(), 'project_id': uid()}]
        self.args = (self.db, 'tx', self.s3, CONFIG, self.physical)

    def append(self):
        return ledger.record_applied(*self.args, self.job, self.targets, 'a' * 64, object_plan=self.plan)

    def test_partial_publication_and_aborted_commit_accept_only_same_job_and_manifest(self):
        self.s3.fail_seal = True
        with self.assertRaises(RuntimeError):
            self.append()
        self.assertEqual(self.db.mark, self.root)
        with self.assertRaises(MemoryError):
            ledger.latest(self.s3, CONFIG)
        with self.assertRaises(MemoryError):
            ledger.verify(*self.args, job=uid(), plan_digest='a' * 64)
        self.s3.fail_seal = False
        checkpoint = self.append()
        self.db.mark = self.root  # crash after the S3 seal but before SQL commit
        with self.assertRaises(MemoryError):
            ledger.verify(*self.args)
        self.assertEqual(self.append(), checkpoint)
        self.assertEqual(self.db.mark, checkpoint)
        self.assertEqual(len(list(ledger.history(self.s3, CONFIG, ledger.latest(self.s3, CONFIG)))), 2)

    def test_truncated_or_corrupt_history_cannot_promote_an_old_checkpoint(self):
        self.append()
        tip = ledger.latest(self.s3, CONFIG)
        key = ledger._key(CONFIG, tip['sequence'])
        old = self.s3.bytes.pop(key)
        with self.assertRaises(MemoryError):
            ledger.latest(self.s3, CONFIG)  # separately listed seal detects complete suffix loss
        self.s3.bytes[key] = old
        self.s3.bytes[ledger._key(CONFIG, 0)] = canonical({**ledger._read(self.s3, CONFIG, 0), 'salt': uid()}).encode()
        with self.assertRaises(MemoryError):
            list(ledger.history(self.s3, CONFIG, tip))
        self.db.mark = None
        with self.assertRaises(MemoryError):
            ledger.genesis(*self.args)  # a missing SQL mark never resets a nonempty log

    def test_stale_arn_or_missing_control_does_not_use_application_credentials(self):
        self.s3.bytes['control.json'] = canonical(control_document(CONFIG, 'ready', redaction_bound=True)).encode()
        data = Mock()
        store = Store(CONFIG, data, self.s3, rds(CONFIG, 'cluster-DIFFERENT1234'))
        with self.assertRaises(MemoryError):
            store.perform('/v1/records/query', {'record_id': uid()}, 'fixture')
        self.s3.bytes = {'control.json': self.s3.bytes['control.json']}
        with self.assertRaises(MemoryError):
            Store(CONFIG, data, self.s3, self.rds).perform('/v1/records/query', {'record_id': uid()}, 'fixture')
        data.begin_transaction.assert_not_called()
        data.execute_statement.assert_not_called()

    def test_no_unknown_fields_or_provider_locators_cross_append_boundary(self):
        original = dict(self.s3.bytes)
        for target in ({'kind': 'note-body', 'id': uid()}, {'kind': 'project_record', 'id': uid()},
                       {'kind': 'record', 'id': uid(), 'reason': 'private text'}):
            self.targets = [target]
            with self.assertRaises(MemoryError):
                self.append()
        self.targets = [{'kind': 'record', 'id': uid()}]
        self.plan['objects'] = [{'kind': 'recovery_object', 'id': uid(), 'class': 'orphan',
                                 'versions': ['version-id-in-clear'], 'preserve': None}]
        with self.assertRaises(MemoryError):
            self.append()
        self.assertEqual(self.s3.bytes, original)

    def test_only_allowlisted_provider_block_codes_can_be_preserved(self):
        target = {'kind': 'recovery_object', 'id': uid()}
        self.targets = [target]
        obj = {**target, 'class': 'orphan', 'versions': ['c' * 64], 'preserve': None, 'blocked': 'private text'}
        self.plan['objects'] = [obj]
        with self.assertRaises(MemoryError):
            self.append()
        obj['blocked'] = 'object_retained'
        self.assertEqual(self.append(), digest(ledger.latest(self.s3, CONFIG)))

    def test_open_network_blocks_restore_before_sql_or_login(self):
        ec2 = Mock()
        ec2.describe_security_groups.return_value = {'SecurityGroups': [{'GroupId': 'sg-fixture', 'IpPermissions': [{}]}]}
        with self.assertRaises(MemoryError):
            recovery._isolated({'rds': self.rds, 'ec2': ec2}.__getitem__, CONFIG)
