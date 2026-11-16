"""Private, literal configuration shared by cloud clients; never shell-source .env."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shlex
import ssl
import stat

import boto3
from botocore.config import Config
from botocore.credentials import CredentialResolver, LoginProvider
from botocore.utils import JSONFileCache
from botocore.exceptions import BotoCoreError, ClientError

from tools.project import default_environment
from .client import (AUTHENTICATION_CODES, CONFIG_KEYS, OPTIONAL_CONFIG_KEYS,
                     MemoryClientError, aws_error, validate_config)

DEFAULT_ENV = default_environment()
EXTRA_KEYS = {
    'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_AWS_LOGIN_CACHE',
    'MAVEN_DEPLOY_PROFILE', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_LOGIN_CACHE',
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


def sign_in(values: dict[str, str], *, deploy: bool = False) -> dict:
    """Exact owner sign-in for a selected profile into MAVEN's own private login cache.

    A plain `aws login` writes the AWS default cache, which MAVEN never reads; only this
    command (with AWS_LOGIN_CACHE_DIRECTORY) renews the sign-in MAVEN uses.
    """
    profile, cache, region = _login_selection(values, deploy=deploy)
    cache = str(cache)
    argv = ['aws', 'login', '--profile', profile, '--region', region]
    return {'purpose': 'Maintenance access' if deploy else 'Daily-use access', 'profile': profile,
            'argv': argv, 'environment': {'AWS_LOGIN_CACHE_DIRECTORY': cache},
            'command': 'AWS_LOGIN_CACHE_DIRECTORY=' + shlex.quote(cache) + ' ' + shlex.join(argv)}


def authentication_handoff(error, values, *, deploy=False):
    """Return executable, selected-cache login advice only for typed auth failures."""
    normalized = aws_error(error)
    if normalized.code not in AUTHENTICATION_CODES:
        return None
    try:
        exact = sign_in(values, deploy=deploy)
    except MemoryClientError:
        return None
    return {'type': 'aws_login', 'command': exact['argv'], 'environment': exact['environment'],
            'reason': normalized.code, 'purpose': exact['purpose'], 'profile': exact['profile'],
            'reconnect': 'After owner sign-in, retry the same read-only inspection or resume the exact '
                         'retained operation. Do not replace an unresolved request or initialize another session.'}


def _login_selection(values, *, deploy=False, region=None):
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
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
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
    except (KeyError, TypeError, ValueError):
        raise MemoryClientError('invalid_login_cache') from None
    except OSError:
        raise MemoryClientError('login_cache_unavailable') from None


def selected_aws_session(values: dict[str, str], *, deploy: bool = False, region: str | None = None):
    """Use only a named existing login and verify its principal; never elevate or login."""
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    try:
        expected = values[prefix + 'EXPECTED_PRINCIPAL']
        if (not isinstance(expected, str)
                or not re.fullmatch(r'arn:aws:(?:iam|sts)::[0-9]{12}:[A-Za-z0-9+=,.@_/-]+', expected)):
            raise MemoryClientError('selected_login_configuration_unavailable')
        session = login_session(values, deploy=deploy, region=region)
        client = session.client('sts', verify=transport_ca_bundle(),
                                config=Config(connect_timeout=3, read_timeout=10,
                                              retries={'total_max_attempts': 1}))
        try:
            if client.get_caller_identity().get('Arn') != expected:
                raise MemoryClientError('unexpected_login_identity')
        finally:
            client.close()
        return session
    except (BotoCoreError, ClientError) as error:
        raise aws_error(error) from None
    except (KeyError, ValueError, TypeError):
        raise MemoryClientError('selected_login_configuration_unavailable') from None


def configured_client(path: Path = DEFAULT_ENV, *, admin: bool = False,
                      approval: bool = False, values: dict[str, str] | None = None):
    from .client import MemoryClient, selected_session
    if values is None:
        values = load_environment(path)
    config = memory_config(values)
    return MemoryClient(config, selected_session(config, admin=admin, approval=approval, environment=values))
