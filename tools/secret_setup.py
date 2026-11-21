"""Required, explicitly reviewed secrets-store setup and integration repair.

Only locators and AWS receipts are journaled. The separately retained bucket stack
and wrapping parameter are never part of memory-service deletion.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from secrets import token_bytes
from uuid import UUID

from botocore.config import Config
from botocore.exceptions import ClientError

from tools.interaction import Field
from tools.setup_common import (SetupPending, check_login_identity, expected_account,
                                inspect_login_identity, private_directory, setup_lock)

SECRETS_KEYS = frozenset({
    'MAVEN_SECRETS_BUCKET', 'MAVEN_SECRETS_KEY_ID', 'MAVEN_SECRETS_PARAMETER_PREFIX',
    'MAVEN_SECRETS_FILE_KEY_PREFIX', 'MAVEN_SECRETS_OBJECT_PREFIX',
})
# Integration locators, not host identity, login cache paths, or memory bindings.
HOST_KEYS = frozenset({
    'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_AWS_LOGIN_CACHE',
    'MAVEN_DEPLOY_PROFILE', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_LOGIN_CACHE',
    'MAVEN_DEPLOY_MODE', 'MAVEN_DEPLOY_ROLE_ARN',
    'MAVEN_AWS_REGION', 'MAVEN_STACK_NAME', 'MAVEN_NOTIFICATION_EMAIL', 'MAVEN_ENGINE_VERSION',
    'MAVEN_INSTANCE_NAME', 'MAVEN_ACCOUNT_ID', 'MAVEN_CALLER_PRINCIPAL_ARN',
    'MAVEN_AVAILABILITY_ZONES', 'MAVEN_INSTANCE_ID', 'MAVEN_RECOVERY_EPOCH',
})
INTEGRATION_KEYS = SECRETS_KEYS
TAG = 'maven-secrets-operation'


def integration_settings(values):
    """Select supported portable integration locators, retaining future integrations."""
    from tools.memory.environment import ENV_KEYS
    keys = ENV_KEYS - HOST_KEYS - {key for key in ENV_KEYS if key.startswith('MAVEN_MEMORY_')}
    return {key: value for key, value in values.items() if key in keys}


def validate_settings(values):
    selected = {key: values.get(key) for key in SECRETS_KEYS}
    if any(not isinstance(value, str) or not value for value in selected.values()):
        raise SetupPending('All five MAVEN_SECRETS locators are required. Repair the existing binding; missing values never authorize creating or adopting a store.')
    bucket = selected['MAVEN_SECRETS_BUCKET']
    if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket)
            or '..' in bucket or '.-' in bucket or '-.' in bucket
            or re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', bucket)):
        raise SetupPending('Select an exact valid S3 bucket name.')
    if not re.fullmatch(r'[0-9a-f]{16}', selected['MAVEN_SECRETS_KEY_ID']):
        raise SetupPending('The wrapping key ID must be exactly 16 lowercase hexadecimal characters.')
    for key in ('MAVEN_SECRETS_PARAMETER_PREFIX', 'MAVEN_SECRETS_FILE_KEY_PREFIX'):
        prefix = selected[key]
        if (not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)+', prefix)
                or len(prefix) > 850 or prefix.lower().startswith(('/aws/', '/ssm/'))
                or any(part in ('.', '..') for part in prefix.split('/'))):
            raise SetupPending('SSM prefixes must be explicit absolute slash-terminated namespaces, not resources or reserved paths.')
    first, second = selected['MAVEN_SECRETS_PARAMETER_PREFIX'], selected['MAVEN_SECRETS_FILE_KEY_PREFIX']
    if first.startswith(second) or second.startswith(first):
        raise SetupPending('Secret values and wrapping keys require separate nonoverlapping namespaces.')
    prefix = selected['MAVEN_SECRETS_OBJECT_PREFIX']
    if (not re.fullmatch(r'(?:[A-Za-z0-9_.-]+/)+', prefix) or len(prefix) > 850
            or any(part in ('.', '..') for part in prefix.split('/'))):
        raise SetupPending('The object prefix must be an explicit relative slash-terminated namespace.')
    return selected


def configured(values):
    try:
        validate_settings(values)
        return True
    except SetupPending:
        return False


def _client(session, service):
    from tools.memory.environment import transport_ca_bundle
    return session.client(service, verify=transport_ca_bundle(), config=Config(
        connect_timeout=3, read_timeout=15, retries={'total_max_attempts': 1}))


def _code(error):
    return error.response.get('Error', {}).get('Code') if isinstance(error, ClientError) else type(error).__name__


def identities(values):
    """Revalidate both explicitly selected identities and one exact account/region."""
    from tools.memory.environment import deployment_session, selected_aws_session
    account, region = expected_account(values), values.get('MAVEN_AWS_REGION')
    if not re.fullmatch(r'[0-9]{12}', account) or not region or region != values.get('MAVEN_MEMORY_REGION', region):
        raise SetupPending('Select the exact store account and the same region as this installation before reviewing access.')
    result, sessions = {}, {}
    for name, deploy in (('daily', False), ('maintenance', True)):
        if deploy:
            sessions[name], actual = deployment_session(values, region=region)
        else:
            actual = inspect_login_identity(values)
            sessions[name] = selected_aws_session(values, region=region)
        check_login_identity(values, actual, deploy=deploy)
        if actual['Account'] != account:
            raise SetupPending('Both selected identities must belong to the exact reviewed store account.')
        result[name] = actual
    return result, sessions


def _parameter_metadata(ssm, name):
    try:
        current = ssm.get_parameter(Name=name, WithDecryption=False)['Parameter']
    except ClientError as error:
        if _code(error) == 'ParameterNotFound':
            raise SetupPending('The exact wrapping parameter is missing. Repair the binding; never generate a replacement for an uncertain original write.') from None
        raise
    current.pop('Value', None)
    if current.get('Name') != name or current.get('Type') != 'SecureString' or current.get('DataType', 'text') != 'text':
        raise SetupPending('The exact wrapping parameter must be a SecureString with text data type.')
    # GetParameterHistory is scoped to this exact parameter; DescribeParameters
    # would require resource-wide discovery permissions.
    token = None
    seen = set()
    while True:
        response = ssm.get_parameter_history(Name=name, WithDecryption=False, MaxResults=50,
                                            **({'NextToken': token} if token else {}))
        selected = None
        for item in response.get('Parameters', []):
            item.pop('Value', None)
            if item.get('Name') == name and item.get('Version') == current.get('Version'):
                selected = item
        if selected is not None:
            return {key: selected[key] for key in ('Name', 'Type', 'KeyId', 'Version', 'DataType') if key in selected}
        token = response.get('NextToken')
        if not token or token in seen:
            raise SetupPending('The exact wrapping parameter version metadata is unavailable; retain the original binding.')
        seen.add(token)


def _retained_wrapping_parameters(ssm, settings, current, *, account, region):
    """Review historical key decryption permissions without fetching wrapping-key values."""
    namespace = settings['MAVEN_SECRETS_FILE_KEY_PREFIX']
    retained = {current['Name']: current}
    token, seen = None, set()
    while True:
        request = {'ParameterFilters': [{'Key': 'Name', 'Option': 'BeginsWith', 'Values': [namespace]}],
                   'MaxResults': 50}
        if token:
            request['NextToken'] = token
        page = ssm.describe_parameters(**request)
        for item in page.get('Parameters', []):
            name = item.get('Name', '')
            if not isinstance(name, str) or not name.startswith(namespace):
                raise SetupPending('Wrapping-key discovery returned a parameter outside the selected namespace.')
            if not re.fullmatch(r'[0-9a-f]{16}', name[len(namespace):]):
                continue
            if item.get('Type') != 'SecureString':
                raise SetupPending('Retained wrapping keys must remain SecureString parameters.')
            key = item.get('KeyId', '')
            if key != 'alias/aws/ssm' and (not isinstance(key, str) or not re.fullmatch(
                    rf'arn:aws:kms:{re.escape(region)}:{account}:key/[A-Za-z0-9-]+', key)):
                raise SetupPending('Every retained wrapping key must use alias/aws/ssm or an exact same-account regional KMS key ARN.')
            metadata = {field: item[field] for field in ('Name', 'Type', 'KeyId', 'Version', 'DataType') if field in item}
            if name != current['Name']:
                retained[name] = metadata
        token = page.get('NextToken')
        if not token:
            return [retained[name] for name in sorted(retained)]
        if not isinstance(token, str) or token in seen:
            raise SetupPending('Wrapping-key discovery pagination did not advance; no complete permission review is available.')
        seen.add(token)


def inspect_store(values, session):
    """Read exact resource metadata only; no parameter value enters a review."""
    settings = validate_settings(values)
    bucket, account, region = settings['MAVEN_SECRETS_BUCKET'], expected_account(values), values['MAVEN_AWS_REGION']
    s3, ssm = _client(session, 's3'), _client(session, 'ssm')
    try:
        args = {'Bucket': bucket, 'ExpectedBucketOwner': account}
        s3.head_bucket(**args)
        location = s3.get_bucket_location(**args).get('LocationConstraint') or 'us-east-1'
        if location == 'EU':
            location = 'eu-west-1'
        if location != region:
            raise SetupPending('Selected secrets bucket is in a different region.')
        block = s3.get_public_access_block(**args)['PublicAccessBlockConfiguration']
        if any(block.get(key) is not True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')):
            raise SetupPending('All four bucket public-access blocks must be enabled.')
        try:
            ownership = s3.get_bucket_ownership_controls(**args)['OwnershipControls']['Rules']
        except ClientError as error:
            if _code(error) != 'OwnershipControlsNotFoundError':
                raise
            ownership = []
        if ownership != [{'ObjectOwnership': 'BucketOwnerEnforced'}]:
            acl = s3.get_bucket_acl(**args)
            owner = acl.get('Owner', {}).get('ID')
            grants = acl.get('Grants')
            if (not isinstance(owner, str) or not owner or not isinstance(grants, list) or not grants
                    or any(grant.get('Permission') != 'FULL_CONTROL'
                           or grant.get('Grantee', {}).get('Type') != 'CanonicalUser'
                           or grant.get('Grantee', {}).get('ID') != owner for grant in grants)):
                raise SetupPending('Existing secrets bucket ACLs must grant access only to its verified bucket owner.')
            ownership = {'rules': ownership, 'acl': 'owner-only'}
        try:
            if s3.get_bucket_policy_status(**args)['PolicyStatus']['IsPublic']:
                raise SetupPending('The secrets bucket policy must not be public.')
        except ClientError as error:
            if _code(error) != 'NoSuchBucketPolicy':
                raise
        encryption = s3.get_bucket_encryption(**args)['ServerSideEncryptionConfiguration']['Rules']
        if len(encryption) != 1:
            raise SetupPending('The secrets bucket needs one explicit default encryption rule.')
        default = encryption[0]['ApplyServerSideEncryptionByDefault']
        if default.get('SSEAlgorithm') not in ('AES256', 'aws:kms'):
            raise SetupPending('Unsupported bucket encryption; review AES256 or an exact regional KMS key.')
        if default['SSEAlgorithm'] == 'aws:kms' and not re.fullmatch(
                rf'arn:aws:kms:{re.escape(region)}:{account}:key/[A-Za-z0-9-]+', default.get('KMSMasterKeyID', '')):
            raise SetupPending('KMS bucket encryption must name the exact same-account regional key ARN.')
        parameter = _parameter_metadata(ssm, settings['MAVEN_SECRETS_FILE_KEY_PREFIX'] + settings['MAVEN_SECRETS_KEY_ID'])
        key = parameter.get('KeyId')
        if key != 'alias/aws/ssm' and not re.fullmatch(
                rf'arn:aws:kms:{re.escape(region)}:{account}:key/[A-Za-z0-9-]+', key or ''):
            raise SetupPending('Wrapping SecureString encryption must use alias/aws/ssm or an exact same-account regional KMS key ARN.')
        retained = _retained_wrapping_parameters(ssm, settings, parameter, account=account, region=region)
        return {'bucket': bucket, 'account': account, 'region': location,
                'public_access_block': block, 'ownership': ownership, 'encryption': default,
                'wrapping_parameter': parameter, 'retained_wrapping_parameters': retained}
    finally:
        s3.close()
        ssm.close()


def required_policy(values, metadata=None):
    """Exact store resources only; prefixes are namespaces, not created resources."""
    selected = validate_settings(values)
    account, region = expected_account(values), values['MAVEN_AWS_REGION']
    bucket = 'arn:aws:s3:::' + selected['MAVEN_SECRETS_BUCKET']
    ssm = f'arn:aws:ssm:{region}:{account}:parameter'
    statements = [
        {'Effect': 'Allow', 'Action': ['ssm:GetParameter', 'ssm:GetParameterHistory', 'ssm:PutParameter'],
         'Resource': ssm + selected['MAVEN_SECRETS_PARAMETER_PREFIX'] + '*'},
        {'Effect': 'Allow', 'Action': ['ssm:GetParameter'],
         'Resource': ssm + selected['MAVEN_SECRETS_FILE_KEY_PREFIX'] + '*'},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': bucket,
         'Condition': {'StringLike': {'s3:prefix': selected['MAVEN_SECRETS_OBJECT_PREFIX'] + '*'}}},
        {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:GetObjectVersion', 's3:PutObject', 's3:AbortMultipartUpload'],
         'Resource': bucket + '/' + selected['MAVEN_SECRETS_OBJECT_PREFIX'] + '*'},
        # AWS does not support resource-level scoping for metadata enumeration.
        {'Effect': 'Allow', 'Action': ['ssm:DescribeParameters'], 'Resource': '*'},
    ]
    metadata = metadata or {}
    # Rotation changes only the current key ID, not the store. Historical references
    # still need the retained namespace and every reviewed backing KMS key.
    parameters = [metadata.get('wrapping_parameter', {}), *metadata.get('retained_wrapping_parameters', [])]
    wrapping_keys = sorted({item['KeyId'] for item in parameters if item.get('KeyId', '').startswith('arn:')})
    for wrapping in wrapping_keys:
        statements.append({'Effect': 'Allow', 'Action': ['kms:Decrypt'], 'Resource': wrapping,
                           'Condition': {'StringEquals': {'kms:ViaService': f'ssm.{region}.amazonaws.com'}}})
    encryption = metadata.get('encryption', {}).get('KMSMasterKeyID')
    if encryption:
        statements.append({'Effect': 'Allow', 'Action': ['kms:Decrypt', 'kms:GenerateDataKey'], 'Resource': encryption,
                           'Condition': {'StringEquals': {'kms:ViaService': f's3.{region}.amazonaws.com'}}})
    return {'Version': '2012-10-17', 'Statement': statements}


def maintenance_policy(values, operation_id, *, create=False):
    """Resource-scoped metadata rights, plus separately reviewed creation rights."""
    selected = validate_settings(values)
    account, region = expected_account(values), values['MAVEN_AWS_REGION']
    bucket = 'arn:aws:s3:::' + selected['MAVEN_SECRETS_BUCKET']
    wrapping = f'arn:aws:ssm:{region}:{account}:parameter' + selected['MAVEN_SECRETS_FILE_KEY_PREFIX'] + selected['MAVEN_SECRETS_KEY_ID']
    statements = [
        {'Effect': 'Allow', 'Action': ['s3:ListBucket', 's3:GetBucketLocation', 's3:GetBucketPublicAccessBlock',
                                     's3:GetBucketOwnershipControls', 's3:GetBucketAcl', 's3:GetBucketPolicyStatus', 's3:GetEncryptionConfiguration'],
         'Resource': bucket},
        {'Effect': 'Allow', 'Action': ['ssm:GetParameter', 'ssm:GetParameterHistory', 'ssm:ListTagsForResource'],
         'Resource': wrapping},
        {'Effect': 'Allow', 'Action': ['ssm:DescribeParameters'], 'Resource': '*'},
    ]
    if create:
        statements.extend([
            {'Effect': 'Allow', 'Action': ['cloudformation:CreateStack', 'cloudformation:DescribeStacks',
                                         'cloudformation:GetTemplate', 'cloudformation:DescribeStackResource', 'cloudformation:TagResource'],
             'Resource': f'arn:aws:cloudformation:{region}:{account}:stack/maven-secrets-{operation_id}/*'},
            {'Effect': 'Allow', 'Action': ['s3:CreateBucket', 's3:PutBucketPublicAccessBlock', 's3:PutBucketOwnershipControls',
                                         's3:PutEncryptionConfiguration', 's3:PutBucketTagging', 's3:GetBucketTagging'],
             'Resource': bucket},
            {'Effect': 'Allow', 'Action': ['ssm:PutParameter', 'ssm:AddTagsToResource'], 'Resource': wrapping},
        ])
    return {'Version': '2012-10-17', 'Statement': statements}


def bucket_template(settings, operation_id):
    return {'AWSTemplateFormatVersion': '2010-09-09',
            'Description': 'Independent retained MAVEN secrets store; not a memory-service resource.',
            'Resources': {'SecretFiles': {'Type': 'AWS::S3::Bucket', 'DeletionPolicy': 'Retain',
                'UpdateReplacePolicy': 'Retain', 'Properties': {
                    'BucketName': settings['MAVEN_SECRETS_BUCKET'],
                    'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]},
                    'PublicAccessBlockConfiguration': {key: True for key in (
                        'BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')},
                    'BucketEncryption': {'ServerSideEncryptionConfiguration': [
                        {'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]},
                    'Tags': [{'Key': TAG, 'Value': operation_id}],
                }}}}


def provision(values, session, state, persist):
    """Resume original ownership evidence; never regenerate an uncertain wrapping write."""
    selected = validate_settings(values)
    operation = state['operation_id']
    journal = state.setdefault('secrets_resources', {})
    stack_name = 'maven-secrets-' + operation
    template = bucket_template(selected, operation)
    cloud, ssm, s3 = (_client(session, name) for name in ('cloudformation', 'ssm', 's3'))
    try:
        if not journal.get('stack_submitted') or journal.get('stack_rejected'):
            # A collision is not an invitation to adopt an existing bucket/parameter.
            try:
                s3.head_bucket(Bucket=selected['MAVEN_SECRETS_BUCKET'])
            except ClientError as error:
                if _code(error) not in ('404', 'NoSuchBucket', 'NotFound'):
                    raise
            else:
                raise SetupPending('The proposed bucket already exists. New-store creation cannot adopt it.')
            try:
                found = ssm.get_parameter(Name=selected['MAVEN_SECRETS_FILE_KEY_PREFIX'] + selected['MAVEN_SECRETS_KEY_ID'],
                                          WithDecryption=False)
            except ClientError as error:
                if _code(error) != 'ParameterNotFound':
                    raise
            else:
                found.clear()
                raise SetupPending('The proposed wrapping parameter already exists. New-store creation cannot adopt it.')
            journal.update(stack_submitted=True, stack_name=stack_name, template_sha256=_digest(template))
            journal.pop('stack_rejected', None)
            persist()
            try:
                result = cloud.create_stack(StackName=stack_name, TemplateBody=json.dumps(template),
                    ClientRequestToken=operation, OnFailure='DO_NOTHING',
                    Tags=[{'Key': TAG, 'Value': operation}])
            except ClientError as error:
                if _code(error) in ('AccessDenied', 'AccessDeniedException'):
                    journal['stack_rejected'] = _code(error)
                    journal.setdefault('rejected_requests', []).append({'resource': 'stack', 'code': _code(error)})
                    persist()
                raise
            journal['stack_id'] = result['StackId']
            persist()
        stacks = cloud.describe_stacks(StackName=journal.get('stack_id', stack_name)).get('Stacks', [])
        if len(stacks) != 1:
            raise SetupPending('Original secrets stack creation outcome is unknown; retain its exact request and do not create another store.')
        stack = stacks[0]
        account, region = expected_account(values), values['MAVEN_AWS_REGION']
        if (not stack.get('StackId', '').startswith(f'arn:aws:cloudformation:{region}:{account}:stack/{stack_name}/')
                or {item['Key']: item['Value'] for item in stack.get('Tags', [])}.get(TAG) != operation
                or journal.get('template_sha256') != _digest(template)):
            raise SetupPending('Secrets stack ownership does not match this original operation.')
        actual = cloud.get_template(StackName=stack['StackId'], TemplateStage='Original')['TemplateBody']
        if isinstance(actual, str):
            actual = json.loads(actual)
        if actual != template:
            raise SetupPending('Secrets stack template differs from the exact reviewed resources.')
        journal['stack_id'] = stack['StackId']
        persist()
        if stack['StackStatus'] != 'CREATE_COMPLETE':
            raise SetupPending('Original secrets stack is ' + stack['StackStatus'] + '. Resume this operation after it settles; do not replace it.')
        resource = cloud.describe_stack_resource(StackName=stack['StackId'], LogicalResourceId='SecretFiles')['StackResourceDetail']
        if resource.get('PhysicalResourceId') != selected['MAVEN_SECRETS_BUCKET'] or resource.get('ResourceType') != 'AWS::S3::Bucket':
            raise SetupPending('The retained bucket is not owned by the original reviewed stack.')
        name = selected['MAVEN_SECRETS_FILE_KEY_PREFIX'] + selected['MAVEN_SECRETS_KEY_ID']
        if not journal.get('key_submitted') or journal.get('key_rejected'):
            if journal.get('key_rejected'):
                try:
                    found = ssm.get_parameter(Name=name, WithDecryption=False)
                except ClientError as error:
                    if _code(error) != 'ParameterNotFound':
                        raise
                else:
                    found.clear()
                    raise SetupPending('A parameter exists after a definitively rejected creation request; inspect its provenance instead of generating or adopting a key.')
            journal['key_submitted'] = True
            journal.pop('key_rejected', None)
            persist()  # Crash after here is never permission to generate another key.
            key = base64.b64encode(token_bytes(32)).decode('ascii')
            try:
                result = ssm.put_parameter(Name=name, Type='SecureString', KeyId='alias/aws/ssm',
                    Value=key, Overwrite=False, Tier='Standard', DataType='text',
                    Tags=[{'Key': TAG, 'Value': operation}])
            except ClientError as error:
                if _code(error) in ('AccessDenied', 'AccessDeniedException'):
                    journal['key_rejected'] = _code(error)
                    journal.setdefault('rejected_requests', []).append({'resource': 'wrapping_parameter', 'code': _code(error)})
                    persist()
                raise
            finally:
                del key
            journal['key_version'] = result['Version']
            persist()
        metadata = _parameter_metadata(ssm, name)
        tags = ssm.list_tags_for_resource(ResourceType='Parameter', ResourceId=name).get('TagList', [])
        if ({item['Key']: item['Value'] for item in tags}.get(TAG) != operation
                or metadata.get('Version') != 1 or journal.get('key_version', 1) != 1):
            raise SetupPending('Wrapping key ownership/version differs from this creation operation; never overwrite or regenerate it.')
        journal.update(key_version=1, wrapping_parameter=name, retained_independently=True)
        persist()
        return dict(journal)
    finally:
        cloud.close()
        ssm.close()
        s3.close()


def verify_use(values, session, metadata):
    """Read-only actual probes; write access remains explicitly unverified."""
    selected = validate_settings(values)
    ssm, s3 = _client(session, 'ssm'), _client(session, 's3')
    try:
        wrapping = ssm.get_parameter(Name=metadata['wrapping_parameter']['Name'], WithDecryption=True)['Parameter']
        try:
            key = base64.b64decode(wrapping['Value'], validate=True)
            if wrapping.get('Type') != 'SecureString' or len(key) != 32:
                raise ValueError('invalid')
        except (ValueError, TypeError):
            raise SetupPending('The selected wrapping SecureString is not a base64-encoded 32-byte key.') from None
        finally:
            wrapping.clear()
        del key
        s3.list_objects_v2(Bucket=selected['MAVEN_SECRETS_BUCKET'],
                          ExpectedBucketOwner=expected_account(values),
                          Prefix=selected['MAVEN_SECRETS_OBJECT_PREFIX'], MaxKeys=1)
        return {'status': 'read_access_verified', 'wrapping_key_decryption': True,
                's3_prefix_listing': True, 'write_access_verified': False,
                'limitations': 'Read-only checks do not prove SSM writes, S3 object reads/writes, multipart operations, or all IAM/KMS conditions. The account owner must grant the exact reviewed policy. No test secrets or objects were created.'}
    finally:
        ssm.close()
        s3.close()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _cloud_binding(wizard, identifier):
    from tools.memory.environment import configured_client
    if str(UUID(identifier)) != identifier:
        raise SetupPending('Select an exact cloud binding record UUID, never a search term or name.')
    client = configured_client(wizard.env, values=wizard.values)
    try:
        response = client.invoke('/v1/memory/read', {'record_id': identifier})
    finally:
        client.close()
    record = response.get('data', {})
    data = record.get('data', {})
    value = data.get('value', {})
    source = record.get('source')
    if (record.get('record_id') != identifier or record.get('kind') != 'setting'
            or record.get('data_scope') != 'owner' or record.get('project_archived') is True
            or not isinstance(source, dict) or not source.get('origin') or not source.get('excerpt')
            or not isinstance(value, dict) or value.get('type') != 'maven-secrets-binding'
            or value.get('account') != expected_account(wizard.values)
            or value.get('region') != wizard.values.get('MAVEN_AWS_REGION')):
        raise SetupPending('The exact cloud setting must be a sourced maven-secrets-binding for this account and region. It is reference data, not approval; the owner must review it here.')
    settings = integration_settings(value.get('settings', {}))
    validate_settings(settings)
    return settings, {'record_id': identifier, 'revision': record['revision'], 'source': source}


def _selection(wizard, state):
    mode = state['answers']['secrets_source']['mode']
    details = state['answers'].get('secrets_details', {})
    preserved = integration_settings(wizard.values)
    if mode == 'saved':
        return integration_settings(wizard.values), {'origin': 'current private settings'}
    if mode == 'import':
        from tools.environment_migrations import load_import
        path = Path(details['path']).expanduser()
        if not path.is_absolute():
            raise SetupPending('Select the absolute owner-private .env path to import.')
        values = load_import(wizard.home, path)
        if expected_account(values) != expected_account(wizard.values) or values.get('MAVEN_AWS_REGION', values.get('MAVEN_MEMORY_REGION')) != wizard.values['MAVEN_AWS_REGION']:
            raise SetupPending('Imported integration account/region differs; host identities and login paths are never imported.')
        return {**preserved, **integration_settings(values)}, {'origin': str(path)}
    if mode == 'cloud':
        settings, provenance = _cloud_binding(wizard, details['record_id'])
        return {**preserved, **settings}, provenance
    return {**preserved, **{key: details[key] for key in SECRETS_KEYS}}, {'origin': 'owner manual locators' if mode == 'manual' else 'owner reviewed new store'}


def _prior_settings(controller, state, wizard):
    """Use only explicit operation links, never scan for or adopt a newest store."""
    for identifier in (state.get('previous_secrets_operation'), wizard.state.get('secrets_operation'),
                       state.get('previous_setup_operation'), wizard.state.get('administration_operation')):
        if not identifier or identifier == state['operation_id']:
            continue
        previous = controller._load(identifier)
        result = previous.get('secrets_ready') or previous.get('outcome', {})
        settings = result.get('settings') or result.get('binding', {})
        retained = {key: settings[key] for key in SECRETS_KEYS if key in settings}
        if retained:
            return retained
    return {}


def advance(controller, state, wizard):
    """One durable administration checkpoint, shared by setup, Repair and TUI."""
    step = state['step']
    if step in ('start', 'secrets_start'):
        choices = [('Reuse and verify saved binding', 'saved'), ('Import integration locators from a private .env', 'import'),
                   ('Enter the five store locators manually', 'manual'), ('Read an exact cloud binding record (when accessible)', 'cloud')]
        if not any(key in wizard.values for key in SECRETS_KEYS) and not _prior_settings(controller, state, wizard):
            choices.append(('Create a separately retained secrets store', 'create'))
        controller._question(state, 'secrets_source', 'Required secrets store / integration Repair', [
            Field('mode', 'Source', 'saved' if configured(wizard.values) else 'manual', kind='select', choices=tuple(choices))],
            'secrets_select', description='The store is required before readiness. Missing existing values require repair, not automatic adoption. Import only portable integration locators; never login caches or host profiles. Cloud lookup is optional and never requires Home.')
    elif step == 'secrets_select':
        mode = state['answers']['secrets_source']['mode']
        if mode == 'saved':
            validate_settings(wizard.values)
            state['step'] = 'secrets_identity'
            return
        fields = []
        if mode == 'import':
            fields = [Field('path', 'Absolute private .env path')]
        elif mode == 'cloud':
            fields = [Field('record_id', 'Exact sourced cloud binding record UUID')]
        else:
            if mode == 'create' and (any(key in wizard.values for key in SECRETS_KEYS)
                                     or _prior_settings(controller, state, wizard)):
                raise SetupPending('An existing or partial store binding must be repaired, not replaced by new provisioning.')
            suffix = state['operation_id'].replace('-', '')[:16]
            defaults = {'MAVEN_SECRETS_BUCKET': 'maven-secrets-' + suffix,
                        'MAVEN_SECRETS_KEY_ID': suffix, 'MAVEN_SECRETS_PARAMETER_PREFIX': '/maven/' + suffix + '/values/',
                        'MAVEN_SECRETS_FILE_KEY_PREFIX': '/maven/' + suffix + '/file-keys/',
                        'MAVEN_SECRETS_OBJECT_PREFIX': 'encrypted-files/'} if mode == 'create' else {
                            **_prior_settings(controller, state, wizard), **wizard.values}
            fields = [Field(key, key, defaults.get(key, ''), help='Nonsecret locator only. Prefixes are namespaces, not AWS resources.') for key in sorted(SECRETS_KEYS)]
        controller._question(state, 'secrets_details', 'Select exact secrets-store locators', fields, 'secrets_identity')
    elif step == 'secrets_identity':
        deploy_mode = wizard.values.get('MAVEN_DEPLOY_MODE', 'profile')
        if deploy_mode == 'deferred':
            raise SetupPending('Configure deployment access before secrets-store administration. No store was changed.')
        fields = [Field('account', 'Exact AWS account', expected_account(wizard.values)),
                  Field('region', 'Exact AWS region', wizard.values.get('MAVEN_AWS_REGION', 'us-east-1'))]
        if deploy_mode == 'profile':
            for label, prefix, folder in (('daily', 'MAVEN_AWS_', 'aws-login-cache'), ('maintenance', 'MAVEN_DEPLOY_', 'aws-deploy-login-cache')):
                fields.extend([Field(label + '_profile', label.capitalize() + ' named login-only profile', wizard.values.get(prefix + 'PROFILE', '')),
                               Field(label + '_cache', label.capitalize() + ' private login cache', wizard.values.get(prefix + 'LOGIN_CACHE', str(wizard.home / 'runtime' / folder)))])
        controller._question(state, 'secrets_identity', 'Select daily and maintenance AWS access', fields, 'secrets_prepare',
            description=('Maintenance uses the selected role: ' + wizard.values['MAVEN_DEPLOY_ROLE_ARN']
                         + ' via daily profile ' + wizard.values['MAVEN_AWS_PROFILE']
                         if deploy_mode == 'role' else 'Select both named login profiles in the same account and region.')
                        + ' No IAM policy is attached automatically.')
    elif step == 'secrets_prepare':
        answer = state['answers']['secrets_identity']
        if not re.fullmatch(r'[0-9]{12}', answer['account']) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]', answer['region']):
            raise SetupPending('Select a valid exact AWS account and region.')
        current_account = expected_account(wizard.values)
        if current_account.isdigit() and current_account != answer['account']:
            raise SetupPending('Repair cannot change this installation account.')
        updates = {'MAVEN_ACCOUNT_ID': answer['account'], 'MAVEN_AWS_REGION': answer['region']}
        caches = []
        if wizard.values.get('MAVEN_DEPLOY_MODE') == 'role':
            if answer['region'] != wizard.values.get('MAVEN_AWS_REGION'):
                raise SetupPending('Use the region of the verified daily identity for the selected deployment role.')
        else:
            for label, prefix in (('daily', 'MAVEN_AWS_'), ('maintenance', 'MAVEN_DEPLOY_')):
                cache = Path(answer[label + '_cache']).expanduser()
                if not cache.is_absolute() or not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', answer[label + '_profile']):
                    raise SetupPending('Select named profiles and absolute owner-private login caches.')
                private_directory(cache)
                caches.append(cache)
                updates.update({prefix + 'PROFILE': answer[label + '_profile'], prefix + 'LOGIN_CACHE': str(cache)})
        if len(caches) != len(set(caches)):
            raise SetupPending('Daily and maintenance login caches must remain separate.')
        wizard.settings(updates)
        actual = {}
        for label, deploy in (('daily', False), ('maintenance', True)):
            actual[label] = inspect_login_identity(wizard.values, deploy=deploy)
        proposed_identity = {'MAVEN_AWS_EXPECTED_PRINCIPAL': actual['daily']['Arn']}
        if wizard.values.get('MAVEN_DEPLOY_MODE') != 'role':
            proposed_identity['MAVEN_DEPLOY_EXPECTED_PRINCIPAL'] = actual['maintenance']['Arn']
        settings, provenance = _selection(wizard, state)
        validate_settings(settings)
        values = {**wizard.values, **proposed_identity, **settings}
        checked, sessions = identities(values)
        create = state['answers']['secrets_source']['mode'] == 'create'
        policy = maintenance_policy(values, state['operation_id'], create=create)
        try:
            metadata = None if create else inspect_store(values, sessions['maintenance'])
        except ClientError as error:
            controller._handoff(state, 'Exact secrets metadata access is unavailable (' + _code(error) + '). Ask the account owner to review this resource-scoped read policy for the selected maintenance principal; no policy is attached.', [])
            state['handoff'].update(required_policy=maintenance_policy(values, state['operation_id']),
                                    principal=checked['maintenance']['Arn'], policy_review_required=True)
            return
        state['presentation'] = {'title': 'Required secrets store / Repair', 'purpose': 'Explicit independent secrets-store binding',
                                 'account': answer['account'], 'region': answer['region'],
                                 'principal': checked['daily']['Arn'], 'profile': values['MAVEN_AWS_PROFILE'],
                                 'verified': True, 'target': settings['MAVEN_SECRETS_BUCKET']}
        review = {'settings': settings, 'previous_settings': integration_settings(wizard.values), 'provenance': provenance,
                  'account': answer['account'], 'region': answer['region'], 'identities': checked,
                  'identity_settings': proposed_identity, 'environment': dict(wizard.values), 'metadata': metadata,
                  'create': create, 'daily_required_policy': required_policy(values, metadata),
                  'maintenance_required_policy': policy, 'maintenance_principal': checked['maintenance']['Arn'],
                  'policy_principal': checked['daily']['Arn'], 'iam_effect': 'No IAM policy attachment or broad grants. Account owner grants only reviewed resources to this exact selected principal.',
                  'permission_checks': 'Read exact resource metadata and enumerate the dedicated wrapping-key namespace, decrypt only '
                                       'the current wrapping key in process, and list the configured object prefix. No canary writes, '
                                       'object reads or secret rotations. Historical-key decryption and write access remain unverified; '
                                       'the account owner reviews/grants the scoped policy.',
                  'key_retention': 'The daily policy retains read access to the dedicated wrapping-key namespace and decryption '
                                   'under every discovered wrapping-parameter KMS key. Preserve all historical wrapping material '
                                   'and S3 KMS keys/permissions while references exist; changing the current key ID does not migrate old files.',
                  'retention': 'Secrets bucket stack and wrapping SecureString remain independently retained when memory service is removed. Prefixes are only namespaces. Storage/requests can incur charges.',
                  'template': bucket_template(settings, state['operation_id']) if create else None,
                  'wrapping_creation': 'Generate a 32-byte random base64 SecureString encrypted by alias/aws/ssm, version 1, tagged with this operation. Never persist plaintext or regenerate uncertain writes. Only a definitively rejected permission request may retry creation after external access repair and an exact absence check.' if create else None}
        state['secrets_candidate'] = review
        controller._review(state, 'secrets_review', 'Review exact secrets binding, resources, identities and read-only checks', review, 'secrets_apply')
    elif step == 'secrets_apply':
        review = state['secrets_candidate']
        controller._approved(state, 'secrets_review', review)
        settings, provenance = _selection(wizard, state)
        final_values = {**review['environment'], **review['identity_settings'], **review['settings']}
        if (settings != review['settings'] or provenance != review['provenance']
                or wizard.values not in (review['environment'], final_values)):
            raise SetupPending('Secrets binding, source, or local identity settings changed after review; retain this operation and review the exact selection again.')
        values = final_values
        actual, sessions = identities(values)
        if actual != review['identities']:
            raise SetupPending('The selected AWS identity changed after the exact secrets-store review.')
        if review['create']:
            state['effect_sequence'] = state.get('effect_sequence', 0) + 1
            controller._save(state)
        try:
            if review['create']:
                provision(values, sessions['maintenance'], state, lambda: controller._save(state))
            metadata = inspect_store(values, sessions['maintenance'])
            if not review['create'] and metadata != review['metadata']:
                raise SetupPending('Secrets resource metadata changed after review; no binding was saved.')
            probe = verify_use(values, sessions['daily'], metadata)
        except ClientError as error:
            controller._handoff(state, 'AWS refused the reviewed secrets action (' + _code(error) + '). Ask the account owner to grant only the exact reviewed principal/resources and resume this operation. No IAM grant or successful verification is claimed.', [])
            state['handoff']['required_policy'] = review['daily_required_policy']
            state['handoff']['principal'] = review['policy_principal']
            state['handoff']['maintenance_required_policy'] = review['maintenance_required_policy']
            state['handoff']['maintenance_principal'] = review['maintenance_principal']
            return
        if not review['create']:
            state['effect_sequence'] = state.get('effect_sequence', 0) + 1
            controller._save(state)
        wizard.settings({**review['identity_settings'], **settings})
        result = {'secrets_ready': True, 'settings': settings, 'account': review['account'], 'region': review['region'],
                  'metadata': metadata, 'access': probe, 'retained_independently': True}
        controller._receipt(state, 'secrets_verify', result)
        state['secrets_ready'] = result
        if state['kind'] == 'setup':
            state['step'] = 'verify'
        else:
            state.update(state='completed', outcome=result)
    else:
        raise SetupPending('Unknown secrets setup phase; preserve the original operation.')


def standalone(controller, state):
    from tools.setup import Wizard
    directory = controller.home / 'runtime' / 'setup'
    private_directory(directory)
    with setup_lock(directory):
        wizard = Wizard(controller.home)
        owner = wizard.state.get('administration_operation')
        if owner:
            original = controller._load(owner)
            if original['state'] != 'completed':
                raise SetupPending('Resume original setup operation ' + owner + '; its required secrets stage cannot be replaced by a separate Repair.')
        previous = wizard.state.get('secrets_operation')
        if previous and previous != state['operation_id']:
            original = controller._load(previous)
            if original['state'] != 'completed':
                raise SetupPending('Resume original secrets operation ' + previous + '; unresolved resource creation must not be replaced.')
            state['previous_secrets_operation'] = previous
            controller._save(state)
        if previous != state['operation_id']:
            wizard.state['secrets_operation'] = state['operation_id']
            wizard.save()
        advance(controller, state, wizard)
