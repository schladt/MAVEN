"""Shared private setup artifacts, prompts and connection verification."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from tools.interaction import Field, checkpoint, require


class SetupPending(Exception):
    """A safe stop, not a successful setup."""


def provision_fields(values=None):
    """Editable deployment suggestions; none assert live AWS availability."""
    from dataclasses import replace
    fields = [
        Field('MAVEN_STACK_NAME', 'New CloudFormation stack name',
              help='AWS uses this name to manage the deployment as one group of resources.'),
        Field('MAVEN_INSTANCE_NAME', 'MAVEN instance name',
              default_from='MAVEN_STACK_NAME', pattern=r'[a-z][a-z0-9-]{0,30}[a-z0-9]',
              help='Defaults to the stack name when compatible; edit to override. Use 2–32 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit. If the stack name does not fit, enter a separate instance name.'),
        Field('MAVEN_NOTIFICATION_EMAIL', 'Notification email'),
        Field('MAVEN_AVAILABILITY_ZONES', 'Two distinct us-east-1 zones, comma-separated',
              'us-east-1a, us-east-1b',
              help='Suggested zones, not verified for your account. Confirm availability and edit if needed; spaces after commas are accepted.'),
        Field('MAVEN_ENGINE_VERSION', 'Aurora PostgreSQL version', '18.6',
              help='Suggested version, not verified as available in AWS. Confirm regional availability and Data API/auto-pause support. MAVEN requires PostgreSQL 18 patch >=18.3.'),
    ]
    return [replace(field, default=(values or {}).get(field.name) or field.default) for field in fields]


def ask(prompt, default=None):
    values = require().form(prompt, [Field('value', prompt, default or '')])
    if values is None:
        raise SetupPending('Selection paused; existing artifacts are preserved.')
    return values['value']


def confirm(prompt):
    return require().confirm(prompt)


def approve(prompt, token, *, body='', destructive=False):
    checkpoint()
    if not require().review(prompt, body or str(token), identity=str(token),
                            destructive=destructive):
        raise SetupPending('Approval declined; no subsequent action was performed.')


def private_directory(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        private_directory(path.parent) if not path.parent.exists() else None
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise SetupPending(f'Use an owner-owned, non-symlink directory with mode 0700: {path}')
    # A private leaf underneath a symlink is not an independent installation.
    if path.absolute() != path.resolve():
        raise SetupPending(f'Select a path without symlink components: {path}')


def read_private(path, *, maximum=4 * 1024 * 1024):
    from tools.memory.client import MemoryClientError, read_json
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise MemoryClientError('setup_artifact_must_be_owner_private')
    finally:
        os.close(fd)
    return read_json(path, maximum=maximum)


def atomic_private(path, text):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise SetupPending(f'Existing file must be owner-private (0600), not a symlink: {path}')
    fd, temporary = tempfile.mkstemp(prefix='.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, document):
    atomic_private(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


@contextmanager
def setup_lock(directory):
    path = directory / 'lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise SetupPending('The setup lock must be an owner-private regular file.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupPending('Another setup is running for this home. Close it before resuming.') from None
        yield
    finally:
        os.close(fd)


def verify_connection(env, project_id=None, *, values=None):
    from tools.memory.environment import configured_client
    from tools.memory.reconcile_cloud import status
    client = configured_client(env, values=values)
    try:
        verified = status(client)
        if project_id:
            record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
            if (record.get('record_id') != project_id or record.get('project_id') is not None
                    or record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                    or record.get('data', {}).get('entity_type') != 'project'
                    or 'archived' in record.get('tags', []) or record.get('data', {}).get('state') == 'archived'):
                raise SetupPending('The selected cloud record is not an active owner project entity.')
            verified = {**verified, 'project_name': record['data'].get('name')}
        return verified
    finally:
        client.close()


def expected_account(values):
    """Derive the account only from saved bindings, never from ambient credentials."""
    accounts = set()
    if values.get('MAVEN_ACCOUNT_ID'):
        accounts.add(values['MAVEN_ACCOUNT_ID'])
    for key in ('MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_ROLE_ARN',
                'MAVEN_CALLER_PRINCIPAL_ARN', 'MAVEN_MEMORY_CLUSTER_ARN',
                'MAVEN_MEMORY_CLIENT_ROLE_ARN', 'MAVEN_MEMORY_ADMIN_ROLE_ARN'):
        value = values.get(key, '')
        if value.startswith('arn:'):
            accounts.add(value.split(':')[4])
    if len(accounts) > 1:
        raise SetupPending('Saved/imported account bindings disagree; preserve the settings and select their original account.')
    return next(iter(accounts), 'Unknown — confirm after sign-in')


def check_login_identity(values, identity, *, deploy=False):
    from tools.memory.environment import deployment_mode
    key = ('MAVEN_DEPLOY_ROLE_ARN' if deploy and deployment_mode(values) == 'role' else
           'MAVEN_DEPLOY_EXPECTED_PRINCIPAL' if deploy else 'MAVEN_AWS_EXPECTED_PRINCIPAL')
    expected = values.get(key)
    account = expected_account(values)
    if (expected and identity['Arn'] != expected) or (
            account.isdigit() and identity['Account'] != account):
        raise SetupPending('Actual AWS account ' + identity['Account'] + ', principal ' + identity['Arn'] +
                           ' differs from the saved/imported binding (account ' + account +
                           ', principal ' + (expected or 'not yet selected') +
                           '). Sign in to the expected identity; no binding was replaced.')
    if not deploy and identity['Arn'].endswith(':root'):
        raise SetupPending('Root cannot be the daily-use identity.')


def database_status(values):
    """Read-only wake probe through the SQL administration role: 'awake', 'warming', or an error code.

    The SELECT itself starts an auto-paused Aurora cluster resuming; it never changes MAVEN state.
    """
    from botocore.config import Config
    from tools.memory.client import selected_session
    from tools.memory.environment import migration_config, transport_ca_bundle
    try:
        config = migration_config(values)
        session = selected_session(config, admin=True, environment=values)
        client = session.client('rds-data', verify=transport_ca_bundle(), config=Config(
            connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
        try:
            client.execute_statement(resourceArn=config['cluster_arn'], secretArn=config['admin_secret_arn'],
                                     database=config['database'], sql='SELECT 1')
        finally:
            client.close()
        return 'awake'
    except Exception as error:
        from tools.memory.client import aws_error
        return aws_error(error).code


def wait_for_database(values, *, budget=60.0, interval=2.0, checkpoint=None, on_warming=None):
    """Bounded read-only SELECT progression, never an administration-step retry.

    SQL administration credentials are needed only for this optional diagnostic,
    not ordinary memory readiness. The budget is checked between requests.
    """
    from tools.memory.client import MemoryClientError, wait_for_read_only

    def probe():
        status = database_status(values)
        if status == 'warming':
            raise MemoryClientError('warming')
        return status

    try:
        return wait_for_read_only(probe, budget=budget, interval=interval,
                                  checkpoint=checkpoint, on_warming=on_warming)
    except MemoryClientError as error:
        if error.code != 'warming':
            raise
        return 'warming'


def verify_cloudformation_access(session):
    """Check deployment reads with the actual verified session, never ambient credentials."""
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from tools.memory.client import aws_error
    from tools.memory.environment import transport_ca_bundle
    try:
        client = session.client('cloudformation', verify=transport_ca_bundle(), config=Config(
            connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
        try:
            client.describe_account_limits()
        finally:
            client.close()
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None


def verify_deployment_access(values):
    """Setup may inspect an unbound profile, but never an unbound role source."""
    from tools.memory.environment import deployment_session
    session, identity = deployment_session(values, allow_unbound=True)
    check_login_identity(values, identity, deploy=True)
    verify_cloudformation_access(session)
    return identity


def permission_preflight(values, task):
    """Read/assume/simulate only. Results describe probes, not blanket permission."""
    from botocore.config import Config
    from tools.memory.environment import deployment_mode, selected_aws_session, transport_ca_bundle, migration_config
    checks = []
    limits = ['STS identity alone is not proof of permissions. No resource is created or deleted by these checks.']
    options = {'verify': transport_ca_bundle(), 'config': Config(
        connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1})}

    def probe(label, action, *, required=True):
        try:
            action()
            checks.append({'check': label, 'status': 'Passed'})
        except Exception as error:
            from tools.memory.client import aws_error
            code = aws_error(error).code
            denied = code == 'not_authorized'
            checks.append({'check': label, 'status': 'Missing access' if denied and required else 'Could not verify',
                           'reason': code})

    def call(session, service, method, **kwargs):
        client = session.client(service, **options)
        try:
            return getattr(client, method)(**kwargs)
        finally:
            client.close()

    if task in ('infrastructure', 'removal', 'metadata'):
        try:
            session = selected_aws_session(values, deploy=True, region=values.get('MAVEN_AWS_REGION', 'us-east-1'))
        except Exception as error:
            checks.append({'check': 'Resolve selected deployment access', 'status': 'Could not verify',
                           'reason': getattr(error, 'code', type(error).__name__)})
        else:
            if task in ('metadata', 'removal'):
                probe('Read the selected CloudFormation stack', lambda: call(session, 'cloudformation',
                      'describe_stacks', StackName=values['MAVEN_STACK_NAME']))
            else:
                probe('Read CloudFormation account limits', lambda: verify_cloudformation_access(session))
            if task != 'metadata':
                actions = (['cloudformation:CreateChangeSet', 'cloudformation:DescribeChangeSet',
                            'cloudformation:ExecuteChangeSet', 'cloudformation:DescribeStacks',
                            'iam:CreateRole', 'iam:PutRolePolicy', 'iam:PassRole',
                            'rds:CreateDBCluster', 'rds:CreateDBInstance', 'ec2:CreateVpc',
                            'ec2:CreateSubnet', 'ec2:CreateSecurityGroup', 's3:CreateBucket',
                            'lambda:CreateFunction', 'apigateway:POST', 'logs:CreateLogGroup',
                            'secretsmanager:CreateSecret', 'kms:CreateKey', 'sns:CreateTopic']
                           if task == 'infrastructure' else
                           ['cloudformation:DeleteStack', 'cloudformation:DescribeStacks',
                            'rds:DeleteDBCluster', 'rds:DeleteDBInstance', 's3:DeleteBucket',
                            'iam:DeleteRole', 'lambda:DeleteFunction'])
                role_mode = deployment_mode(values) == 'role'
                principal = values['MAVEN_DEPLOY_ROLE_ARN' if role_mode else 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL']
                if role_mode or (principal.startswith('arn:aws:iam:') and ':user/' in principal):
                    def simulate():
                        result = call(session, 'iam', 'simulate_principal_policy',
                                      PolicySourceArn=principal, ActionNames=actions, ResourceArns=['*'])
                        rows = result.get('EvaluationResults', [])
                        for row in rows:
                            decision = row.get('EvalDecision')
                            checks.append({'check': 'IAM simulation: ' + row['EvalActionName'],
                                'status': ('Could not verify' if row.get('MissingContextValues') else
                                           'Passed' if decision == 'allowed' else
                                           'Missing access' if decision == 'explicitDeny' else 'Could not verify'),
                                'reason': decision, 'missing_context': row.get('MissingContextValues', [])})
                        if result.get('IsTruncated') or {row['EvalActionName'] for row in rows} != set(actions):
                            raise SetupPending('Simulation incomplete; inspect the exact required actions with your IAM administrator.')
                    probe('Infrastructure IAM simulation coverage', simulate, required=False)
                else:
                    checks.append({'check': 'Infrastructure IAM simulation', 'status': 'Could not verify',
                                   'reason': 'The selected principal is not an IAM user; no role ARN is guessed from an STS session.'})
                limits.append('Simulation covers representative infrastructure actions against *, not every template dependency, resource condition, SCP, boundary, service quota, or runtime policy. Implicit denials at wildcard scope and inability to call IAM simulation mean Could not verify, not proof of missing target access. It cannot guarantee deployment/removal.')
    elif task == 'sql':
        from tools.memory.client import selected_session
        config = migration_config(values)
        role = 'admin'
        try:
            session = selected_session(config, admin=True, environment=values)
            checks.append({'check': 'Assume exact ' + role + ' role from daily-use login', 'status': 'Passed'})
        except Exception as error:
            checks.append({'check': 'Assume exact ' + role + ' role from daily-use login',
                           'status': 'Could not verify', 'reason': getattr(error, 'code', type(error).__name__)})
        else:
            probe('Read database through the existing SQL administration role', lambda: call(
                session, 'rds-data', 'execute_statement', resourceArn=config['cluster_arn'],
                secretArn=config['admin_secret_arn'], database=config['database'], sql='SELECT 1'))
        limits.append('Role assumption and reads do not prove write/DDL permissions. Existing role trusts are unchanged; SQL administration uses the selected daily-use principal.')
    elif task == 'routine':
        probe('Verify signed project reconciliation capability through the daily-use role',
              lambda: verify_connection(None, values=values))
    else:
        raise SetupPending('Unknown permission preflight.')
    status = ('Missing access' if any(item['status'] == 'Missing access' for item in checks) else
              'Could not verify' if any(item['status'] == 'Could not verify' for item in checks) else 'Passed')
    return {'status': status, 'task': task, 'checks': checks, 'limitations': limits,
            'remediation': 'Ask the account/IAM owner to inspect the named role, actions, resource policies, conditions and organization controls. Correct the selected profile or permissions, then retry this exact operation. Do not grant blanket administrator access just to silence a check.'}


def inspect_login_identity(values, *, deploy=False):
    """Inspect the session-owned login cache, permitting initial unbound setup."""
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from tools.memory.client import aws_error
    from tools.memory.environment import deployment_session, login_session, transport_ca_bundle
    if deploy:
        _, identity = deployment_session(values, allow_unbound=True)
        check_login_identity(values, identity, deploy=True)
        return identity
    session = login_session(values, deploy=deploy, region=values.get('MAVEN_AWS_REGION', 'us-east-1'))
    try:
        client = session.client('sts', verify=transport_ca_bundle(), config=Config(
            connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
        try:
            result = client.get_caller_identity()
        finally:
            client.close()
        identity = {key: result[key] for key in ('Arn', 'Account', 'UserId') if key in result}
        check_login_identity(values, identity, deploy=deploy)
        return identity
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
