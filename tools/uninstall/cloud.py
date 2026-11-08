"""Exact-inventory cloud retirement and purge; no AWS imports on the local path.

The deployment login, never a stack-owned role, performs teardown. CloudFormation
owns runtime/IAM deletion; its deliberately retained database/network/data are
handled explicitly. Every asynchronous operation is a durable, resumable step.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path

from .common import UninstallError, canonical, checked_path, digest, save_private

_MAX_PAGES = 1000
_POLL_ATTEMPTS = 6
_RETAINED_TYPES = {
    'AWS::EC2::VPC', 'AWS::EC2::Subnet', 'AWS::EC2::RouteTable',
    'AWS::EC2::SubnetRouteTableAssociation', 'AWS::EC2::SecurityGroup',
    'AWS::RDS::DBSubnetGroup', 'AWS::RDS::DBParameterGroup',
    'AWS::RDS::DBCluster', 'AWS::RDS::DBInstance',
    'AWS::SecretsManager::Secret', 'AWS::S3::Bucket', 'AWS::S3::BucketPolicy',
}
_RUNTIME_TYPES = {
    'AWS::ApiGatewayV2::Api', 'AWS::ApiGatewayV2::Route',
    'AWS::ApiGatewayV2::Integration', 'AWS::ApiGatewayV2::Stage',
    'AWS::Lambda::Function', 'AWS::Lambda::Permission', 'AWS::IAM::Role',
    'AWS::Logs::LogGroup', 'AWS::Budgets::Budget',
}
_MISSING = {
    'DBClusterNotFoundFault', 'DBInstanceNotFound', 'DBInstanceNotFoundFault',
    'DBClusterSnapshotNotFoundFault', 'DBSubnetGroupNotFoundFault', 'DBClusterAutomatedBackupNotFoundFault',
    'DBParameterGroupNotFound', 'DBParameterGroupNotFoundFault',
    'ResourceNotFoundException', 'NoSuchBucket', 'NoSuchKey', 'NoSuchEntity',
    'ParameterNotFound', 'InvalidVpcID.NotFound', 'InvalidSubnetID.NotFound',
    'InvalidRouteTableID.NotFound', 'InvalidAssociationID.NotFound',
    'InvalidGroup.NotFound', '404',
}


def _code(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code', '')


def _call(client, method, **kwargs):
    try:
        return getattr(client, method)(**kwargs)
    except Exception as error:
        if _code(error) in _MISSING:
            return None
        # CloudFormation uses ValidationError for both absent and invalid stacks.
        if (method == 'describe_stacks' and _code(error) == 'ValidationError'
                and 'does not exist' in str(getattr(error, 'response', {}).get('Error', {}).get('Message', ''))):
            return None
        raise


def _pages(client, method, key, **kwargs):
    result, token = [], None
    token_key = 'Marker' if method.startswith('describe_db_') or method in ('list_role_policies', 'list_attached_role_policies', 'list_instance_profiles_for_role') else 'NextToken'
    for _ in range(_MAX_PAGES):
        args = dict(kwargs)
        if token:
            args[token_key] = token
        response = getattr(client, method)(**args)
        result.extend(response.get(key, []))
        new = response.get(token_key)
        if not new:
            return result
        if new == token:
            break
        token = new
    raise UninstallError('Cloud inventory pagination did not complete; nothing was truncated.')


def _jsonable(value):
    return json.loads(json.dumps(value, default=lambda obj: obj.isoformat()))


class _Cloud:
    def __init__(self, values):
        from tools.memory.environment import selected_aws_session, transport_ca_bundle
        from botocore.config import Config
        self.session = selected_aws_session(values, deploy=True, region=values.get('MAVEN_AWS_REGION', 'us-east-1'))
        self.verify = transport_ca_bundle()
        self.config = Config(connect_timeout=3, read_timeout=15, retries={'total_max_attempts': 1})
        self.clients = {}

    def client(self, name):
        if name not in self.clients:
            self.clients[name] = self.session.client(name, verify=self.verify, config=self.config)
        return self.clients[name]

    def close(self):
        for client in self.clients.values():
            client.close()


def _load(home):
    from tools.memory.environment import load_environment
    return load_environment(Path(home) / '.env')


def _identity(values):
    keys = ('MAVEN_DEPLOY_PROFILE', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL',
            'MAVEN_DEPLOY_LOGIN_CACHE', 'MAVEN_AWS_REGION', 'MAVEN_STACK_NAME',
            'MAVEN_MEMORY_INSTANCE_ID', 'MAVEN_MEMORY_RECOVERY_EPOCH',
            'MAVEN_MEMORY_CLUSTER_ARN', 'MAVEN_MEMORY_ADMIN_ROLE_ARN',
            'MAVEN_MEMORY_JOURNAL_BUCKET', 'MAVEN_MEMORY_SKILL_BUCKET',
            'MAVEN_SECRETS_BUCKET', 'MAVEN_SECRETS_FILE_KEY_PREFIX',
            'MAVEN_SECRETS_KEY_ID')
    return {key: values.get(key) for key in keys}


def _tags(items):
    return {item['Key']: item['Value'] for item in items}


def _dedicated(tags, instance):
    # A friendly name or a namespace alone is NOT ownership evidence.
    return (tags.get('MavenInstanceId') == instance
            and tags.get('MavenExclusive') == 'true'
            and tags.get('DataScope') == 'owner_memory')


def _bucket(cloud, name, account):
    s3 = cloud.client('s3')
    s3.head_bucket(Bucket=name, ExpectedBucketOwner=account)
    versions, uploads = [], []
    marker = {}
    for _ in range(_MAX_PAGES):
        response = s3.list_object_versions(Bucket=name, ExpectedBucketOwner=account, **marker)
        for field in ('Versions', 'DeleteMarkers'):
            versions.extend({'key': item['Key'], 'version': item['VersionId'],
                             'marker': field == 'DeleteMarkers',
                             'etag': item.get('ETag'), 'size': item.get('Size')}
                            for item in response.get(field, []))
        if not response.get('IsTruncated'):
            break
        next_marker = {'KeyMarker': response['NextKeyMarker']}
        if response.get('NextVersionIdMarker'):
            next_marker['VersionIdMarker'] = response['NextVersionIdMarker']
        if next_marker == marker:
            raise UninstallError('S3 version listing did not advance.')
        marker = next_marker
    else:
        raise UninstallError('S3 version inventory is too large; no partial inventory is approved.')
    marker = {}
    for _ in range(_MAX_PAGES):
        response = s3.list_multipart_uploads(Bucket=name, ExpectedBucketOwner=account, **marker)
        uploads.extend({'key': item['Key'], 'upload': item['UploadId']} for item in response.get('Uploads', []))
        if not response.get('IsTruncated'):
            break
        marker = {'KeyMarker': response['NextKeyMarker'], 'UploadIdMarker': response['NextUploadIdMarker']}
    else:
        raise UninstallError('Multipart inventory is too large.')
    encryption = s3.get_bucket_encryption(Bucket=name, ExpectedBucketOwner=account)['ServerSideEncryptionConfiguration']
    try:
        tags = _tags(s3.get_bucket_tagging(Bucket=name, ExpectedBucketOwner=account).get('TagSet', []))
    except Exception as error:
        if _code(error) != 'NoSuchTagSet':
            raise
        tags = {}
    return {'name': name, 'versions': sorted(versions, key=lambda item: (item['key'], item['version'])),
            'uploads': sorted(uploads, key=lambda item: (item['key'], item['upload'])),
            'encryption': encryption, 'tags': tags}


def _network(cloud, resources):
    """Refuse to destroy a VPC containing resources not in the reviewed stack."""
    ids = {item['physical'] for item in resources}
    vpcs = [item['physical'] for item in resources if item['type'] == 'AWS::EC2::VPC']
    ec2 = cloud.client('ec2')
    for vpc in vpcs:
        if not _call(ec2, 'describe_vpcs', VpcIds=[vpc]):
            continue
        filters = [{'Name': 'vpc-id', 'Values': [vpc]}]
        for method, key, identity in (
            ('describe_subnets', 'Subnets', 'SubnetId'),
            ('describe_route_tables', 'RouteTables', 'RouteTableId'),
            ('describe_security_groups', 'SecurityGroups', 'GroupId'),
        ):
            for item in _pages(ec2, method, key, Filters=filters):
                implicit = (key == 'RouteTables' and any(a.get('Main') for a in item.get('Associations', []))) or (key == 'SecurityGroups' and item.get('GroupName') == 'default')
                if item[identity] not in ids and not implicit:
                    raise UninstallError('Protected VPC contains an unreviewed resource: ' + item[identity])
        for item in _pages(ec2, 'describe_network_interfaces', 'NetworkInterfaces', Filters=filters):
            if not item.get('RequesterManaged') or not str(item.get('Description', '')).startswith('RDSNetworkInterface'):
                raise UninstallError('Protected VPC contains an unreviewed network interface: ' + item['NetworkInterfaceId'])
        if _pages(ec2, 'describe_internet_gateways', 'InternetGateways', Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc]}]):
            raise UninstallError('Protected VPC has an unreviewed internet gateway.')
        if _pages(ec2, 'describe_vpc_endpoints', 'VpcEndpoints', Filters=filters):
            raise UninstallError('Protected VPC has unreviewed endpoints.')


def _source_manifest():
    """Preserve actual installed source/schema alongside the deployed CF template."""
    result = {}
    for package, subdir in (('tools', 'memory'), ('maven_resources', 'infra/memory')):
        root = files(package).joinpath(subdir)
        def collect(node, prefix):
            for child in sorted(node.iterdir(), key=lambda entry: entry.name):
                name = prefix + '/' + child.name
                if child.is_dir() and child.name != '__pycache__':
                    collect(child, name)
                elif child.is_file() and child.name.endswith(('.py', '.sql', '.json')):
                    raw = child.read_bytes()
                    result[name] = {'sha256': hashlib.sha256(raw).hexdigest(),
                                    'base64': base64.b64encode(raw).decode('ascii')}
        collect(root, package + '/' + subdir)
    return result


def _external_bucket_dependency(cloud, name, account):
    s3 = cloud.client('s3')
    notifications = s3.get_bucket_notification_configuration(Bucket=name, ExpectedBucketOwner=account)
    if any(key in notifications for key in ('TopicConfigurations', 'QueueConfigurations', 'LambdaFunctionConfigurations', 'EventBridgeConfiguration')):
        return 'Bucket has external notification dependencies.'
    try:
        replication = s3.get_bucket_replication(Bucket=name, ExpectedBucketOwner=account)
    except Exception as error:
        if _code(error) != 'ReplicationConfigurationNotFoundError':
            raise
    else:
        if replication.get('ReplicationConfiguration'):
            return 'Bucket has replication dependencies.'
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=name, ExpectedBucketOwner=account)['Policy'])
    except Exception as error:
        if _code(error) != 'NoSuchBucketPolicy':
            raise
    else:
        if any(statement.get('Effect') == 'Allow' for statement in policy.get('Statement', [])):
            return 'Bucket has resource-policy access grants whose consumers are not proven dedicated.'
    return None


def _optional(cloud, selector, instance, account, region):
    if selector.startswith('s3:'):
        name = selector[3:]
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', name):
            raise UninstallError('Optional bucket selector must be s3:BUCKET.')
        info = _bucket(cloud, name, account)
        if not _dedicated(info['tags'], instance):
            return None, 'Bucket lacks exact exclusive-instance ownership tags.'
        reason = _external_bucket_dependency(cloud, name, account)
        if reason:
            return None, reason
        info['exclusive'] = True
        return {'kind': 'bucket', 'inventory': info, 'selector': selector}, None
    if selector.startswith('ssm:'):
        prefix = selector[4:]
        if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)+', prefix) or any(part in ('.', '..') for part in prefix.split('/')):
            raise UninstallError('Optional namespace selector must be ssm:/absolute/prefix/.')
        ssm = cloud.client('ssm')
        parameters = _pages(ssm, 'describe_parameters', 'Parameters', ParameterFilters=[{'Key': 'Path', 'Option': 'Recursive', 'Values': [prefix.rstrip('/')]}])
        inventory, ownership = [], {}
        for item in parameters:
            if not item['Name'].startswith(prefix):
                raise UninstallError('SSM returned a resource outside the approved namespace.')
            tags = _tags(ssm.list_tags_for_resource(ResourceType='Parameter', ResourceId=item['Name']).get('TagList', []))
            if not _dedicated(tags, instance):
                return None, 'Namespace contains parameters without exact exclusive-instance ownership tags.'
            inventory.append({key: item.get(key) for key in ('Name', 'ARN', 'Version', 'Type', 'KeyId', 'LastModifiedDate')})
            ownership[item['Name']] = tags
        return {'kind': 'parameters', 'prefix': prefix, 'inventory': _jsonable(inventory), 'tags': ownership, 'selector': selector}, None
    if selector.startswith('secret:'):
        arn = selector[7:]
        if not arn.startswith(f'arn:aws:secretsmanager:{region}:{account}:secret:'):
            raise UninstallError('Optional secret must be an exact secret ARN in the selected account/region.')
        info = cloud.client('secretsmanager').describe_secret(SecretId=arn)
        if info['ARN'] != arn or not _dedicated(_tags(info.get('Tags', [])), instance):
            return None, 'Secret lacks exact exclusive-instance ownership tags.'
        if info.get('ReplicationStatus') or info.get('RotationEnabled'):
            return None, 'Secret has replication or rotation dependencies.'
        return {'kind': 'secret', 'arn': arn, 'selector': selector,
                'kms_key': info.get('KmsKeyId'), 'versions': info.get('VersionIdsToStages', {}),
                'tags': _tags(info.get('Tags', []))}, None
    raise UninstallError('Unknown optional selector; use s3:BUCKET, ssm:/prefix/, or secret:ARN.')


def plan_cloud(home, *, mode='retire', abandon=False, optional=(), retained_resources=()):
    if mode not in ('retire', 'purge'):
        raise UninstallError('Cloud mode must be retire or purge.')
    home = str(checked_path(home))
    values = _load(home)
    cloud = _Cloud(values)
    try:
        return _plan(cloud, values, home, mode, abandon, optional, retained_resources)
    except UninstallError:
        raise
    except Exception as error:
        raise UninstallError('Cloud planning failed (' + (_code(error) or type(error).__name__) + '); no cloud changes were made.') from None
    finally:
        cloud.close()


def _plan(cloud, values, home, mode, abandon, optional, retained_resources):
    from . import lifecycle
    config_identity = _identity(values)
    cf = cloud.client('cloudformation')
    response = _call(cf, 'describe_stacks', StackName=values['MAVEN_STACK_NAME'])
    if not response or response['Stacks'][0]['StackStatus'] == 'DELETE_COMPLETE':
        return _plan_retained(cloud, values, home, mode, optional, retained_resources)
    if len(response['Stacks']) != 1:
        raise UninstallError('Exactly one stack is required.')
    stack = response['Stacks'][0]
    if stack['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
        raise UninstallError('Stack must be stable before uninstall planning.')
    stack_id = stack['StackId']
    parts = stack_id.split(':', 5)
    region, account = parts[3:5]
    if region != values.get('MAVEN_AWS_REGION', 'us-east-1') or not re.fullmatch(r'\d{12}', account):
        raise UninstallError('Stack account/region mismatch.')
    principal = values['MAVEN_DEPLOY_EXPECTED_PRINCIPAL']
    if principal.split(':')[4] != account:
        raise UninstallError('Deployment principal must belong to the stack account.')
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    for output, key in (('InstanceId', 'MAVEN_MEMORY_INSTANCE_ID'), ('RecoveryEpoch', 'MAVEN_MEMORY_RECOVERY_EPOCH'), ('ClusterArn', 'MAVEN_MEMORY_CLUSTER_ARN'), ('AdminRoleArn', 'MAVEN_MEMORY_ADMIN_ROLE_ARN'), ('JournalBucket', 'MAVEN_MEMORY_JOURNAL_BUCKET'), ('SkillBucket', 'MAVEN_MEMORY_SKILL_BUCKET')):
        if not values.get(key) or outputs.get(output) != values[key]:
            raise UninstallError('Stack output does not match configured ' + output + '.')
    if outputs.get('AccountId') != account or outputs.get('Region') != region:
        raise UninstallError('Stack output account/region mismatch.')
    instance = outputs['InstanceId']
    template = cf.get_template(StackName=stack_id, TemplateStage='Original')['TemplateBody']
    if isinstance(template, str):
        template = json.loads(template)
    resources = []
    for item in _pages(cf, 'list_stack_resources', 'StackResourceSummaries', StackName=stack_id):
        if item.get('ResourceStatus') == 'DELETE_COMPLETE':
            continue
        logical, kind = item['LogicalResourceId'], item['ResourceType']
        definition = template['Resources'].get(logical)
        if not definition or definition['Type'] != kind or kind not in _RETAINED_TYPES | _RUNTIME_TYPES:
            raise UninstallError('Unsupported or drifted stack resource: ' + logical)
        retained = definition.get('DeletionPolicy') == 'Retain'
        if (kind in _RETAINED_TYPES) != retained:
            raise UninstallError('Unexpected resource retention policy: ' + logical)
        resources.append({'logical': logical, 'type': kind, 'physical': item['PhysicalResourceId'], 'retained': retained})
    resources.sort(key=lambda item: item['logical'])
    ids = {item['logical']: item['physical'] for item in resources}
    role_names = {item['physical'] for item in resources if item['type'] == 'AWS::IAM::Role'}
    principal_role = principal.split(':assumed-role/', 1)[-1].split('/')[0] if ':assumed-role/' in principal else principal.rsplit('/', 1)[-1]
    if principal_role in role_names or (stack.get('RoleARN') and stack['RoleARN'].rsplit('/', 1)[-1] in role_names):
        raise UninstallError('Teardown requires an independent deployment principal/service role.')
    iam = cloud.client('iam')
    roles, functions = [], []
    for item in resources:
        if item['type'] == 'AWS::IAM::Role':
            name = item['physical']
            role = iam.get_role(RoleName=name)['Role']
            inline = _pages(iam, 'list_role_policies', 'PolicyNames', RoleName=name)
            expected_inline = {policy['PolicyName'] for policy in template['Resources'][item['logical']]['Properties'].get('Policies', [])}
            if set(inline) != expected_inline or _pages(iam, 'list_attached_role_policies', 'AttachedPolicies', RoleName=name) or _pages(iam, 'list_instance_profiles_for_role', 'InstanceProfiles', RoleName=name):
                raise UninstallError('Stack role has shared/unreviewed policy or instance-profile dependencies: ' + name)
            roles.append({'name': name, 'id': role['RoleId'], 'arn': role['Arn'],
                          'inline': {policy: iam.get_role_policy(RoleName=name, PolicyName=policy)['PolicyDocument'] for policy in inline}})
        elif item['type'] == 'AWS::Lambda::Function':
            configuration = cloud.client('lambda').get_function_configuration(FunctionName=item['physical'])
            functions.append({'name': item['physical'], 'arn': configuration['FunctionArn'],
                              'code_sha256': configuration['CodeSha256'], 'revision': configuration['RevisionId']})
    rds = cloud.client('rds')
    cluster = rds.describe_db_clusters(DBClusterIdentifier=ids['DatabaseCluster'])['DBClusters'][0]
    if cluster['DBClusterArn'] != outputs['ClusterArn']:
        raise UninstallError('Database cluster ARN mismatch.')
    members = {item['DBInstanceIdentifier'] for item in cluster['DBClusterMembers']}
    reviewed_members = {item['physical'] for item in resources if item['type'] == 'AWS::RDS::DBInstance'}
    if members != reviewed_members:
        raise UninstallError('Cluster has unreviewed database instances.')
    instances = {}
    for name in reviewed_members:
        actual = rds.describe_db_instances(DBInstanceIdentifier=name)['DBInstances'][0]
        instances[name] = {'arn': actual['DBInstanceArn'], 'resource_id': actual['DbiResourceId']}
    _network(cloud, resources)
    buckets = [_bucket(cloud, item['physical'], account) for item in resources if item['type'] == 'AWS::S3::Bucket']
    snapshots = _jsonable(_pages(rds, 'describe_db_cluster_snapshots', 'DBClusterSnapshots', DBClusterIdentifier=ids['DatabaseCluster']))
    automated = _jsonable(_pages(rds, 'describe_db_cluster_automated_backups', 'DBClusterAutomatedBackups',
                                DbClusterResourceId=cluster['DbClusterResourceId']))
    secrets = []
    for item in resources:
        if item['type'] == 'AWS::SecretsManager::Secret':
            secret = cloud.client('secretsmanager').describe_secret(SecretId=item['physical'])
            if secret.get('ReplicationStatus') or secret.get('RotationEnabled'):
                raise UninstallError('Stack secret has unreviewed rotation/replication dependencies.')
            secrets.append({'arn': secret['ARN'], 'kms_key': secret.get('KmsKeyId'),
                            'versions': secret.get('VersionIdsToStages', {}), 'tags': _tags(secret.get('Tags', []))})
    selected, retained, warnings = [], [], []
    for selector in dict.fromkeys(optional):
        item, reason = _optional(cloud, selector, instance, account, region)
        if reason:
            retained.append({'target': selector, 'status': 'protected', 'reason': reason})
        elif mode == 'retire':
            retained.append({'target': selector, 'status': 'retained', 'reason': 'Retirement preserves optional data and credential dependencies.'})
        else:
            selected.append(item)
    selected_snapshots, selected_automated = [], []
    for arn in dict.fromkeys(retained_resources):
        if arn.startswith(f'arn:aws:rds:{region}:{account}:cluster-auto-backup:'):
            backup = _automated(cloud, arn, resource_id=cluster['DbClusterResourceId'])
            owned = backup.get('DbClusterResourceId') == cluster['DbClusterResourceId']
            if not owned and not _dedicated(_tags(backup.get('TagList', [])), instance):
                retained.append({'target': arn, 'status': 'protected', 'reason': 'No exact automated-backup ownership evidence.'})
            elif mode == 'purge':
                selected_automated.append(backup)
            else:
                retained.append({'target': arn, 'status': 'retained', 'reason': 'Retirement preserves automated recovery points.'})
            continue
        if arn.startswith(f'arn:aws:rds:{region}:{account}:cluster-snapshot:'):
            snapshot = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=arn)['DBClusterSnapshots'][0]
            tags = _tags(rds.list_tags_for_resource(ResourceName=arn).get('TagList', []))
            owned = snapshot.get('DBClusterIdentifier') == ids['DatabaseCluster'] and tags.get('MavenMemoryInstance') == next((p['ParameterValue'] for p in stack.get('Parameters', []) if p['ParameterKey'] == 'InstanceName'), None)
            if snapshot.get('DBClusterSnapshotArn') != arn or not (owned or _dedicated(tags, instance)):
                retained.append({'target': arn, 'status': 'protected', 'reason': 'Snapshot has no exact stack/exclusive-instance ownership evidence.'})
            elif mode == 'purge':
                if snapshot.get('SnapshotType') != 'manual':
                    raise UninstallError('Only manual cluster snapshots can be selected for deletion.')
                selected_snapshots.append(_jsonable(snapshot))
            else:
                retained.append({'target': arn, 'status': 'retained', 'reason': 'Retirement preserves selected recovery snapshots.'})
        elif arn.startswith('arn:aws:s3:::') and '/' not in arn[13:]:
            name = arn[13:]
            if name in {item['name'] for item in buckets}:
                continue
            item, reason = _optional(cloud, 's3:' + name, instance, account, region)
            if reason or mode == 'retire':
                retained.append({'target': arn, 'status': 'protected' if reason else 'retained', 'reason': reason or 'Retirement preserves data.'})
            elif not any(entry.get('selector') == item['selector'] for entry in selected):
                selected.append(item)
        else:
            raise UninstallError('Retained resource must be an exact same-region cluster-snapshot/cluster-auto-backup ARN or S3 bucket ARN.')
    removed_buckets = {item['inventory']['name'] for item in selected if item['kind'] == 'bucket'} | ({item['name'] for item in buckets} if mode == 'purge' else set())
    wrapping_prefix = values.get('MAVEN_SECRETS_FILE_KEY_PREFIX')
    for item in list(selected):
        if item['kind'] == 'parameters' and wrapping_prefix and any(p['Name'].startswith(wrapping_prefix) for p in item['inventory']) and values.get('MAVEN_SECRETS_BUCKET') not in removed_buckets:
            selected.remove(item)
            retained.append({'target': item['selector'], 'status': 'protected', 'reason': 'Wrapping keys are required by an unselected encrypted-file bucket.'})
    for snapshot in snapshots:
        if snapshot.get('SnapshotType') == 'manual' and snapshot['DBClusterSnapshotArn'] not in {item['DBClusterSnapshotArn'] for item in selected_snapshots}:
            retained.append({'target': snapshot['DBClusterSnapshotArn'], 'status': 'retained', 'reason': 'Existing backup was not individually approved for deletion; storage charges may continue.'})
    for backup in automated:
        if backup['DBClusterAutomatedBackupsArn'] not in {item['DBClusterAutomatedBackupsArn'] for item in selected_automated}:
            retained.append({'target': backup['DBClusterAutomatedBackupsArn'], 'status': 'retained',
                             'reason': 'Automated backups preserved until their retention expires; storage charges may continue.'})
    keys = {cluster.get('KmsKeyId')}
    keys.update(item['kms_key'] for item in secrets)
    for bucket in buckets:
        keys.update(rule.get('ApplyServerSideEncryptionByDefault', {}).get('KMSMasterKeyID') for rule in bucket['encryption'].get('Rules', []))
    for key in sorted(key for key in keys if key):
        metadata = cloud.client('kms').describe_key(KeyId=key)['KeyMetadata']
        if mode == 'retire' and metadata['KeyState'] != 'Enabled':
            raise UninstallError('Recovery requires an enabled encryption key: ' + metadata['Arn'])
        retained.append({'target': metadata['Arn'], 'status': 'retained', 'reason': 'Encryption keys are never deleted; recovery or other resources may depend on them.'})
    lifecycle_fragment = lifecycle.plan_cloud(home, abandon=abandon)
    details = {'home': home, 'identity': config_identity, 'instance_id': instance,
               'recovery_epoch': outputs['RecoveryEpoch'], 'mode': mode, 'stack_id': stack_id,
               'account': account, 'region': region, 'resources': resources, 'template': template,
               'stack_parameters': stack.get('Parameters', []), 'stack_outputs': outputs,
               'cluster': _jsonable(cluster), 'buckets': buckets, 'secrets': secrets,
               'selected': selected, 'snapshots': selected_snapshots,
               'instances': instances, 'automated_backups': selected_automated, 'preserved_automated_backups': automated,
               'roles': roles, 'functions': functions,
               'lifecycle': lifecycle_fragment, 'source': _source_manifest(),
               'wrapping_key_dependency': {'bucket': values.get('MAVEN_SECRETS_BUCKET'),
                    'parameter': (wrapping_prefix + values['MAVEN_SECRETS_KEY_ID']) if wrapping_prefix and values.get('MAVEN_SECRETS_KEY_ID') else None}}
    details['snapshot_id'] = 'maven-retire-' + digest(details)[:40]
    details['recovery_key'] = 'uninstall/' + instance + '/' + digest(details) + '.json'
    if mode == 'retire':
        for bucket in buckets:
            retained.append({'target': 'arn:aws:s3:::' + bucket['name'], 'status': 'retained', 'reason': 'Verified recovery/skill versions; ongoing storage charges.'})
        retained.extend({'target': item['arn'], 'status': 'retained', 'reason': 'Database recovery credential; required encryption keys remain.'} for item in secrets)
        retained.append({'target': details['snapshot_id'], 'status': 'retained', 'reason': 'New final cluster snapshot; storage charges; no live restore was performed.'})
    warnings.extend(['Cloud inventory is point-in-time; new/unreviewed data blocks purge rather than being swept.',
                     'No provider account, login, KMS key, or unselected backup is deleted.',
                     'Retained snapshots, buckets and keys can continue to incur charges. No live restore has been tested.'])
    if mode == 'purge':
        warnings.append('Secrets are scheduled for deletion with a 7-day recovery window, not immediately erased.')
    return {'actions': lifecycle_fragment['actions'] + [{'handler': 'cloud', 'kind': 'cloud-teardown', 'target': stack_id, 'stage': 30, 'details': details}],
            'retained': lifecycle_fragment.get('retained', []) + retained,
            'warnings': lifecycle_fragment.get('warnings', []) + warnings,
            'targets': lifecycle_fragment.get('targets', []) + [{'scope': 'cloud', 'home': home, 'instance_id': instance, 'stack_id': stack_id, 'mode': mode}],
            'requires': list(dict.fromkeys(lifecycle_fragment.get('requires', []) + ['writers-quiescent', 'harnesses-stopped'] + (['purge'] if mode == 'purge' else [])))}


def _automated(cloud, arn, *, resource_id=None):
    # cab-* identifies the backup ARN, not its source cluster-* resource ID.
    # A deleted source requires bounded read-only inventory, followed by an
    # exact full-ARN match; only that matched source ID can authorize deletion.
    scope = {'DbClusterResourceId': resource_id} if resource_id else {}
    records = _pages(cloud.client('rds'), 'describe_db_cluster_automated_backups',
                     'DBClusterAutomatedBackups', **scope)
    exact = [item for item in records if item.get('DBClusterAutomatedBackupsArn') == arn]
    if not exact and resource_id is not None:
        return _automated(cloud, arn)
    if len(exact) != 1:
        raise UninstallError('The exact selected automated backup was not found.')
    return _jsonable(exact[0])


def _retired_backup_owned(cloud, values, backup, instance, account):
    if _dedicated(_tags(backup.get('TagList', [])), instance):
        return True
    # Template backups predate exclusive tags. A retirement manifest in the
    # exclusively owned journal binds their immutable source resource ID.
    s3 = cloud.client('s3')
    bucket = values['MAVEN_MEMORY_JOURNAL_BUCKET']
    try:
        response = _call(s3, 'get_bucket_tagging', Bucket=bucket, ExpectedBucketOwner=account)
    except Exception as error:
        if _code(error) == 'NoSuchTagSet':
            return False
        raise
    if not response or not _dedicated(_tags(response.get('TagSet', [])), instance):
        return False
    args = {'Bucket': bucket, 'Prefix': 'uninstall/' + instance + '/', 'ExpectedBucketOwner': account}
    for _ in range(_MAX_PAGES):
        page = s3.list_objects_v2(**args)
        for item in page.get('Contents', []):
            if not item['Key'].endswith('.json') or item['Size'] > 32 * 1024 * 1024:
                continue
            result = s3.get_object(Bucket=bucket, Key=item['Key'], ExpectedBucketOwner=account)
            raw = result['Body'].read(32 * 1024 * 1024 + 1)
            result['Body'].close()
            if len(raw) > 32 * 1024 * 1024:
                raise UninstallError('Retirement manifest exceeds bounded size.')
            document = json.loads(raw)
            if (document.get('format') != 'maven-retirement-recovery-1'
                    or document.get('instance_id') != instance
                    or document.get('recovery_epoch') != values['MAVEN_MEMORY_RECOVERY_EPOCH']
                    or document.get('outputs', {}).get('ClusterArn') != values['MAVEN_MEMORY_CLUSTER_ARN']):
                continue
            if any(old.get('DBClusterAutomatedBackupsArn') == backup['DBClusterAutomatedBackupsArn']
                   and old.get('DbClusterResourceId') == backup['DbClusterResourceId']
                   for old in document.get('automated_backups', [])):
                return True
        if not page.get('IsTruncated'):
            return False
        args['ContinuationToken'] = page['NextContinuationToken']
    raise UninstallError('Retirement manifest listing did not complete.')


def _plan_retained(cloud, values, home, mode, optional, retained_resources):
    """A later purge of individually selected resources after retirement."""
    if mode != 'purge' or not (optional or retained_resources):
        raise UninstallError('Stack no longer exists; select purge and individual retained-resource ARNs/optional selectors.')
    instance = values['MAVEN_MEMORY_INSTANCE_ID']
    region = values.get('MAVEN_AWS_REGION', 'us-east-1')
    account = values['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'].split(':')[4]
    cluster_arn = values['MAVEN_MEMORY_CLUSTER_ARN']
    if not cluster_arn.startswith(f'arn:aws:rds:{region}:{account}:cluster:'):
        raise UninstallError('Retained-resource account/region does not match the configured cluster.')
    if _call(cloud.client('rds'), 'describe_db_clusters', DBClusterIdentifier=cluster_arn):
        raise UninstallError('The stack is absent but its database remains; resume the original uninstall journal before a retained-data purge.')
    selected, snapshots, protected, automated = [], [], [], []
    selectors = list(optional)
    for arn in retained_resources:
        if arn.startswith(f'arn:aws:rds:{region}:{account}:cluster-auto-backup:'):
            backup = _automated(cloud, arn)
            if _retired_backup_owned(cloud, values, backup, instance, account):
                automated.append(backup)
            else:
                protected.append({'target': arn, 'status': 'protected', 'reason': 'Automated backup lacks exact exclusive-instance retirement ownership evidence.'})
            continue
        if arn.startswith('arn:aws:s3:::') and '/' not in arn[13:]:
            selectors.append('s3:' + arn[13:])
        elif arn.startswith(f'arn:aws:secretsmanager:{region}:{account}:secret:'):
            selectors.append('secret:' + arn)
        elif arn.startswith(f'arn:aws:rds:{region}:{account}:cluster-snapshot:'):
            snapshot = cloud.client('rds').describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=arn)['DBClusterSnapshots'][0]
            tags = _tags(cloud.client('rds').list_tags_for_resource(ResourceName=arn).get('TagList', []))
            if snapshot.get('DBClusterSnapshotArn') != arn or not _dedicated(tags, instance):
                protected.append({'target': arn, 'status': 'protected', 'reason': 'No exact exclusive-instance retirement ownership evidence.'})
            elif snapshot.get('SnapshotType') != 'manual':
                protected.append({'target': arn, 'status': 'protected', 'reason': 'Automated recovery points are not manual snapshots.'})
            else:
                snapshots.append(_jsonable(snapshot))
        else:
            raise UninstallError('Retained purge accepts exact same-account/region cluster-snapshot, cluster-auto-backup or secret ARNs and S3 bucket ARNs.')
    for selector in dict.fromkeys(selectors):
        item, reason = _optional(cloud, selector, instance, account, region)
        if reason:
            protected.append({'target': selector, 'status': 'protected', 'reason': reason})
        else:
            selected.append(item)
    removed_buckets = {item['inventory']['name'] for item in selected if item['kind'] == 'bucket'}
    prefix = values.get('MAVEN_SECRETS_FILE_KEY_PREFIX')
    for item in list(selected):
        if item['kind'] == 'parameters' and prefix and any(p['Name'].startswith(prefix) for p in item['inventory']) and values.get('MAVEN_SECRETS_BUCKET') not in removed_buckets:
            selected.remove(item)
            protected.append({'target': item['selector'], 'status': 'protected', 'reason': 'An unselected encrypted-file bucket still depends on these wrapping keys.'})
    details = {'home': home, 'identity': _identity(values), 'instance_id': instance,
               'recovery_epoch': values['MAVEN_MEMORY_RECOVERY_EPOCH'], 'mode': mode,
               'retained_only': True, 'account': account, 'region': region,
               'resources': [], 'buckets': [], 'secrets': [], 'selected': selected,
               'automated_backups': automated,
               'snapshots': snapshots, 'stack_outputs': {'JournalBucket': values['MAVEN_MEMORY_JOURNAL_BUCKET']}}
    return {'actions': [{'handler': 'cloud', 'kind': 'cloud-teardown',
                         'target': cluster_arn, 'stage': 30, 'details': details}],
            'retained': protected,
            'warnings': ['Only individually selected, exclusively owned retired resources are deleted. Unselected resources/keys remain.',
                         'Secrets have a 7-day recovery window; no immediate irreversible erasure is claimed.'],
            'targets': [{'scope': 'cloud', 'home': home, 'instance_id': instance, 'mode': mode, 'retained_only': True}],
            'requires': ['writers-quiescent', 'harnesses-stopped', 'purge']}


def _step(progress, save, key, operation):
    entry = progress.setdefault('steps', {}).setdefault(key, {})
    if entry.get('done') and (entry.get('result') or {}).get('status') != 'pending-recovery-window':
        return entry.get('result')
    entry['intent'] = True
    save()
    result = operation(entry)
    entry['result'], entry['done'] = result, True
    save()
    return result


def _pending(progress, save, message):
    progress['pending'] = message
    save()
    raise UninstallError(message + ' Resume the same plan to reconcile; do not re-plan.')


def _snapshot(cloud, details, entry, save):
    rds = cloud.client('rds')
    name = details['snapshot_id']
    response = _call(rds, 'describe_db_cluster_snapshots', DBClusterSnapshotIdentifier=name)
    if not response:
        if entry.get('created'):
            raise UninstallError('Previously created recovery snapshot disappeared.')
        rds.create_db_cluster_snapshot(DBClusterIdentifier=details['cluster']['DBClusterIdentifier'], DBClusterSnapshotIdentifier=name,
            Tags=[{'Key': 'MavenInstanceId', 'Value': details['instance_id']}, {'Key': 'MavenExclusive', 'Value': 'true'}, {'Key': 'DataScope', 'Value': 'owner_memory'}, {'Key': 'MavenUninstall', 'Value': digest(details)}])
        entry['created'] = True
        save()
    for _ in range(_POLL_ATTEMPTS):
        response = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=name)
        snapshot = response['DBClusterSnapshots'][0]
        tags = _tags(rds.list_tags_for_resource(ResourceName=snapshot['DBClusterSnapshotArn']).get('TagList', []))
        if snapshot['DBClusterIdentifier'] != details['cluster']['DBClusterIdentifier'] or tags.get('MavenUninstall') != digest(details):
            raise UninstallError('Recovery snapshot identity does not match this exact plan.')
        if snapshot['Status'] == 'available':
            if snapshot.get('KmsKeyId') != details['cluster'].get('KmsKeyId'):
                raise UninstallError('Recovery snapshot encryption key mismatch.')
            return _jsonable(snapshot)
        if snapshot['Status'] not in ('creating', 'copying'):
            raise UninstallError('Recovery snapshot is not usable: ' + snapshot['Status'])
    raise UninstallError('Recovery snapshot is pending; resume this plan after AWS finishes it.')


def _frozen_control(cloud, details, *, version=None):
    freeze = next(item['details'] for item in details['lifecycle']['actions'] if item['kind'] == 'cloud-freeze')
    args = {'Bucket': details['stack_outputs']['JournalBucket'], 'Key': 'control.json',
            'ExpectedBucketOwner': details['account']}
    if version is not None:
        args['VersionId'] = version
    response = cloud.client('s3').get_object(**args)
    raw = response['Body'].read(65537)
    response['Body'].close()
    if len(raw) > 65536:
        raise UninstallError('Cloud barrier exceeds its bounded size.')
    control = json.loads(raw)
    if (control.get('state') != 'maintenance' or control.get('uninstall') != freeze['barrier_token']
            or control.get('instance_id') != details['instance_id']
            or control.get('recovery_epoch') != details['recovery_epoch']):
        raise UninstallError('The exact approved persistent write barrier is not active.')
    return control


def _runtime_archives(cloud, details):
    from urllib.parse import urlsplit
    import ssl
    from urllib.request import build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler
    from tools.memory.environment import transport_ca_bundle
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise UninstallError('Runtime recovery download refused an HTTP redirect.')
    bundle = transport_ca_bundle()
    context = ssl.create_default_context(cafile=bundle if isinstance(bundle, str) else None)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    archives = {}
    for expected in details['functions']:
        result = cloud.client('lambda').get_function(FunctionName=expected['name'])
        configuration = result['Configuration']
        if configuration['FunctionArn'] != expected['arn'] or configuration['CodeSha256'] != expected['code_sha256']:
            raise UninstallError('Deployed runtime code changed since review.')
        url = result['Code']['Location']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.amazonaws.com')
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise UninstallError('Unexpected AWS runtime archive location.')
        with opener.open(url, timeout=20) as response:
            archive = response.read(16 * 1024 * 1024 + 1)
        if len(archive) > 16 * 1024 * 1024 or base64.b64encode(hashlib.sha256(archive).digest()).decode('ascii') != expected['code_sha256']:
            raise UninstallError('Deployed runtime archive verification failed or exceeds the recovery size.')
        archives[expected['arn']] = {'sha256': expected['code_sha256'], 'zip_base64': base64.b64encode(archive).decode('ascii')}
    return archives


def _recovery(cloud, details, snapshot, operation_dir=None):
    s3 = cloud.client('s3')
    account = details['account']
    preserved = []
    for expected in details['buckets']:
        actual = _bucket(cloud, expected['name'], account)
        known = {(item['key'], item['version']): item for item in actual['versions']}
        for item in expected['versions']:
            if known.get((item['key'], item['version'])) != item:
                raise UninstallError('A reviewed recovery/skill version disappeared or changed.')
        preserved.append(actual)
    # Exact current stack ownership permits durable exclusive-instance evidence
    # for a later individually selected purge after CloudFormation is gone.
    ownership = {'MavenInstanceId': details['instance_id'], 'MavenExclusive': 'true',
                 'DataScope': 'owner_memory', 'MavenRetiredStack': details['stack_id']}
    for bucket_info in preserved:
        merged = {**bucket_info['tags'], **ownership}
        if len(merged) > 50:
            raise UninstallError('Recovery bucket ownership tags exceed AWS tag limit.')
        s3.put_bucket_tagging(Bucket=bucket_info['name'], ExpectedBucketOwner=account,
                              Tagging={'TagSet': [{'Key': key, 'Value': value} for key, value in merged.items()]})
    for secret in details['secrets']:
        cloud.client('secretsmanager').tag_resource(SecretId=secret['arn'],
            Tags=[{'Key': key, 'Value': value} for key, value in ownership.items()])
    manifest = {'format': 'maven-retirement-recovery-1', 'instance_id': details['instance_id'],
                'recovery_epoch': details['recovery_epoch'], 'snapshot': snapshot, 'plan_digest': digest(details),
                'buckets': preserved, 'secrets': details['secrets'],
                'frozen_control': _frozen_control(cloud, details),
                'template': details['template'], 'parameters': details['stack_parameters'],
                'outputs': details['stack_outputs'], 'source': details['source'],
                'deployed_runtime_archives': _runtime_archives(cloud, details),
                'automated_backups': details['preserved_automated_backups'],
                'lifecycle': details['lifecycle'], 'wrapping_key_dependency': details['wrapping_key_dependency'],
                'verification': 'Snapshot available; retained object version metadata checked; manifest read back. No live restore performed.',
                'restore_requirements': ['Retain snapshot KMS key, database secrets and versioned skill/recovery buckets.',
                    'Recreate the reviewed private network and restore the cluster from the exact snapshot; do not create an empty replacement database.',
                    'Rebind restored cluster, secrets and bucket identities using reviewed deployment/admin credentials; retain schema migration source.',
                    'Keep restored service frozen until administrative recovery assigns a new recovery epoch and reconciles sessions/receipts; never silently clear the barrier.']}
    body = canonical(manifest)
    if len(body) > 32 * 1024 * 1024:
        raise UninstallError('Recovery manifest exceeds the bounded recovery size.')
    bucket, key = details['stack_outputs']['JournalBucket'], details['recovery_key']
    response = _call(s3, 'get_object', Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    if response:
        existing = response['Body'].read(32 * 1024 * 1024 + 1)
        response['Body'].close()
        # The first write is immutable even after a crash and later control changes.
        saved = json.loads(existing)
        if saved.get('plan_digest') != digest(details) or saved.get('instance_id') != details['instance_id'] or saved.get('snapshot') != snapshot or saved.get('source') != details['source'] or saved.get('template') != details['template']:
            raise UninstallError('Existing recovery manifest does not match this plan.')
        body = existing
    else:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType='application/json', ServerSideEncryption='AES256', IfNoneMatch='*', ExpectedBucketOwner=account)
    check = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    actual = check['Body'].read(len(body) + 1)
    check['Body'].close()
    if actual != body:
        raise UninstallError('Recovery manifest read-back verification failed.')
    local_path = None
    if operation_dir:
        local_path = str(Path(operation_dir) / ('cloud-recovery-' + details['instance_id'] + '.json'))
        save_private(local_path, json.loads(body))
    return {'bucket': bucket, 'key': key, 'version': check.get('VersionId'), 'sha256': hashlib.sha256(body).hexdigest(),
            'local_manifest': local_path, 'portable_runner': str(Path(operation_dir) / 'runner') if operation_dir else None,
            'live_restore_verified': False}


def _delete_database(cloud, details, progress, save):
    rds = cloud.client('rds')
    cluster_id = details['cluster']['DBClusterIdentifier']
    def writer(item):
        def remove(entry):
            response = _call(rds, 'describe_db_instances', DBInstanceIdentifier=item['physical'])
            if not response:
                return {'status': 'removed'}
            instance = response['DBInstances'][0]
            expected = details['instances'][item['physical']]
            if (instance.get('DBClusterIdentifier') != cluster_id
                    or instance.get('DBInstanceArn') != expected['arn']
                    or instance.get('DbiResourceId') != expected['resource_id']):
                raise UninstallError('Database instance identity or cluster changed.')
            if instance['DBInstanceStatus'] != 'deleting':
                rds.delete_db_instance(DBInstanceIdentifier=item['physical'], SkipFinalSnapshot=True, DeleteAutomatedBackups=False)
            for _ in range(_POLL_ATTEMPTS):
                if not _call(rds, 'describe_db_instances', DBInstanceIdentifier=item['physical']):
                    return {'status': 'removed'}
            _pending(progress, save, 'Database instance deletion is pending.')
        return remove
    response = _call(rds, 'describe_db_clusters', DBClusterIdentifier=cluster_id)
    if response:
        current = response['DBClusters'][0]
        if current['DBClusterArn'] != details['cluster']['DBClusterArn'] or current.get('DbClusterResourceId') != details['cluster'].get('DbClusterResourceId'):
            raise UninstallError('Cluster identity changed.')
        planned = {item['physical'] for item in details['resources'] if item['type'] == 'AWS::RDS::DBInstance'}
        if any(member['DBInstanceIdentifier'] not in planned for member in current['DBClusterMembers']):
            raise UninstallError('Cluster has new unreviewed members.')
    for item in details['resources']:
        if item['type'] == 'AWS::RDS::DBInstance':
            _step(progress, save, 'writer:' + item['physical'], writer(item))
    def remove_cluster(entry):
        response = _call(rds, 'describe_db_clusters', DBClusterIdentifier=cluster_id)
        if not response:
            return {'status': 'removed'}
        cluster = response['DBClusters'][0]
        if cluster['DeletionProtection']:
            entry['disable_protection_intent'] = True
            save()
            rds.modify_db_cluster(DBClusterIdentifier=cluster_id, DeletionProtection=False, ApplyImmediately=True)
            cluster = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)['DBClusters'][0]
            if cluster['DeletionProtection']:
                _pending(progress, save, 'Database deletion-protection update is pending.')
        if cluster['Status'] != 'deleting':
            rds.delete_db_cluster(DBClusterIdentifier=cluster_id, SkipFinalSnapshot=True, DeleteAutomatedBackups=False)
        for _ in range(_POLL_ATTEMPTS):
            if not _call(rds, 'describe_db_clusters', DBClusterIdentifier=cluster_id):
                return {'status': 'removed'}
        _pending(progress, save, 'Database cluster deletion is pending.')
    _step(progress, save, 'cluster', remove_cluster)


def _delete_network(cloud, details, progress, save):
    _network(cloud, details['resources'])
    rds, ec2 = cloud.client('rds'), cloud.client('ec2')
    order = [
        ('AWS::RDS::DBSubnetGroup', rds, 'delete_db_subnet_group', 'DBSubnetGroupName'),
        ('AWS::RDS::DBParameterGroup', rds, 'delete_db_parameter_group', 'DBParameterGroupName'),
        ('AWS::EC2::SubnetRouteTableAssociation', ec2, 'disassociate_route_table', 'AssociationId'),
        ('AWS::EC2::Subnet', ec2, 'delete_subnet', 'SubnetId'),
        ('AWS::EC2::RouteTable', ec2, 'delete_route_table', 'RouteTableId'),
        ('AWS::EC2::SecurityGroup', ec2, 'delete_security_group', 'GroupId'),
        ('AWS::EC2::VPC', ec2, 'delete_vpc', 'VpcId'),
    ]
    for kind, client, method, parameter in order:
        for item in details['resources']:
            if item['type'] == kind:
                _step(progress, save, method + ':' + item['physical'],
                      lambda entry, c=client, m=method, p=parameter, i=item: _call(c, m, **{p: i['physical']}) or {'status': 'removed'})


def _verify_roles(cloud, details):
    iam = cloud.client('iam')
    for expected in details['roles']:
        result = _call(iam, 'get_role', RoleName=expected['name'])
        if result is None:
            continue
        if result['Role']['RoleId'] != expected['id']:
            raise UninstallError('Stack role was replaced since approval.')
        policies = _pages(iam, 'list_role_policies', 'PolicyNames', RoleName=expected['name'])
        # During a resumed CF deletion reviewed policies may already be gone.
        if not set(policies).issubset(expected['inline']) or _pages(iam, 'list_attached_role_policies', 'AttachedPolicies', RoleName=expected['name']) or _pages(iam, 'list_instance_profiles_for_role', 'InstanceProfiles', RoleName=expected['name']):
            raise UninstallError('Stack role acquired unreviewed policy/profile dependencies.')
        for policy in policies:
            actual = iam.get_role_policy(RoleName=expected['name'], PolicyName=policy)['PolicyDocument']
            if actual != expected['inline'][policy]:
                raise UninstallError('Stack role policy changed since approval.')


def _validate_stack_before_delete(cf, details, stack, entry):
    template = cf.get_template(StackName=details['stack_id'], TemplateStage='Original')['TemplateBody']
    if isinstance(template, str):
        template = json.loads(template)
    if template != details['template']:
        raise UninstallError('Stack template changed since review; deletion is blocked.')
    parameters = {item['ParameterKey']: item.get('ParameterValue') for item in stack.get('Parameters', [])}
    expected_parameters = {item['ParameterKey']: item.get('ParameterValue') for item in details['stack_parameters']}
    if parameters != expected_parameters:
        raise UninstallError('Stack parameters changed since review; deletion is blocked.')
    expected = {item['logical']: item for item in details['resources']}
    seen = set()
    for item in _pages(cf, 'list_stack_resources', 'StackResourceSummaries', StackName=details['stack_id']):
        logical = item['LogicalResourceId']
        approved = expected.get(logical)
        if approved is None or item['ResourceType'] != approved['type']:
            raise UninstallError('Stack contains an unreviewed resource; deletion is blocked.')
        if item.get('PhysicalResourceId') != approved['physical']:
            if not (entry.get('delete_requested') and item.get('ResourceStatus') == 'DELETE_COMPLETE' and not item.get('PhysicalResourceId')):
                raise UninstallError('Stack resource identity changed since review; deletion is blocked.')
        seen.add(logical)
    if seen != set(expected) and not entry.get('delete_requested'):
        raise UninstallError('Stack inventory changed before deletion began.')


def _delete_stack(cloud, details, progress, save):
    cf = cloud.client('cloudformation')
    def remove(entry):
        response = _call(cf, 'describe_stacks', StackName=details['stack_id'])
        if not response or response['Stacks'][0]['StackStatus'] == 'DELETE_COMPLETE':
            return {'status': 'removed'}
        stack = response['Stacks'][0]
        if stack['StackId'] != details['stack_id']:
            raise UninstallError('Stack identity changed.')
        _validate_stack_before_delete(cf, details, stack, entry)
        if stack.get('EnableTerminationProtection'):
            entry['disable_protection_intent'] = True
            save()
            cf.update_termination_protection(EnableTerminationProtection=False, StackName=details['stack_id'])
        if stack['StackStatus'] != 'DELETE_IN_PROGRESS':
            _verify_roles(cloud, details)
            entry['delete_requested'] = True
            save()
            cf.delete_stack(StackName=details['stack_id'], ClientRequestToken='maven-' + digest(details)[:48])
        for _ in range(_POLL_ATTEMPTS):
            response = _call(cf, 'describe_stacks', StackName=details['stack_id'])
            if not response or response['Stacks'][0]['StackStatus'] == 'DELETE_COMPLETE':
                return {'status': 'removed'}
            if response['Stacks'][0]['StackStatus'] == 'DELETE_FAILED':
                _pending(progress, save, 'CloudFormation deletion failed; inspect exact stack events and resolve dependency/permission blockers before resume.')
        _pending(progress, save, 'CloudFormation runtime/IAM/log/API deletion is pending.')
    return _step(progress, save, 'stack', remove)


def _purge_bucket(cloud, expected, details, entry, save):
    s3, account, name = cloud.client('s3'), details['account'], expected['name']
    if _call(s3, 'head_bucket', Bucket=name, ExpectedBucketOwner=account) is None:
        return {'status': 'removed'}
    actual = _bucket(cloud, name, account)
    if actual['tags'] != expected['tags']:
        raise UninstallError('Bucket ownership tags changed since approval.')
    if expected.get('exclusive'):
        reason = _external_bucket_dependency(cloud, name, account)
        if reason:
            raise UninstallError(reason)
    allowed = {(item['key'], item['version']): item for item in expected['versions']}
    # The stage-20 barrier is the sole approved post-plan write. Record its exact
    # versions durably before deletion; no other newly-created object is swept.
    journal = details['stack_outputs']['JournalBucket']
    for item in actual['versions']:
        identity = (item['key'], item['version'])
        if identity in allowed and item != allowed[identity]:
            raise UninstallError('Reviewed object content changed (including an overwritten null version); protected: ' + name)
        if identity not in allowed:
            if details.get('retained_only') or name != journal or item['key'] != 'control.json' or item['marker']:
                raise UninstallError('Bucket contains unreviewed object versions; protected: ' + name)
            _frozen_control(cloud, details, version=item['version'])
    allowed_uploads = {(item['key'], item['upload']) for item in expected['uploads']}
    if any((item['key'], item['upload']) not in allowed_uploads for item in actual['uploads']):
        raise UninstallError('Bucket has unreviewed multipart uploads; protected: ' + name)
    entry['versions'] = actual['versions']
    entry['uploads'] = actual['uploads']
    save()
    for item in actual['uploads']:
        try:
            s3.abort_multipart_upload(Bucket=name, Key=item['key'], UploadId=item['upload'], ExpectedBucketOwner=account)
        except Exception as error:
            if _code(error) != 'NoSuchUpload':
                raise
    for start in range(0, len(actual['versions']), 1000):
        batch = actual['versions'][start:start + 1000]
        result = s3.delete_objects(Bucket=name, ExpectedBucketOwner=account,
            Delete={'Objects': [{'Key': item['key'], 'VersionId': item['version']} for item in batch], 'Quiet': True})
        if result.get('Errors'):
            raise UninstallError('Some reviewed S3 versions could not be deleted (object lock, MFA or permissions); resume after resolving the blocker.')
    remaining = _bucket(cloud, name, account)
    if remaining['versions'] or remaining['uploads']:
        raise UninstallError('Bucket is not empty; concurrent writes are protected, not swept.')
    s3.delete_bucket(Bucket=name, ExpectedBucketOwner=account)
    return {'status': 'removed', 'bucket': name}


def _schedule_secret(cloud, arn, entry, save, expected=None):
    client = cloud.client('secretsmanager')
    response = _call(client, 'describe_secret', SecretId=arn)
    if not response:
        return {'status': 'removed', 'arn': arn}
    if response['ARN'] != arn:
        raise UninstallError('Secret ARN changed.')
    if response.get('ReplicationStatus') or response.get('RotationEnabled'):
        raise UninstallError('Secret acquired rotation or replication dependencies since approval.')
    if expected is not None and (response.get('VersionIdsToStages', {}) != expected['versions']
            or ('tags' in expected and _tags(response.get('Tags', [])) != expected['tags'])):
        raise UninstallError('Secret versions or ownership changed since approval.')
    if response.get('DeletedDate'):
        return {'status': 'pending-recovery-window', 'arn': arn, 'deletion_date': _jsonable(response['DeletedDate'])}
    result = client.delete_secret(SecretId=arn, RecoveryWindowInDays=7)
    entry['scheduled'] = _jsonable(result.get('DeletionDate'))
    save()
    return {'status': 'pending-recovery-window', 'arn': arn, 'deletion_date': entry['scheduled']}


def _purge_parameters(cloud, item, entry, save):
    ssm = cloud.client('ssm')
    current = _pages(ssm, 'describe_parameters', 'Parameters', ParameterFilters=[{'Key': 'Path', 'Option': 'Recursive', 'Values': [item['prefix'].rstrip('/')]}])
    expected = {parameter['Name']: parameter for parameter in item['inventory']}
    for parameter in current:
        known = expected.get(parameter['Name'])
        if not known or parameter.get('Version') != known['Version'] or _jsonable(parameter.get('LastModifiedDate')) != known['LastModifiedDate']:
            raise UninstallError('SSM namespace contains new/changed parameters; protected.')
        tags = _tags(ssm.list_tags_for_resource(ResourceType='Parameter', ResourceId=parameter['Name']).get('TagList', []))
        if tags != item['tags'][parameter['Name']]:
            raise UninstallError('SSM parameter ownership changed since approval.')
    entry['names'] = [parameter['Name'] for parameter in current]
    save()
    for start in range(0, len(current), 10):
        result = ssm.delete_parameters(Names=[parameter['Name'] for parameter in current[start:start + 10]])
        if result.get('InvalidParameters'):
            # A simultaneous disappearance is safe; changed values are not.
            for name in result['InvalidParameters']:
                if _call(ssm, 'get_parameter', Name=name, WithDecryption=False):
                    raise UninstallError('SSM parameter deletion was not confirmed.')
    return {'status': 'removed', 'namespace': item['prefix']}


def execute(action, progress, save):
    if action.get('kind') != 'cloud-teardown':
        raise UninstallError('Unknown cloud uninstall action.')
    details = action['details']
    values = _load(details['home'])
    if _identity(values) != details['identity']:
        raise UninstallError('Cloud configuration changed since approval.')
    cloud = _Cloud(values)
    try:
        return _execute(cloud, details, progress, save, action.get('operation_dir'))
    except UninstallError:
        raise
    except Exception as error:
        progress['pending'] = 'AWS operation did not finish: ' + (_code(error) or type(error).__name__)
        save()
        raise UninstallError(progress['pending'] + '. Resume this exact plan; no success is assumed.') from None
    finally:
        cloud.close()


def _execute(cloud, details, progress, save, operation_dir=None):
    # Before the first mutation, bind the still-live stack/template/resources.
    if details.get('retained_only'):
        stack = _call(cloud.client('cloudformation'), 'describe_stacks', StackName=details['identity']['MAVEN_STACK_NAME'])
        if stack and stack['Stacks'][0]['StackStatus'] != 'DELETE_COMPLETE':
            raise UninstallError('A stack now exists at the retired deployment name; protect retained dependencies.')
        if _call(cloud.client('rds'), 'describe_db_clusters', DBClusterIdentifier=details['identity']['MAVEN_MEMORY_CLUSTER_ARN']):
            raise UninstallError('The retired cluster now exists; protect recovery dependencies.')
    if not details.get('retained_only') and not progress.get('validated'):
        cf = cloud.client('cloudformation')
        response = cf.describe_stacks(StackName=details['stack_id'])
        if response['Stacks'][0]['StackId'] != details['stack_id']:
            raise UninstallError('Stack identity changed.')
        template = cf.get_template(StackName=details['stack_id'], TemplateStage='Original')['TemplateBody']
        if isinstance(template, str):
            template = json.loads(template)
        if template != details['template']:
            raise UninstallError('Stack template changed since review.')
        actual = {(item['LogicalResourceId'], item['ResourceType'], item.get('PhysicalResourceId')) for item in _pages(cf, 'list_stack_resources', 'StackResourceSummaries', StackName=details['stack_id']) if item.get('ResourceStatus') != 'DELETE_COMPLETE'}
        expected = {(item['logical'], item['type'], item['physical']) for item in details['resources']}
        if actual != expected:
            raise UninstallError('Stack inventory changed since review.')
        progress['validated'] = True
        save()
    if not details.get('retained_only') and not progress.get('steps', {}).get('cluster', {}).get('done'):
        _frozen_control(cloud, details)
    if details['mode'] == 'retire':
        snapshot = _step(progress, save, 'snapshot', lambda entry: _snapshot(cloud, details, entry, save))
        _step(progress, save, 'recovery', lambda entry: _recovery(cloud, details, snapshot, operation_dir))
    # Persistent lifecycle maintenance has drained cooperating writers. Disable
    # direct Lambda execution too before deleting any database or retained data.
    for item in details['resources']:
        if item['type'] == 'AWS::Lambda::Function':
            _step(progress, save, 'disable:' + item['physical'], lambda entry, item=item:
                  _call(cloud.client('lambda'), 'put_function_concurrency', FunctionName=item['physical'], ReservedConcurrentExecutions=0) or {'status': 'disabled'})
    if not details.get('retained_only'):
        if not progress.get('steps', {}).get('cluster', {}).get('done'):
            _frozen_control(cloud, details)
        _delete_database(cloud, details, progress, save)
        _delete_network(cloud, details, progress, save)
    # Deployment login is independent. Stack admin remains until DB/network
    # removal is reconciled; CF now cleans IAM inline policies, API, logs, Lambda,
    # budget and other non-retained resources using its real dependency graph.
    if not details.get('retained_only'):
        _delete_stack(cloud, details, progress, save)
    pending = []
    if details['mode'] == 'purge':
        for bucket in details['buckets']:
            _step(progress, save, 'bucket:' + bucket['name'], lambda entry, bucket=bucket: _purge_bucket(cloud, bucket, details, entry, save))
        for item in details['selected']:
            if item['kind'] == 'bucket':
                _step(progress, save, 'bucket:' + item['inventory']['name'], lambda entry, item=item: _purge_bucket(cloud, item['inventory'], details, entry, save))
        for snapshot in details['snapshots']:
            arn = snapshot['DBClusterSnapshotArn']
            def delete_snapshot(entry, arn=arn):
                rds = cloud.client('rds')
                response = _call(rds, 'describe_db_cluster_snapshots', DBClusterSnapshotIdentifier=arn)
                if not response:
                    return {'status': 'removed'}
                if (response['DBClusterSnapshots'][0]['DBClusterSnapshotArn'] != arn
                        or _jsonable(response['DBClusterSnapshots'][0].get('SnapshotCreateTime')) != snapshot.get('SnapshotCreateTime')):
                    raise UninstallError('Snapshot identity changed.')
                if response['DBClusterSnapshots'][0]['Status'] != 'deleting':
                    rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=arn)
                for _ in range(_POLL_ATTEMPTS):
                    if not _call(rds, 'describe_db_cluster_snapshots', DBClusterSnapshotIdentifier=arn):
                        return {'status': 'removed'}
                _pending(progress, save, 'Selected snapshot deletion is pending.')
            _step(progress, save, 'snapshot-delete:' + arn, delete_snapshot)
        # Ciphertext disappears before its wrapping keys/optional credentials.
        for item in details['selected']:
            if item['kind'] == 'parameters':
                _step(progress, save, 'parameters:' + item['prefix'], lambda entry, item=item: _purge_parameters(cloud, item, entry, save))
        for backup in details['automated_backups']:
            arn = backup['DBClusterAutomatedBackupsArn']
            def delete_automated(entry, backup=backup):
                rds = cloud.client('rds')
                resource_id = backup['DbClusterResourceId']
                def current():
                    try:
                        return _pages(rds, 'describe_db_cluster_automated_backups', 'DBClusterAutomatedBackups', DbClusterResourceId=resource_id)
                    except Exception as error:
                        if _code(error) == 'DBClusterAutomatedBackupNotFoundFault':
                            return []
                        raise
                records = current()
                if not records:
                    return {'status': 'removed'}
                if len(records) != 1 or records[0]['DBClusterAutomatedBackupsArn'] != backup['DBClusterAutomatedBackupsArn'] or _jsonable(records[0].get('ClusterCreateTime')) != backup.get('ClusterCreateTime'):
                    raise UninstallError('Automated backup identity changed.')
                rds.delete_db_cluster_automated_backup(DbClusterResourceId=resource_id)
                for _ in range(_POLL_ATTEMPTS):
                    if not current():
                        return {'status': 'removed'}
                _pending(progress, save, 'Selected automated backup deletion is pending.')
            _step(progress, save, 'automated-backup:' + arn, delete_automated)
        secrets = details['secrets'] + [item for item in details['selected'] if item['kind'] == 'secret']
        for item in secrets:
            result = _step(progress, save, 'secret:' + item['arn'], lambda entry, item=item: _schedule_secret(cloud, item['arn'], entry, save, item))
            if result['status'] == 'pending-recovery-window':
                pending.append(result)
    if pending:
        progress['pending_recovery_windows'] = pending
        _pending(progress, save, 'Secrets remain inside AWS recovery windows; deletion is scheduled, not complete.')
    progress.pop('pending', None)
    progress.pop('pending_recovery_windows', None)
    save()
    return {'status': 'retired' if details['mode'] == 'retire' else 'purged',
            'instance_id': details['instance_id'], 'pending_recovery_windows': pending,
            'recovery': progress.get('steps', {}).get('recovery', {}).get('result'),
            'live_restore_verified': False,
            'retained_notice': 'Unselected snapshots, encryption keys and protected/shared resources remain; storage charges may continue.'}
