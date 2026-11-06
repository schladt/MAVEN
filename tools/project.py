"""Explicit project selection; shared instance settings never select a project."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from uuid import UUID
from urllib.parse import urlsplit


class ProjectError(ValueError):
    """A project selection cannot be safely used."""


def instance_home() -> Path:
    value = os.environ.get('MAVEN_HOME')
    path = Path(value).expanduser() if value else Path.home() / '.maven'
    if not path.is_absolute():
        raise ProjectError('MAVEN_HOME must be an absolute path')
    return path


def default_environment() -> Path:
    return instance_home() / '.env'


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError('noncanonical')
    except (ValueError, TypeError, AttributeError):
        raise ProjectError('project and request IDs must be canonical UUIDs') from None
    return value


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ProjectError('project .maven directory must be owner-private and not a symlink')


def validate_accepted_url(value: str) -> str:
    """Validate a reportable Git destination without retaining embedded secrets."""
    invalid = ProjectError('invalid Git destination')
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 32 for char in value)
            or '?' in value or '#' in value or '\\' in value):
        raise invalid
    if value.startswith(('/', './', '../', '~/')):
        return value
    if '://' in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in ('http', 'https', 'ssh', 'file')
                    or parsed.password is not None or parsed.query or parsed.fragment):
                raise invalid
            if parsed.scheme == 'file':
                if parsed.netloc not in ('', 'localhost') or not parsed.path.startswith('/'):
                    raise invalid
            else:
                if (not parsed.hostname or any(char.isspace() for char in value)
                        or '%' in parsed.hostname):
                    raise invalid
                if parsed.scheme in ('http', 'https') and parsed.username is not None:
                    raise invalid
                if parsed.username is not None and not re.fullmatch(r'[A-Za-z0-9._~-]+', parsed.username):
                    raise invalid
                parsed.port  # Reject malformed/out-of-range ports before any reporting.
        except ValueError:
            raise invalid from None
        return value
    # Git's scp-like syntax permits SSH usernames, never a user:password prefix.
    if '@' in value and value.find(':') < value.find('@'):
        raise invalid
    if re.fullmatch(r'(?:[A-Za-z0-9._~-]+@)?(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._-]+):[^:\s].*', value):
        return value
    raise invalid


def _validate(project: dict, root: Path) -> dict:
    if not isinstance(project, dict) or type(project.get('schema')) is not int or project['schema'] != 1:
        raise ProjectError('unsupported project schema')
    allowed = {'schema', 'project_id', 'root', 'remote', 'accepted_url', 'branch', 'checks', 'git_enabled'}
    if set(project) - allowed:
        raise ProjectError('unknown project setting')
    _uuid(project.get('project_id'))
    if project.get('root') != str(root):
        raise ProjectError('project root does not match this checkout')
    destinations = [project.get(key) for key in ('remote', 'accepted_url', 'branch')]
    enabled = project.get('git_enabled', any(value is not None for value in destinations))
    if type(enabled) is not bool:
        raise ProjectError('git_enabled must be boolean')
    if enabled:
        if any(not isinstance(value, str) or not value or value.startswith('-')
               or any(ord(c) < 32 for c in value) for value in destinations):
            raise ProjectError('Git projects require remote, accepted_url and branch together')
        validate_accepted_url(project['accepted_url'])
    elif any(value is not None for value in destinations):
        raise ProjectError('memory-only projects cannot specify a Git destination')
    checks = project.get('checks', [])
    if not isinstance(checks, list) or any(
        not isinstance(command, list) or not command or any(
            not isinstance(arg, str) or not arg or '\0' in arg for arg in command
        ) for command in checks
    ):
        raise ProjectError('checks must be a list of nonempty command argument lists')
    return {**project, 'git_enabled': enabled, 'checks': checks,
            **dict(zip(('remote', 'accepted_url', 'branch'), destinations))}


def load_project(root: Path) -> dict:
    root = Path(root).expanduser().resolve(strict=True)
    directory = root / '.maven'
    try:
        _private_directory(directory)
        fd = os.open(directory / 'project.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ProjectError('project configuration must be an owner-private regular file')
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ProjectError('project configuration is too large')
        return _validate(json.loads(raw), root)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError('project configuration unavailable or invalid') from exc


def project_values(values: dict, project: dict) -> dict:
    result = dict(values)
    result.pop('MAVEN_PROJECT_ID', None)
    result.pop('MAVEN_GIT_ACCEPTED_URL', None)
    result['MAVEN_PROJECT_ID'] = project['project_id']
    if project.get('git_enabled', project.get('accepted_url') is not None):
        result['MAVEN_GIT_ACCEPTED_URL'] = project['accepted_url']
    return result


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise ProjectError('Git destination validation failed: ' + ' '.join(args))
    return result.stdout.strip()


def _ensure_ignore(root: Path) -> None:
    """Append only the local runtime exclusion, retaining all existing content."""
    target = root / '.gitignore'
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    with os.fdopen(fd, 'r+b') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ProjectError('.gitignore must be a regular file')
        content = stream.read()
        if b'/.maven/' not in content.splitlines():
            stream.write((b'\n' if content and not content.endswith(b'\n') else b'') + b'/.maven/\n')
            stream.flush()
            os.fsync(stream.fileno())


def _initialize(args) -> dict:
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProjectError('project root must be an existing directory')
    checks = []
    for command in args.check:
        try:
            checks.append(json.loads(command))
        except json.JSONDecodeError:
            raise ProjectError('--check requires a JSON argument list') from None
    project = _validate({
        'schema': 1, 'project_id': args.project_id, 'root': str(root),
        'remote': args.remote, 'accepted_url': args.accepted_url, 'branch': args.branch,
        'git_enabled': not args.no_git, 'checks': checks,
    }, root)
    directory = root / '.maven'
    if directory.exists() or directory.is_symlink():
        _private_directory(directory)
    target = directory / 'project.json'
    if target.exists() or target.is_symlink():
        existing = load_project(root)
        if existing != project:
            raise ProjectError('project already initialized; existing configuration was preserved')
        _ensure_ignore(root)
        return {'status': 'unchanged', 'project': existing}
    if project['git_enabled']:
        if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
            raise ProjectError('--root must select the Git checkout root')
        if _git(root, 'remote', 'get-url', '--push', project['remote']) != project['accepted_url']:
            raise ProjectError('configured remote push URL differs from --accepted-url')
        _git(root, 'check-ref-format', '--branch', project['branch'])
        if _git(root, 'symbolic-ref', '--short', 'HEAD') != project['branch']:
            raise ProjectError('--branch must select the current branch')
    if args.create:
        if not all((args.name, args.source_origin, args.source_excerpt, args.request_key)):
            raise ProjectError('--create requires --name, --source-origin, --source-excerpt and --request-key')
        _uuid(args.request_key)
    elif any((args.name, args.source_origin, args.source_excerpt, args.request_key)):
        raise ProjectError('project creation arguments require --create')
    from tools.memory.environment import configured_client
    client = configured_client(args.env)
    try:
        if args.create:
            client.invoke('/v1/memory/write', {
                'request_key': args.request_key, 'record_id': project['project_id'],
                'expected_revision': 0, 'kind': 'entity', 'classification': 'Sensitive',
                'data_scope': 'owner',
                'data': {'entity_type': 'project', 'name': args.name, 'aliases': []},
                'source': {'origin': args.source_origin, 'excerpt': args.source_excerpt},
                'change_reason': 'Initialize owner-selected project',
            })
        record = client.invoke('/v1/memory/read', {'record_id': project['project_id']})['data']
        if (record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                or record.get('data', {}).get('entity_type') != 'project'):
            raise ProjectError('selected cloud record is not an owner project entity')
    finally:
        client.close()
    _private_directory(directory, create=True)
    _ensure_ignore(root)
    payload = (json.dumps(project, indent=2) + '\n').encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {'status': 'initialized', 'project': project}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    initialize = commands.add_parser('init', help='Validate a cloud project and create private local selection')
    initialize.add_argument('--root', type=Path, required=True)
    initialize.add_argument('--project-id', required=True)
    initialize.add_argument('--env', type=Path, default=default_environment())
    initialize.add_argument('--no-git', action='store_true', help='Select memory-only lifecycle')
    initialize.add_argument('--remote')
    initialize.add_argument('--accepted-url')
    initialize.add_argument('--branch')
    initialize.add_argument('--check', action='append', default=[], metavar='JSON_ARGV')
    initialize.add_argument('--create', action='store_true', help='Explicitly create the cloud project entity')
    initialize.add_argument('--name')
    initialize.add_argument('--source-origin')
    initialize.add_argument('--source-excerpt')
    initialize.add_argument('--request-key', help='Stable UUID; reuse unchanged after an unknown outcome')
    initialize.add_argument('--json', action='store_true')
    show = commands.add_parser('show', help='Show the explicit local project, without cloud access')
    show.add_argument('--root', type=Path, required=True)
    show.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = _initialize(args) if args.command == 'init' else load_project(args.root)
        print(json.dumps(result, indent=2))
        return 0
    except (ProjectError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        from tools.memory.client import MemoryClientError
        if not isinstance(exc, MemoryClientError):
            raise
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
