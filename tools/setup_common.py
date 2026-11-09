"""Shared private setup artifacts, prompts and authority verification."""
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


def verify_authority(env, project_id=None, *, values=None):
    from tools.memory.environment import configured_client
    client = configured_client(env, values=values)
    try:
        scope = {'workflow': 'start-work', 'operation': 'start-work'}
        if project_id:
            scope['project_id'] = project_id
        context = client.load_context(scope)
        if not any(page.get('page_kind') == 'directive' for page in context['data']['instructions']):
            raise SetupPending('Routine cloud access succeeded, but applicable approved MAVEN authority is missing. Ask the instance owner to approve complete authority, then resume.')
        if project_id:
            record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
            if (record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                    or record.get('data', {}).get('entity_type') != 'project'):
                raise SetupPending('The selected cloud record is not an owner project entity.')
        return context['data']['authority_generation']
    finally:
        client.close()


def expected_account(values):
    """Derive the account only from saved bindings, never from ambient credentials."""
    accounts = set()
    if values.get('MAVEN_ACCOUNT_ID'):
        accounts.add(values['MAVEN_ACCOUNT_ID'])
    for key in ('MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL',
                'MAVEN_CALLER_PRINCIPAL_ARN', 'MAVEN_MEMORY_CLUSTER_ARN',
                'MAVEN_MEMORY_CLIENT_ROLE_ARN', 'MAVEN_MEMORY_ADMIN_ROLE_ARN'):
        value = values.get(key, '')
        if value.startswith('arn:'):
            accounts.add(value.split(':')[4])
    if len(accounts) > 1:
        raise SetupPending('Saved/imported account bindings disagree; preserve the settings and select their original account.')
    return next(iter(accounts), 'Unknown — confirm after sign-in')


def check_login_identity(values, identity, *, deploy=False):
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    expected = values.get(prefix + 'EXPECTED_PRINCIPAL')
    account = expected_account(values)
    if (expected and identity['Arn'] != expected) or (
            account.isdigit() and identity['Account'] != account):
        raise SetupPending('Actual AWS account ' + identity['Account'] + ', principal ' + identity['Arn'] +
                           ' differs from the saved/imported binding (account ' + account +
                           ', principal ' + (expected or 'not yet selected') +
                           '). Sign in to the expected identity; no binding was replaced.')
    if not deploy and identity['Arn'].endswith(':root'):
        raise SetupPending('Root cannot be the daily-use identity.')


def permission_preflight(values, task):
    """Read/assume/simulate only. Results describe probes, not blanket permission."""
    from botocore.config import Config
    from tools.memory.environment import selected_aws_session, transport_ca_bundle, migration_config
    checks = []
    limits = ['STS identity alone is not proof of permissions. No resource is created or deleted by these checks.']
    options = {'verify': transport_ca_bundle(), 'config': Config(
        connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1})}

    def probe(label, action, *, required=True):
        try:
            action()
            checks.append({'check': label, 'status': 'Passed'})
        except Exception as error:
            code = getattr(error, 'response', {}).get('Error', {}).get('Code', '')
            denied = code in ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation',
                              'ForbiddenException')
            checks.append({'check': label, 'status': 'Missing access' if denied and required else 'Could not verify',
                           'reason': code or getattr(error, 'code', type(error).__name__)})

    def call(session, service, method, **kwargs):
        client = session.client(service, **options)
        try:
            return getattr(client, method)(**kwargs)
        finally:
            client.close()

    if task in ('infrastructure', 'removal', 'metadata'):
        try:
            session = selected_aws_session(values, deploy=True, region=values['MAVEN_AWS_REGION'])
        except Exception as error:
            checks.append({'check': 'Selected maintenance login', 'status': 'Could not verify',
                           'reason': getattr(error, 'code', type(error).__name__)})
        else:
            if task in ('metadata', 'removal'):
                probe('Read the selected CloudFormation stack', lambda: call(session, 'cloudformation',
                      'describe_stacks', StackName=values['MAVEN_STACK_NAME']))
            else:
                probe('Read CloudFormation account limits', lambda: call(session, 'cloudformation',
                      'describe_account_limits'))
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
                principal = values['MAVEN_DEPLOY_EXPECTED_PRINCIPAL']
                if principal.startswith('arn:aws:iam:') and ':user/' in principal:
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
    else:
        from tools.memory.client import selected_session
        config = migration_config(values)
        role = 'admin' if task == 'sql' else 'approval' if task == 'authority' else 'client'
        try:
            session = selected_session(config, admin=role == 'admin', approval=role == 'approval', environment=values)
            checks.append({'check': 'Assume exact ' + role + ' role from daily-use login', 'status': 'Passed'})
        except Exception as error:
            checks.append({'check': 'Assume exact ' + role + ' role from daily-use login',
                           'status': 'Could not verify', 'reason': getattr(error, 'code', type(error).__name__)})
        else:
            if task == 'sql':
                probe('Read database through the existing SQL administration role', lambda: call(
                    session, 'rds-data', 'execute_statement', resourceArn=config['cluster_arn'],
                    secretArn=config['admin_secret_arn'], database=config['database'], sql='SELECT 1'))
            else:
                from tools.memory.environment import configured_client
                def read_context():
                    client = configured_client(values=values)
                    try:
                        client.load_context()
                    finally:
                        client.close()
                probe('Read current memory context through the daily-use role', read_context)
        limits.append('Role assumption and reads do not prove write/DDL or authority activation permissions. Existing role trusts are unchanged; SQL admin and approval use the selected daily-use principal.')
    status = ('Missing access' if any(item['status'] == 'Missing access' for item in checks) else
              'Could not verify' if any(item['status'] == 'Could not verify' for item in checks) else 'Passed')
    return {'status': status, 'task': task, 'checks': checks, 'limitations': limits,
            'remediation': 'Ask the account/IAM owner to inspect the named role, actions, resource policies, conditions and organization controls. Correct the selected profile or permissions, then retry this exact operation. Do not grant blanket administrator access just to silence a check.'}


def inspect_login_identity(values, *, deploy=False):
    """Inspect only the explicitly selected login profile; never initiate login."""
    import boto3
    from botocore.config import Config
    from tools.memory.environment import transport_ca_bundle
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    previous = os.environ.get('AWS_LOGIN_CACHE_DIRECTORY')
    os.environ['AWS_LOGIN_CACHE_DIRECTORY'] = values[prefix + 'LOGIN_CACHE']
    try:
        session = boto3.Session(profile_name=values[prefix + 'PROFILE'],
                                region_name=values['MAVEN_AWS_REGION'])
        credentials = session.get_credentials()
        if credentials is None:
            from botocore.exceptions import NoCredentialsError
            raise NoCredentialsError()
        if credentials.method != 'login':
            raise SetupPending('Use the explicitly selected AWS login-only profile, not static credentials.')
        client = session.client('sts', verify=transport_ca_bundle(), config=Config(
            connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
        try:
            result = client.get_caller_identity()
        finally:
            client.close()
        identity = {key: result[key] for key in ('Arn', 'Account', 'UserId') if key in result}
        check_login_identity(values, identity, deploy=deploy)
        return identity
    finally:
        if previous is None:
            os.environ.pop('AWS_LOGIN_CACHE_DIRECTORY', None)
        else:
            os.environ['AWS_LOGIN_CACHE_DIRECTORY'] = previous
