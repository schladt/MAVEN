"""Stateful AWS-boundary regressions; no AWS credentials, network or live resources."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from tools.uninstall import cloud
from tools.uninstall.common import UninstallError, digest


class AwsError(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class Clients:
    def __init__(self, **clients):
        self.clients = clients

    def client(self, name):
        return self.clients[name]


class SnapshotRds:
    def __init__(self):
        self.snapshot = None
        self.tags = []
        self.creations = 0

    def describe_db_cluster_snapshots(self, **kwargs):
        if self.snapshot is None:
            raise AwsError('DBClusterSnapshotNotFoundFault')
        return {'DBClusterSnapshots': [dict(self.snapshot)]}

    def create_db_cluster_snapshot(self, **kwargs):
        self.creations += 1
        self.tags = kwargs['Tags']
        self.snapshot = {'DBClusterIdentifier': kwargs['DBClusterIdentifier'],
                         'DBClusterSnapshotArn': 'arn:aws:rds:us-east-1:123456789012:cluster-snapshot:recovery',
                         'Status': 'creating', 'KmsKeyId': 'key'}

    def list_tags_for_resource(self, **kwargs):
        return {'TagList': self.tags}


class VersionedS3:
    def __init__(self):
        self.exists = True
        self.fail_after_delete = True
        self.objects = [
            {'key': 'skill.zip', 'version': 'null', 'marker': False, 'etag': 'abc', 'size': 7},
            {'key': 'skill.zip', 'version': 'v2', 'marker': True, 'etag': None, 'size': None},
        ]
        self.deleted = []

    def head_bucket(self, **kwargs):
        if not self.exists:
            raise AwsError('NoSuchBucket')
        return {}

    def list_object_versions(self, **kwargs):
        return {'Versions': [{'Key': item['key'], 'VersionId': item['version'], 'ETag': item['etag'], 'Size': item['size']} for item in self.objects if not item['marker']],
                'DeleteMarkers': [{'Key': item['key'], 'VersionId': item['version']} for item in self.objects if item['marker']]}

    def list_multipart_uploads(self, **kwargs):
        return {'Uploads': []}

    def get_bucket_encryption(self, **kwargs):
        return {'ServerSideEncryptionConfiguration': {'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}}

    def get_bucket_tagging(self, **kwargs):
        return {'TagSet': []}

    def delete_objects(self, **kwargs):
        identities = {(item['Key'], item['VersionId']) for item in kwargs['Delete']['Objects']}
        self.deleted.extend(kwargs['Delete']['Objects'])
        self.objects = [item for item in self.objects if (item['key'], item['version']) not in identities]
        if self.fail_after_delete:
            self.fail_after_delete = False
            raise TimeoutError('response lost after the effect')
        return {}

    def delete_bucket(self, **kwargs):
        assert not self.objects
        self.exists = False
        return {}


class DatabaseRds:
    def __init__(self):
        self.writer = 'available'
        self.cluster = 'available'
        self.protection = True
        self.calls = []

    def describe_db_instances(self, **kwargs):
        if self.writer is None:
            raise AwsError('DBInstanceNotFound')
        return {'DBInstances': [{'DBClusterIdentifier': 'cluster', 'DBInstanceStatus': self.writer,
                                 'DBInstanceArn': 'writer-arn', 'DbiResourceId': 'immutable-writer'}]}

    def describe_db_clusters(self, **kwargs):
        if self.cluster is None:
            raise AwsError('DBClusterNotFoundFault')
        return {'DBClusters': [{'DBClusterIdentifier': 'cluster', 'DBClusterArn': 'cluster-arn',
                                'DbClusterResourceId': 'immutable-cluster', 'DeletionProtection': self.protection,
                                'Status': self.cluster,
                                'DBClusterMembers': [{'DBInstanceIdentifier': 'writer'}] if self.writer else []}]}

    def delete_db_instance(self, **kwargs):
        assert kwargs['DeleteAutomatedBackups'] is False
        self.calls.append('delete-writer')
        self.writer = 'deleting'
        return {}

    def modify_db_cluster(self, **kwargs):
        self.calls.append('disable-protection')
        self.protection = False
        return {}

    def delete_db_cluster(self, **kwargs):
        assert kwargs['DeleteAutomatedBackups'] is False
        assert self.writer is None
        assert not self.protection
        self.calls.append('delete-cluster')
        self.cluster = 'deleting'
        return {}


class ScheduledSecret:
    def __init__(self):
        self.present = True
        self.scheduled = False
        self.deletes = 0
        self.date = datetime(2026, 11, 15, tzinfo=timezone.utc)

    def describe_secret(self, **kwargs):
        if not self.present:
            raise AwsError('ResourceNotFoundException')
        result = {'ARN': kwargs['SecretId']}
        if self.scheduled:
            result['DeletedDate'] = self.date
        return result

    def delete_secret(self, **kwargs):
        assert kwargs['RecoveryWindowInDays'] == 7
        self.scheduled = True
        self.deletes += 1
        return {'DeletionDate': self.date}


class CloudUninstallTests(unittest.TestCase):
    def test_snapshot_pending_resumes_without_duplicate_and_rejects_foreign_snapshot(self):
        rds = SnapshotRds()
        provider = Clients(rds=rds)
        details = {'snapshot_id': 'recovery', 'instance_id': 'instance',
                   'cluster': {'DBClusterIdentifier': 'cluster', 'KmsKeyId': 'key'}}
        progress, persisted = {}, []
        save = lambda: persisted.append(copy.deepcopy(progress))
        operation = lambda entry: cloud._snapshot(provider, details, entry, save)
        with self.assertRaisesRegex(UninstallError, 'pending'):
            cloud._step(progress, save, 'snapshot', operation)
        self.assertEqual(rds.creations, 1)
        self.assertTrue(persisted[0]['steps']['snapshot']['intent'])
        rds.snapshot['Status'] = 'available'
        result = cloud._step(progress, save, 'snapshot', operation)
        self.assertEqual(result['Status'], 'available')
        self.assertEqual(rds.creations, 1)
        rds.tags = [{'Key': 'MavenUninstall', 'Value': digest({'different': 'plan'})}]
        with self.assertRaisesRegex(UninstallError, 'exact plan'):
            cloud._snapshot(provider, details, {}, save)

    def test_bucket_purge_reconciles_lost_response_and_protects_new_versions(self):
        s3 = VersionedS3()
        provider = Clients(s3=s3)
        details = {'account': '123456789012', 'stack_outputs': {'JournalBucket': 'journal'}}
        expected = cloud._bucket(provider, 'skills', details['account'])
        progress, persisted = {}, []
        save = lambda: persisted.append(copy.deepcopy(progress))
        s3.objects[0]['etag'] = 'changed'
        with self.assertRaisesRegex(UninstallError, 'content changed'):
            cloud._purge_bucket(provider, expected, details, {}, save)
        s3.objects[0]['etag'] = 'abc'
        s3.objects.append({'key': 'new', 'version': 'v3', 'marker': False, 'etag': 'new', 'size': 1})
        with self.assertRaisesRegex(UninstallError, 'unreviewed'):
            cloud._purge_bucket(provider, expected, details, {}, save)
        self.assertFalse(s3.deleted)
        s3.objects.pop()
        operation = lambda entry: cloud._purge_bucket(provider, expected, details, entry, save)
        with self.assertRaises(TimeoutError):
            cloud._step(progress, save, 'bucket', operation)
        self.assertEqual({item['VersionId'] for item in s3.deleted}, {'null', 'v2'})
        self.assertFalse(progress['steps']['bucket'].get('done'))
        result = cloud._step(progress, save, 'bucket', operation)
        self.assertEqual(result['status'], 'removed')
        self.assertFalse(s3.exists)
        self.assertEqual(len(s3.deleted), 2)
        self.assertTrue(any(entry['steps']['bucket'].get('versions') for entry in persisted))

    def test_database_deletion_waits_for_writer_and_resumes_cluster_deletion(self):
        rds = DatabaseRds()
        provider = Clients(rds=rds)
        details = {'cluster': {'DBClusterIdentifier': 'cluster', 'DBClusterArn': 'cluster-arn', 'DbClusterResourceId': 'immutable-cluster'},
                   'instances': {'writer': {'arn': 'writer-arn', 'resource_id': 'immutable-writer'}},
                   'resources': [{'type': 'AWS::RDS::DBInstance', 'physical': 'writer'}]}
        progress, persisted = {}, []
        save = lambda: persisted.append(copy.deepcopy(progress))
        with self.assertRaisesRegex(UninstallError, 'instance deletion is pending'):
            cloud._delete_database(provider, details, progress, save)
        self.assertEqual(rds.calls, ['delete-writer'])
        rds.writer = None
        with self.assertRaisesRegex(UninstallError, 'cluster deletion is pending'):
            cloud._delete_database(provider, details, progress, save)
        self.assertEqual(rds.calls, ['delete-writer', 'disable-protection', 'delete-cluster'])
        rds.cluster = None
        cloud._delete_database(provider, details, progress, save)
        self.assertTrue(progress['steps']['cluster']['done'])
        self.assertEqual(rds.calls.count('delete-cluster'), 1)
        self.assertTrue(persisted)

    def test_resumed_stack_deletion_rejects_added_replaced_or_changed_resources(self):
        cf = Mock()
        resource = {'LogicalResourceId': 'Runtime', 'ResourceType': 'AWS::Lambda::Function',
                    'PhysicalResourceId': 'approved-function', 'ResourceStatus': 'CREATE_COMPLETE'}
        template = {'Resources': {'Runtime': {'Type': 'AWS::Lambda::Function'}}}
        cf.describe_stacks.return_value = {'Stacks': [{'StackId': 'stack-id',
            'StackStatus': 'DELETE_FAILED', 'Parameters': []}]}
        cf.get_template.return_value = {'TemplateBody': template}
        cf.list_stack_resources.return_value = {'StackResourceSummaries': [resource]}
        details = {'stack_id': 'stack-id', 'template': copy.deepcopy(template), 'stack_parameters': [],
                   'resources': [{'logical': 'Runtime', 'type': 'AWS::Lambda::Function',
                                  'physical': 'approved-function'}]}
        provider = Clients(cloudformation=cf)
        # An earlier invocation passed the initial check and persisted intent;
        # neither flag authorizes new resources discovered on a later resume.
        progress = {'validated': True, 'steps': {'stack': {'intent': True, 'delete_requested': True}}}
        mutations = (
            [resource, {'LogicalResourceId': 'Unreviewed', 'ResourceType': 'AWS::S3::Bucket',
                        'PhysicalResourceId': 'shared-data', 'ResourceStatus': 'CREATE_COMPLETE'}],
            [{**resource, 'PhysicalResourceId': 'replacement-function'}],
        )
        for inventory in mutations:
            with self.subTest(inventory=inventory):
                cf.list_stack_resources.return_value = {'StackResourceSummaries': inventory}
                with self.assertRaisesRegex(UninstallError, 'unreviewed|identity changed'):
                    cloud._delete_stack(provider, details, progress, lambda: None)
                cf.delete_stack.assert_not_called()
        cf.list_stack_resources.return_value = {'StackResourceSummaries': [resource]}
        cf.get_template.return_value = {'TemplateBody': {'Resources': {}}}
        with self.assertRaisesRegex(UninstallError, 'template changed'):
            cloud._delete_stack(provider, details, progress, lambda: None)
        cf.delete_stack.assert_not_called()

    def test_secret_recovery_window_is_pending_and_shared_optional_bucket_protected(self):
        secret = ScheduledSecret()
        provider = Clients(secretsmanager=secret)
        progress = {}
        save = lambda: None
        operation = lambda entry: cloud._schedule_secret(provider, 'secret-arn', entry, save)
        first = cloud._step(progress, save, 'secret', operation)
        self.assertEqual(first['status'], 'pending-recovery-window')
        again = cloud._step(progress, save, 'secret', operation)
        self.assertEqual(again['status'], 'pending-recovery-window')
        self.assertEqual(secret.deletes, 1)
        secret.present = False
        self.assertEqual(cloud._step(progress, save, 'secret', operation)['status'], 'removed')
        with patch.object(cloud, '_bucket', return_value={'name': 'shared', 'tags': {'MavenMemoryInstance': 'friendly-name'}}):
            resource, reason = cloud._optional(Clients(), 's3:shared', 'instance', '123456789012', 'us-east-1')
        self.assertIsNone(resource)
        self.assertIn('exclusive-instance ownership', reason)
