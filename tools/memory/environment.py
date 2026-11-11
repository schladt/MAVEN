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
from botocore.exceptions import BotoCoreError, ClientError

from tools.project import default_environment
from .client import CONFIG_KEYS, OPTIONAL_CONFIG_KEYS, MemoryClientError, validate_config

DEFAULT_ENV = default_environment()
EXTRA_KEYS = {
    'MAVEN_AWS_PROFILE', 'MAVEN_AWS_EXPECTED_PRINCIPAL', 'MAVEN_AWS_LOGIN_CACHE',
    'MAVEN_DEPLOY_PROFILE', 'MAVEN_DEPLOY_EXPECTED_PRINCIPAL', 'MAVEN_DEPLOY_LOGIN_CACHE',
    'MAVEN_AWS_REGION',
    'MAVEN_SECRETS_BUCKET', 'MAVEN_SECRETS_KEY_ID', 'MAVEN_GOOGLE_PARAMETER_PREFIX',
    'MAVEN_SECRETS_PARAMETER_PREFIX', 'MAVEN_SECRETS_FILE_KEY_PREFIX', 'MAVEN_SECRETS_OBJECT_PREFIX',
    'MAVEN_STACK_NAME', 'MAVEN_NOTIFICATION_EMAIL', 'MAVEN_ENGINE_VERSION',
    'MAVEN_INSTANCE_NAME', 'MAVEN_ACCOUNT_ID', 'MAVEN_CALLER_PRINCIPAL_ARN',
    'MAVEN_AVAILABILITY_ZONES', 'MAVEN_INSTANCE_ID', 'MAVEN_RECOVERY_EPOCH',
}
ENV_KEYS = EXTRA_KEYS | {'MAVEN_MEMORY_' + key.upper() for key in CONFIG_KEYS | OPTIONAL_CONFIG_KEYS}


def load_environment(path: Path = DEFAULT_ENV) -> dict[str, str]:
    """Read a bounded owner-only regular file. No expansion, exports or credentials."""
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
            if not separator or key not in ENV_KEYS or key in result:
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
        return result
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
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    profile = values[prefix + 'PROFILE']
    cache = str(Path(values[prefix + 'LOGIN_CACHE']).expanduser())
    argv = ['aws', 'login', '--profile', profile, '--region', values.get('MAVEN_AWS_REGION', 'us-east-1')]
    return {'purpose': 'Maintenance access' if deploy else 'Daily-use access', 'profile': profile,
            'argv': argv, 'environment': {'AWS_LOGIN_CACHE_DIRECTORY': cache},
            'command': 'AWS_LOGIN_CACHE_DIRECTORY=' + shlex.quote(cache) + ' ' + shlex.join(argv)}


def selected_aws_session(values: dict[str, str], *, deploy: bool = False, region: str = 'us-east-1'):
    """Use only a named existing login and verify its principal; never elevate or login."""
    prefix = 'MAVEN_DEPLOY_' if deploy else 'MAVEN_AWS_'
    try:
        profile = values[prefix + 'PROFILE']
        expected = values[prefix + 'EXPECTED_PRINCIPAL']
        cache = Path(values[prefix + 'LOGIN_CACHE']).expanduser()
        if (not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', profile)
                or not re.fullmatch(r'arn:aws:(?:iam|sts)::[0-9]{12}:[A-Za-z0-9+=,.@_/-]+', expected)):
            raise ValueError('selection')
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = cache.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MemoryClientError('login_cache_must_be_owner_private')
        for name in ('boto3', 'botocore', 'urllib3'):
            logging.getLogger(name).setLevel(logging.WARNING)
        os.environ['AWS_LOGIN_CACHE_DIRECTORY'] = str(cache)
        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client('sts', verify=transport_ca_bundle(),
                                config=Config(connect_timeout=3, read_timeout=10,
                                              retries={'total_max_attempts': 1}))
        try:
            if client.get_caller_identity().get('Arn') != expected:
                raise MemoryClientError('unexpected_login_identity')
        finally:
            client.close()
        return session
    except (KeyError, ValueError, OSError, BotoCoreError, ClientError):
        raise MemoryClientError('selected_login_unavailable_or_expired') from None


def configured_client(path: Path = DEFAULT_ENV, *, admin: bool = False,
                      approval: bool = False, values: dict[str, str] | None = None):
    from .client import MemoryClient, selected_session
    if values is None:
        values = load_environment(path)
    config = memory_config(values)
    return MemoryClient(config, selected_session(config, admin=admin, approval=approval, environment=values))
