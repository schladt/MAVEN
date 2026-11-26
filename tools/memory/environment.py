"""Private, literal configuration shared by cloud clients; never shell-source .env."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys

import boto3
from botocore.config import Config
from botocore.credentials import CredentialResolver, LoginProvider, RefreshableCredentials
from botocore.session import Session as BotocoreSession
from botocore.utils import JSONFileCache
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from tools.project import default_environment
from .client import (AUTHENTICATION_CODES, CONFIG_KEYS, OPTIONAL_CONFIG_KEYS,
                     MemoryClientError, aws_error, validate_config)

DEFAULT_ENV = default_environment()
EXTRA_KEYS = {
    'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_AWS_LOGIN_CACHE',
    'MAVEN_DEPLOY_PROFILE', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_LOGIN_CACHE',
    'MAVEN_DEPLOY_MODE', 'MAVEN_DEPLOY_ROLE_ARN',
    'MAVEN_AWS_REGION',
    'MAVEN_SECRETS_BUCKET', 'MAVEN_SECRETS_KEY_ID',
    'MAVEN_SECRETS_PARAMETER_PREFIX', 'MAVEN_SECRETS_FILE_KEY_PREFIX', 'MAVEN_SECRETS_OBJECT_PREFIX',
    'MAVEN_STACK_NAME', 'MAVEN_NOTIFICATION_EMAIL', 'MAVEN_ENGINE_VERSION',
    'MAVEN_INSTANCE_NAME', 'MAVEN_ACCOUNT_ID', 'MAVEN_CALLER_PRINCIPAL_ARN',
    'MAVEN_AVAILABILITY_ZONES', 'MAVEN_INSTANCE_ID', 'MAVEN_RECOVERY_EPOCH',
}
ENV_KEYS = EXTRA_KEYS | {'MAVEN_MEMORY_' + key.upper() for key in CONFIG_KEYS | OPTIONAL_CONFIG_KEYS}


def load_environment(path: Path = DEFAULT_ENV) -> dict[str, str]:
    """Read a bounded owner-only regular file. No expansion, exports or credentials."""
    return _load_environment(path, ENV_KEYS)


def _load_environment(path, allowed_keys, *, with_raw=False):
    """Internal bounded parser; migration alone supplies its retired locator key."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise MemoryClientError('configuration_must_be_owner_private')
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('size')
        result = {}
        for line in raw.decode('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if not separator or key not in allowed_keys or key in result:
                raise ValueError('key')
            value = value.strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'"):
                if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
                    raise ValueError('quote')
                value = value[1:-1]
            elif any(char.isspace() for char in value) or any(char in value for char in "'\"`$"):
                raise ValueError('literal')
            if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                raise ValueError('value')
            result[key] = value
        return (result, raw) if with_raw else result
    except OSError:
        raise MemoryClientError('configuration_unavailable') from None
    except (UnicodeError, ValueError, RecursionError):
        raise MemoryClientError('invalid_environment') from None


def transport_ca_bundle() -> str | bool:
    """Honor only an explicitly selected, valid per-process CA trust bundle."""
    value = os.environ.get('AWS_CA_BUNDLE')
    if value is None:
        return True
    try:
        path = Path(value).expanduser()
        if not value or not path.is_absolute() or not path.is_file():
            raise ValueError('path')
        ssl.create_default_context(cafile=str(path))
        return str(path)
    except (OSError, ValueError, ssl.SSLError):
        raise MemoryClientError('invalid_transport_ca_bundle') from None


def memory_config(values: dict[str, str]) -> dict:
    try:
        config = {key: values['MAVEN_MEMORY_' + key.upper()] for key in CONFIG_KEYS}
        config.update({key: values['MAVEN_MEMORY_' + key.upper()] for key in OPTIONAL_CONFIG_KEYS
                       if 'MAVEN_MEMORY_' + key.upper() in values})
        config['schema_version'] = int(config['schema_version'])
    except (KeyError, TypeError, ValueError):
        raise MemoryClientError('memory_configuration_incomplete') from None
    return validate_config(config)


def migration_config(values: dict[str, str]) -> dict:
    """Accept a legacy binding only for its explicitly confirmed schema upgrade."""
    from .validation import SCHEMA_VERSION
    if values.get('MAVEN_MEMORY_SCHEMA_VERSION') == '1':
        values = {**values, 'MAVEN_MEMORY_SCHEMA_VERSION': str(SCHEMA_VERSION)}
    # Persistence still requires reconnecting reviewed upgraded stack outputs;
    # successful SQL migration alone does not prove the service was deployed.
    return memory_config(values)


def deployment_mode(values):
    mode = values.get('MAVEN_DEPLOY_MODE', 'profile')
    if mode not in ('role', 'profile', 'deferred'):
        raise MemoryClientError('invalid_deployment_mode')
    return mode


def _role_binding(values):
    role = values.get('MAVEN_DEPLOY_ROLE_ARN')
    source = values.get('MAVEN_AWS_EXPECTED_PRINCIPAL')
    match = isinstance(role, str) and re.fullmatch(
        r'arn:aws:iam::([0-9]{12}):role/(?:[!-~]+/)?([\w+=,.@-]{1,64})', role, re.ASCII)
    if (not match or not isinstance(source, str)
            or not re.fullmatch(r'arn:aws:(?:iam|sts)::[0-9]{12}:[A-Za-z0-9+=,.@_/-]+', source)):
        raise MemoryClientError('selected_login_configuration_unavailable')
    account, name = match.groups()
    if (source.endswith(':root') or source.split(':')[4] != account
            or values.get('MAVEN_ACCOUNT_ID', account) != account):
        raise MemoryClientError('unexpected_login_identity')
    return role, account, name


# AWS CLI opens its sign-in URL through Python's webbrowser, which honors BROWSER (%s is the URL).
# A new private window carries no console session, so AWS asks for the password and a fresh MFA code.
_LINUX_PRIVATE_BROWSERS = {
    'google-chrome': ('google-chrome', '--incognito', '--new-window'),
    'chromium': ('chromium', '--incognito', '--new-window'),
    'chromium_chromium': ('chromium', '--incognito', '--new-window'),
    'chromium-browser': ('chromium-browser', '--incognito', '--new-window'),
    'brave-browser': ('brave-browser', '--incognito', '--new-window'),
    'microsoft-edge': ('microsoft-edge', '--inprivate', '--new-window'),
    'firefox': ('firefox', '--private-window'),
    'firefox_firefox': ('firefox', '--private-window'),
    'firefox-esr': ('firefox-esr', '--private-window'),
}
_MAC_PRIVATE_BROWSERS = {
    'com.google.chrome': ('Google Chrome', '--incognito', '--new-window'),
    'com.brave.browser': ('Brave Browser', '--incognito', '--new-window'),
    'com.microsoft.edgemac': ('Microsoft Edge', '--inprivate', '--new-window'),
    'org.chromium.chromium': ('Chromium', '--incognito', '--new-window'),
}
PRIVATE_BROWSERS = frozenset(
    [shlex.join(argv) + ' %s' for argv in _LINUX_PRIVATE_BROWSERS.values()]
    + [shlex.join(['/usr/bin/open', '-na', app, '--args', *flags]) + ' %s'
       for app, *flags in _MAC_PRIVATE_BROWSERS.values()])


def private_browser():
    """BROWSER launcher for a private window of the desktop's default browser; None for a plain URL."""
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
        return None
    try:
        if sys.platform == 'darwin':
            path = Path.home() / 'Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist'
            with path.open('rb') as stream:
                handlers = plistlib.load(stream).get('LSHandlers', [])
            bundle = next((item.get('LSHandlerRoleAll') for item in handlers
                           if isinstance(item, dict) and item.get('LSHandlerURLScheme') == 'https'), None)
            selected = _MAC_PRIVATE_BROWSERS.get(str(bundle).lower())
            if selected:
                return shlex.join(['/usr/bin/open', '-na', selected[0], '--args', *selected[1:]]) + ' %s'
        elif os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            desktop = subprocess.run(['xdg-settings', 'get', 'default-web-browser'], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=5, check=False).stdout.strip()
            selected = _LINUX_PRIVATE_BROWSERS.get(desktop.removesuffix('.desktop'))
            if selected and shutil.which(selected[0]):
                return shlex.join(selected) + ' %s'
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return None


def fresh_sign_in(selected, browser):
    """Force a new console sign-in: a private window via BROWSER, else a URL to open in one."""
    if browser is not None and browser not in PRIVATE_BROWSERS:
        raise ValueError('unsupported private browser launcher')
    argv, environment = list(selected['argv']), dict(selected['environment'])
    if browser:
        environment['BROWSER'] = browser
        note = ('AWS sign-in opens in a new private browser window, so AWS asks for your password and MFA. '
                'If no window opens, open the printed URL in a private window.')
    else:
        argv.append('--remote')
        note = ('Open the printed URL in a new private/incognito browser window (an existing console session '
                'carries no fresh MFA), sign in with your password and MFA, then paste the code back here.')
    command = ' '.join(key + '=' + shlex.quote(value) for key, value in environment.items()) + ' ' + shlex.join(argv)
    return {**selected, 'argv': argv, 'environment': environment, 'command': command,
            'fresh_mfa': True, 'note': note}


def sign_in(values: dict[str, str], *, deploy: bool = False) -> dict:
    """Exact owner sign-in for a selected profile into MAVEN's own private login cache.

    A plain `aws login` writes the AWS default cache, which MAVEN never reads; only this
    command (with AWS_LOGIN_CACHE_DIRECTORY) renews the sign-in MAVEN uses. Role elevation
    needs fresh MFA, so its sign-in never reuses an existing browser console session.
    """
    purpose = 'Maintenance access' if deploy else 'Daily-use access'
    mode = deployment_mode(values) if deploy else 'profile'
    if mode == 'deferred':
        raise MemoryClientError('deployment_access_deferred')
    if mode == 'role':
        role, _, _ = _role_binding(values)
        purpose = 'Fresh MFA for deployment role ' + role
        deploy = False
    profile, cache, region = _login_selection(values, deploy=deploy)
    cache = str(cache)
    argv = ['aws', 'login', '--profile', profile, '--region', region]
    selected = {'purpose': purpose, 'profile': profile,
                'argv': argv, 'environment': {'AWS_LOGIN_CACHE_DIRECTORY': cache},
                'command': 'AWS_LOGIN_CACHE_DIRECTORY=' + shlex.quote(cache) + ' ' + shlex.join(argv)}
    return fresh_sign_in(selected, private_browser()) if mode == 'role' else selected


def authentication_handoff(error, values, *, deploy=False):
    """Return executable, selected-cache login advice only for typed auth failures."""
    normalized = aws_error(error)
    try:
        # Only role elevation raises these codes, whatever stage observed them.
        role_handoff = (normalized.code in ('deployment_elevation_denied', 'deployment_session_expired')
                        and deployment_mode(values) == 'role')
        if not role_handoff and normalized.code not in AUTHENTICATION_CODES:
            return None
        exact = sign_in(values, deploy=deploy or role_handoff)
    except MemoryClientError:
        return None
    reconnect = ('After owner sign-in, retry the same read-only inspection or resume the exact '
                 'retained operation. Do not replace an unresolved request or initialize another session.')
    if normalized.code == 'deployment_elevation_denied':
        reconnect += (' AWS denied AssumeRole; a console session without fresh MFA is the usual cause. If a fresh '
                      'MFA sign-in does not resolve it, stop and check the role trust and permissions.')
    handoff = {'type': 'aws_login', 'command': exact['argv'], 'environment': exact['environment'],
               'reason': normalized.code, 'purpose': exact['purpose'], 'profile': exact['profile'],
               'reconnect': reconnect}
    if exact.get('fresh_mfa'):
        handoff.update(fresh_mfa=True, note=exact['note'])
    return handoff


def _login_selection(values, *, deploy=False, region=None):
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    if deploy and deployment_mode(values) != 'profile':
        raise MemoryClientError('selected_login_configuration_unavailable')
    try:
        profile = values[prefix + 'PROFILE']
        raw_cache = values[prefix + 'LOGIN_CACHE']
        selected_region = region or values.get('MAVEN_AWS_REGION', 'us-east-1')
        if (not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', profile)
                or not isinstance(raw_cache, str) or not raw_cache
                or any(ord(char) < 32 for char in raw_cache)
                or not isinstance(selected_region, str)
                or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', selected_region)):
            raise ValueError('selection')
        cache = Path(raw_cache).expanduser()
        if not cache.is_absolute():
            raise ValueError('cache')
        return profile, cache, selected_region
    except (KeyError, TypeError, ValueError):
        raise MemoryClientError('selected_login_configuration_unavailable') from None


def login_session(values, *, deploy=False, region=None):
    """Construct a login-only session with its own cache, including refresh writes.

    The resolver deliberately contains no ambient/static/role fallback. Neither
    construction nor refresh changes AWS_LOGIN_CACHE_DIRECTORY in the process.
    Initial setup may inspect a login before selecting its expected principal.
    """
    profile, cache, region = _login_selection(values, deploy=deploy, region=region)
    try:
        if cache.is_symlink():
            raise MemoryClientError('login_cache_must_be_owner_private')
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = cache.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MemoryClientError('login_cache_must_be_owner_private')
        for name in ('boto3', 'botocore', 'urllib3'):
            logging.getLogger(name).setLevel(logging.WARNING)
        session = boto3.Session(profile_name=profile, region_name=region)
        sdk = session._session
        provider = LoginProvider(load_config=lambda: sdk.full_config,
                                 client_creator=sdk.create_client, profile_name=profile,
                                 token_cache=JSONFileCache(str(cache)))
        sdk.register_component('credential_provider', CredentialResolver([provider]))
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'login':
            raise MemoryClientError('selected_profile_requires_login')
        return session
    except ProfileNotFound as error:
        if error.kwargs.get('profile') == profile:
            raise MemoryClientError('selected_login_profile_missing') from None
        raise aws_error(error) from None
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
    except (KeyError, TypeError, ValueError):
        raise MemoryClientError('invalid_login_cache') from None
    except OSError:
        raise MemoryClientError('login_cache_unavailable') from None


def _sts(session):
    return session.client('sts', verify=transport_ca_bundle(),
                          config=Config(connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))


def _identity(session):
    client = _sts(session)
    try:
        result = client.get_caller_identity()
        return {key: result[key] for key in ('Account', 'Arn', 'UserId') if key in result}
    finally:
        client.close()


def _profile_session(values, *, deploy=False, region=None, allow_unbound=False):
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    try:
        expected = values.get(prefix + 'EXPECTED_PRINCIPAL')
        if expected is not None or not allow_unbound:
            if (not isinstance(expected, str)
                    or not re.fullmatch(r'arn:aws:(?:iam|sts)::[0-9]{12}:[A-Za-z0-9+=,.@_/-]+', expected)):
                raise MemoryClientError('selected_login_configuration_unavailable')
        session = login_session(values, deploy=deploy, region=region)
        identity = _identity(session)
        if expected is not None and identity.get('Arn') != expected:
            raise MemoryClientError('unexpected_login_identity')
        return session, identity
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
    except (KeyError, ValueError, TypeError):
        raise MemoryClientError('selected_login_configuration_unavailable') from None


def _deployment_time():
    return datetime.now(timezone.utc)


def _expired_deployment():
    # Never renew an elevated session behind the owner's back or replay its operation.
    raise MemoryClientError('deployment_session_expired')


def deployment_session(values, *, region=None, allow_unbound=False):
    """Resolve exactly the reviewed deployment path; return its session and stable identity."""
    mode = deployment_mode(values)
    region = region or values.get('MAVEN_AWS_REGION', 'us-east-1')
    if mode == 'profile':
        return _profile_session(values, deploy=True, region=region, allow_unbound=allow_unbound)
    if mode == 'deferred':
        raise MemoryClientError('deployment_access_deferred')
    role, account, name = _role_binding(values)
    source, identity = _profile_session(values, region=region)
    if identity.get('Account') != account:
        raise MemoryClientError('unexpected_login_identity')
    try:
        sts = _sts(source)
        try:
            try:
                result = sts.assume_role(RoleArn=role, RoleSessionName='maven-deployment', DurationSeconds=900)
            except ClientError as error:
                if aws_error(error).code == 'not_authorized':
                    raise MemoryClientError('deployment_elevation_denied') from None
                raise
        finally:
            sts.close()
        temporary = result['Credentials']
        expiration = temporary['Expiration']
        now = _deployment_time()
        if not isinstance(expiration, datetime) or expiration.tzinfo is None:
            raise ValueError('expiration')
        if expiration <= now:
            raise MemoryClientError('deployment_session_expired')
        keys = [temporary[key] for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')]
        if not all(isinstance(key, str) and key for key in keys):
            raise ValueError('credentials')
        sdk = BotocoreSession()
        sdk._credentials = RefreshableCredentials(
            *keys, expiry_time=min(expiration, now + timedelta(seconds=900)), refresh_using=_expired_deployment,
            method='assume-role', time_fetcher=_deployment_time, advisory_timeout=0, mandatory_timeout=0)
        elevated = boto3.Session(botocore_session=sdk, region_name=region)
        actual = _identity(elevated)
        # STS omits the role's path, but IAM role names are unique within an account.
        assumed = rf'arn:aws:sts::{account}:assumed-role/{re.escape(name)}/[\w+=,.@-]{{2,64}}'
        if actual.get('Account') != account or not re.fullmatch(assumed, actual.get('Arn', ''), re.ASCII):
            raise MemoryClientError('unexpected_login_identity')
        actual['Arn'] = role
        if 'UserId' in actual:
            actual['UserId'] = actual['UserId'].partition(':')[0]
        return elevated, actual
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
    except (KeyError, TypeError, ValueError):
        raise MemoryClientError('invalid_role_credentials') from None


def selected_aws_session(values: dict[str, str], *, deploy: bool = False, region: str | None = None):
    """Return only the verified session for the selected purpose; never log in."""
    if deploy:
        return deployment_session(values, region=region)[0]
    return _profile_session(values, region=region)[0]


def configured_client(path: Path = DEFAULT_ENV, *, admin: bool = False,
                      approval: bool = False, values: dict[str, str] | None = None):
    from .client import MemoryClient, selected_session
    if values is None:
        values = load_environment(path)
    config = memory_config(values)
    return MemoryClient(config, selected_session(config, admin=admin, approval=approval, environment=values))
