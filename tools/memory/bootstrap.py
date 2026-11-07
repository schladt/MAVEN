"""Owner-invoked installation using exact reviewed CloudFormation change sets.

No login, root fallback, credential export, IAM elevation or implicit execution.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .client import OPTIONAL_CONFIG_KEYS, MemoryClient, MemoryClientError, read_json, selected_session, validate_config
from .deployment import OUTPUT_CONFIG, build
from .environment import load_environment, selected_aws_session, transport_ca_bundle


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_private(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise MemoryClientError('configuration_must_be_owner_private')
    return read_json(path, maximum=4 * 1024 * 1024)


def _cloud(values):
    session = selected_aws_session(values, deploy=True, region=values.get('MAVEN_AWS_REGION', 'us-east-1'))
    return session.client('cloudformation', verify=transport_ca_bundle(),
                          config=Config(connect_timeout=3, read_timeout=15,
                                        retries={'total_max_attempts': 1}))


def prepare(values, destination):
    try:
        return build(destination, notification_email=values['MAVEN_NOTIFICATION_EMAIL'],
                     engine_version=values.get('MAVEN_ENGINE_VERSION', '18.6'),
                     instance_name=values['MAVEN_INSTANCE_NAME'], account_id=values['MAVEN_ACCOUNT_ID'],
                     caller_principal_arn=values['MAVEN_CALLER_PRINCIPAL_ARN'],
                     instance_id=values.get('MAVEN_INSTANCE_ID', values.get('MAVEN_MEMORY_INSTANCE_ID')),
                     recovery_epoch=values.get('MAVEN_RECOVERY_EPOCH', values.get('MAVEN_MEMORY_RECOVERY_EPOCH')),
                     availability_zones=values['MAVEN_AVAILABILITY_ZONES'].split(','))
    except (KeyError, ValueError, OSError):
        raise MemoryClientError('installation_settings_invalid') from None


def _package(directory):
    directory = Path(directory)
    manifest = _read_private(directory / 'manifest.json')
    if manifest.get('required_inputs'):
        raise MemoryClientError('installation_settings_incomplete')
    for name, expected in manifest['artifact_sha256'].items():
        if Path(name).name != name:
            raise MemoryClientError('installation_package_invalid')
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise MemoryClientError('installation_package_changed')
    template = (directory / 'template.json').read_text(encoding='utf-8')
    parameters = json.loads((directory / 'parameters.json').read_text(encoding='utf-8'))
    if len(template.encode()) > 51200:
        raise MemoryClientError('installation_template_requires_reviewed_upload')
    return manifest, template, parameters


def _describe_changes(cloud, change_set, stack):
    first, changes, token = None, [], None
    for _ in range(100):
        args = {'ChangeSetName': change_set, 'StackName': stack}
        if token:
            args['NextToken'] = token
        result = cloud.describe_change_set(**args)
        if first is None:
            first = result
        changes.extend(result.get('Changes', []))
        token = result.get('NextToken')
        if not token:
            return {**first, 'Changes': changes}
    raise MemoryClientError('installation_review_too_large')


def _review_fields(change):
    return {key: change.get(key) for key in ('ChangeSetId', 'StackId', 'ChangeSetName', 'StackName',
            'Parameters', 'Capabilities', 'RoleARN', 'Changes', 'IncludeNestedStacks', 'OnStackFailure')}


def plan(values, package, review_path, *, change_type, budget=300):
    """Create only a reviewable change set. Exact execution is a separate command."""
    if change_type not in ('CREATE', 'UPDATE'):
        raise MemoryClientError('invalid_change_set_type')
    manifest, template, parameters = _package(package)
    cloud = _cloud(values)
    stack = values['MAVEN_STACK_NAME']
    name = 'maven-reviewed-' + str(uuid.uuid4())
    token = str(uuid.uuid4())
    # Persist the stable names before dispatch, so timeout reconciliation never creates
    # a second change set. The owner can describe this exact name after uncertainty.
    pending_path = Path(str(review_path) + '.pending')
    identity = {'stack': stack, 'change_type': change_type, 'manifest_sha256': _digest(manifest),
                'template_sha256': hashlib.sha256(template.encode()).hexdigest()}
    if pending_path.exists():
        pending = _read_private(pending_path)
        if any(pending.get(key) != value for key, value in identity.items()):
            raise MemoryClientError('pending_change_set_mismatch')
        name = pending['change_set']
    else:
        pending = {'status': 'change_set_pending', **identity, 'change_set': name, 'request_token': token}
        _private_json(pending_path, pending)
        try:
            cloud.create_change_set(StackName=stack, ChangeSetName=name, ChangeSetType=change_type,
                                    TemplateBody=template, Parameters=parameters,
                                    Capabilities=manifest['capabilities_required'], ClientToken=token)
        except (BotoCoreError, ClientError):
            # Reconcile by exact stable name; never repeat create after ambiguity.
            pass
    deadline = time.monotonic() + min(max(budget, 1), 3600)
    while time.monotonic() < deadline:
        try:
            change = _describe_changes(cloud, name, stack)
        except (BotoCoreError, ClientError):
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            continue
        if change.get('Status') == 'FAILED':
            raise MemoryClientError('change_set_failed')
        if change.get('Status') == 'CREATE_COMPLETE':
            if change.get('ExecutionStatus') != 'AVAILABLE':
                raise MemoryClientError('change_set_not_available')
            review = {'format_version': 1, 'change_type': change_type, 'change_set': _review_fields(change),
                      'template_sha256': hashlib.sha256(template.encode()).hexdigest(),
                      'manifest_sha256': _digest(manifest), 'instance_id': manifest['deployment_inputs']['InstanceId'],
                      'recovery_epoch': manifest['deployment_inputs']['RecoveryEpoch']}
            digest = _digest(review)
            document = {'review': review, 'review_sha256': digest}
            if Path(review_path).exists():
                if _read_private(review_path) != document:
                    raise MemoryClientError('existing_review_mismatch')
            else:
                _private_json(review_path, document)
            return {'status': 'review_required', 'review_sha256': digest,
                    'change_set_id': change['ChangeSetId'], 'review_path': str(review_path)}
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise MemoryClientError('change_set_outcome_unknown')


def _outputs(stack):
    try:
        pairs = stack['Outputs']
        outputs = {item['OutputKey']: item['OutputValue'] for item in pairs}
        if len(outputs) != len(pairs):
            raise ValueError('duplicate')
        config = {field: outputs[key] for key, field in OUTPUT_CONFIG.items()
                  if field not in OPTIONAL_CONFIG_KEYS or key in outputs}
        config['schema_version'] = int(config['schema_version'])
        return validate_config(config)
    except (KeyError, ValueError, TypeError):
        raise MemoryClientError('invalid_stack_outputs') from None


def connect(values, environment_path, *, confirm_instance, cloud=None):
    """Bind reviewed stack outputs into the existing private config, never credentials."""
    cloud = _cloud(values) if cloud is None else cloud
    stacks = cloud.describe_stacks(StackName=values['MAVEN_STACK_NAME'])['Stacks']
    if len(stacks) != 1 or stacks[0].get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise MemoryClientError('stack_not_ready')
    config = _outputs(stacks[0])
    if config['instance_id'] != confirm_instance:
        raise MemoryClientError('instance_confirmation_mismatch')
    for field in ('instance_id', 'recovery_epoch'):
        previous = values.get('MAVEN_MEMORY_' + field.upper())
        if previous is not None and previous != config[field]:
            raise MemoryClientError('existing_connection_mismatch')
    updated = {**values, **{'MAVEN_MEMORY_' + key.upper(): str(value) for key, value in config.items()}}
    # Recheck privacy and exact content immediately before atomic replacement.
    path = Path(environment_path).expanduser()
    if load_environment(path) != values:
        raise MemoryClientError('configuration_changed')
    fd, temporary = tempfile.mkstemp(prefix='.maven-env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            for key, value in sorted(updated.items()):
                stream.write(key + '=' + json.dumps(value, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'status': 'connected', 'instance_id': config['instance_id'], 'schema_version': config['schema_version']}


def apply(values, package, review_path, environment_path, *, confirm_review, budget=1800):
    """Execute only the immutable reviewed change set, boundedly reconcile, connect."""
    manifest, template, _ = _package(package)
    document = _read_private(review_path)
    review = document['review']
    digest = _digest(review)
    if digest != document.get('review_sha256') or digest != confirm_review:
        raise MemoryClientError('review_confirmation_mismatch')
    if (_digest(manifest) != review['manifest_sha256']
            or hashlib.sha256(template.encode()).hexdigest() != review['template_sha256']):
        raise MemoryClientError('installation_package_changed')
    cloud = _cloud(values)
    expected = review['change_set']
    if expected['StackName'] != values['MAVEN_STACK_NAME']:
        raise MemoryClientError('review_target_mismatch')
    change = _describe_changes(cloud, expected['ChangeSetId'], expected['StackId'])
    if _review_fields(change) != expected:
        raise MemoryClientError('change_set_changed')
    actual = cloud.get_template(StackName=expected['StackId'], ChangeSetName=expected['ChangeSetId'],
                                TemplateStage='Original')['TemplateBody']
    actual = json.loads(actual) if isinstance(actual, str) else actual
    if actual != json.loads(template):
        raise MemoryClientError('change_set_template_mismatch')
    execution = change.get('ExecutionStatus')
    if execution == 'AVAILABLE':
        try:
            cloud.execute_change_set(StackName=expected['StackId'], ChangeSetName=expected['ChangeSetId'],
                                     ClientRequestToken='maven-' + digest)
        except (BotoCoreError, ClientError):
            pass  # Describe only: never blindly retry an uncertain execution.
    elif execution not in ('EXECUTE_IN_PROGRESS', 'EXECUTE_COMPLETE'):
        raise MemoryClientError('change_set_not_available')
    deadline = time.monotonic() + min(max(budget, 1), 3600)
    while time.monotonic() < deadline:
        try:
            current = _describe_changes(cloud, expected['ChangeSetId'], expected['StackId'])
            stack = cloud.describe_stacks(StackName=expected['StackId'])['Stacks'][0]
        except (BotoCoreError, ClientError, KeyError, IndexError):
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            continue
        status = stack.get('StackStatus', '')
        if 'ROLLBACK' in status or status.endswith('_FAILED') or status.startswith('DELETE_'):
            raise MemoryClientError('stack_execution_failed')
        if current.get('ExecutionStatus') == 'EXECUTE_COMPLETE' and status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
            if _outputs(stack)['recovery_epoch'] != review['recovery_epoch']:
                raise MemoryClientError('stack_output_identity_mismatch')
            return connect(values, environment_path, confirm_instance=review['instance_id'], cloud=cloud)
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise MemoryClientError('stack_execution_outcome_unknown')


def initialize(values, *, confirm_instance, upgrade=False):
    from .admin import bootstrap, migrate
    from .environment import memory_config
    config = memory_config(values)
    if config['instance_id'] != confirm_instance:
        raise MemoryClientError('instance_confirmation_mismatch')
    operation = migrate if upgrade else bootstrap
    result = operation(config, selected_session(config, admin=True, environment=values))
    client = MemoryClient(config, selected_session(config, environment=values))
    try:
        context = client.load_context()
    finally:
        client.close()
    return {**result, 'authority_generation': context['data']['authority_generation'],
            'minimum_client_protocol': context['data']['minimum_client_protocol']}
