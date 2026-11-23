"""Explicit project selection; shared instance settings never select a project."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from uuid import UUID
from urllib.parse import urlsplit
from tools.recovery import DEFAULT_NEXT_ACTION


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
    if not isinstance(project, dict) or type(project.get('schema')) is not int or project['schema'] not in (1, 2):
        raise ProjectError('unsupported project schema')
    allowed = {'schema', 'project_id', 'root', 'remote', 'accepted_url', 'branch', 'checks', 'git_enabled'}
    if project['schema'] == 2:
        allowed |= {'instance_id', 'recovery_epoch'}
        for key in ('instance_id', 'recovery_epoch'):
            _uuid(project.get(key))
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


def require_binding(project, config):
    """A project UUID alone is not a selection of an instance or recovery epoch."""
    if project.get('schema') != 2 or any(project[key] != config.get(key) for key in ('instance_id', 'recovery_epoch')):
        raise ProjectError('Review a folder switch to bind this project to the selected instance and recovery epoch.')


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


def project_label(root, binding):
    """Display metadata only: a name never participates in project selection."""
    from tools.memory.boundary import BoundaryError, read_private
    path = '.maven/project-name.json'
    if (root / path).is_file():
        try:
            cached = read_private(root, path)
        except BoundaryError:
            return binding['project_id']
        if all(cached.get(k) == binding[k] for k in ('project_id', 'instance_id', 'recovery_epoch')):
            name = cached.get('name')
            if isinstance(name, str) and name.strip() and name.isprintable():
                return name
    return binding['project_id']


def save_project_name(root, binding, name):
    from tools.memory.boundary import save_private
    cached = {k: binding[k] for k in ('project_id', 'instance_id', 'recovery_epoch')}
    label = ''.join(c if c.isprintable() else ' ' for c in name).strip() if isinstance(name, str) else ''
    if label and project_label(root, binding) != label:
        save_private(root, '.maven/project-name.json', {**cached, 'name': label})


def project_values(values: dict, project: dict) -> dict:
    result = dict(values)
    result.pop('MAVEN_PROJECT_ID', None)
    result.pop('MAVEN_GIT_ACCEPTED_URL', None)
    result['MAVEN_PROJECT_ID'] = project['project_id']
    if project.get('git_enabled', project.get('accepted_url') is not None):
        result['MAVEN_GIT_ACCEPTED_URL'] = project['accepted_url']
    return result


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ProjectError('Git destination validation failed: ' + ' '.join(args))
    return result.stdout.strip()


def _ensure_ignore(root: Path) -> dict:
    """Append only the local runtime exclusion, retaining all existing content."""
    target = root / '.gitignore'
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    with os.fdopen(fd, 'r+b') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ProjectError('.gitignore must be a regular file')
        content = stream.read()
        appended = b'/.maven/' not in content.splitlines()
        if b'/.maven/' not in content.splitlines():
            stream.write((b'\n' if content and not content.endswith(b'\n') else b'') + b'/.maven/\n')
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        return {'path': str(target), 'appended': appended,
                'sha256': hashlib.sha256(stream.read()).hexdigest()}


def _record_integration(root: Path, payload: bytes, ignore: dict) -> None:
    """Separate ownership history; project.json's schema remains unchanged."""
    from tools.uninstall.common import read_private, save_private
    target = root / '.maven/integration.json'
    receipt = read_private(target) if target.exists() else {
        'schema': 1, 'root': str(root), 'integrations': []}
    if (receipt.get('schema') != 1 or receipt.get('root') != str(root)
            or not isinstance(receipt.get('integrations'), list)):
        raise ProjectError('existing integration ownership receipt is invalid; preserved')
    entry = {'project_sha256': hashlib.sha256(payload).hexdigest(), 'ignore': ignore}
    if entry not in receipt['integrations']:
        receipt['integrations'].append(entry)
    save_private(target, receipt)


def validate_git(root: Path, project: dict) -> None:
    if not project['git_enabled']:
        return
    if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ProjectError('--root must select the Git checkout root; rerun with an explicitly chosen root')
    if _git(root, 'remote', 'get-url', '--push', project['remote']) != project['accepted_url']:
        raise ProjectError('configured remote push URL differs from --accepted-url')
    _git(root, 'check-ref-format', '--branch', project['branch'])
    if _git(root, 'symbolic-ref', '--short', 'HEAD') != project['branch']:
        raise ProjectError('--branch must select the current branch')


def configure_git(root, *, remote, accepted_url, branch, env=None, expected_project=None) -> dict:
    """Opt into exact Git publication separately from memory project enablement."""
    from tools.coordination import project_binding_lock, ensure_available
    from tools.git_workflow import publication_lock
    root = Path(root).expanduser().resolve(strict=True)
    home = Path(env if env is not None else default_environment()).expanduser().resolve().parent
    with project_binding_lock(home, root):
        ensure_available(home, root)
        with publication_lock(root):
            return configure_git_locked(root, remote=remote, accepted_url=accepted_url, branch=branch,
                                        expected_project=expected_project)


def configure_git_locked(root, *, remote, accepted_url, branch, expected_project=None) -> dict:
    """Caller holds the binding gate and Git publication lock through verification."""
    from tools.git_workflow import require_no_pending
    from tools.memory.boundary import save_private
    existing = load_project(root)
    if existing['schema'] != 2:
        raise ProjectError('Review the project binding before configuring Git publication.')
    if expected_project is not None and existing != expected_project:
        raise ProjectError('Project selection changed after Git destination review.')
    require_no_pending(root)
    project = _validate(dict(existing, git_enabled=True, remote=remote,
                             accepted_url=accepted_url, branch=branch), root)
    validate_git(root, project)
    if project == existing:
        return {'status': 'unchanged', 'project': project}
    payload = (json.dumps(project, sort_keys=True, indent=2) + '\n').encode()
    _record_integration(root, payload, _ensure_ignore(root))
    save_private(root, '.maven/project.json', project)
    return {'status': 'configured', 'project': project}


def _initialize(args) -> dict:
    # Serialize binding with uninstall's persistent local removal marker.
    from tools.coordination import ensure_available, project_binding_lock
    home = Path(args.env).expanduser().absolute().parent
    with project_binding_lock(home, args.root, shared=False):
        ensure_available(home, args.root)
        return _initialize_bound(args)


def _initialize_bound(args, *, capability_verified=False) -> dict:
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ProjectError('project root must be an existing directory, never filesystem /')
    checks = []
    for command in args.check:
        try:
            checks.append(json.loads(command))
        except json.JSONDecodeError:
            raise ProjectError('--check requires a JSON argument list') from None
    from tools.memory.environment import load_environment, memory_config
    values = getattr(args, 'values', None)
    if values is None:
        values = load_environment(args.env)
    config = memory_config(values)
    project = _validate({
        'schema': 2, 'instance_id': config['instance_id'], 'recovery_epoch': config['recovery_epoch'],
        'project_id': args.project_id, 'root': str(root),
        'remote': args.remote, 'accepted_url': args.accepted_url, 'branch': args.branch,
        'git_enabled': any((args.remote, args.accepted_url, args.branch)), 'checks': checks,
    }, root)
    validate_git(root, project)
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
    if (directory / 'reconcile').exists() or (directory / 'reconcile').is_symlink():
        raise ProjectError('Unbound local records are already present. Preserve them; select a clean folder instead of reusing an unidentified store.')
    if args.create:
        if not all((args.name, args.source_origin, args.source_excerpt, args.request_key)):
            raise ProjectError('--create requires --name, --source-origin, --source-excerpt and --request-key')
        _uuid(args.request_key)
    elif any((args.name, args.source_origin, args.source_excerpt, args.request_key)):
        raise ProjectError('project creation arguments require --create')
    from tools.memory.environment import configured_client
    client = configured_client(args.env, values=values)
    try:
        if not capability_verified:
            from tools.memory.reconcile_cloud import status
            status(client)
        if args.create:
            client.invoke('/v1/memory/write', {
                'request_key': args.request_key, 'record_id': project['project_id'],
                'expected_revision': 0, 'kind': 'entity', 'classification': 'Sensitive',
                'data_scope': 'owner',
                'data': {'entity_type': 'project', 'name': args.name, 'aliases': []},
                'source': {'origin': args.source_origin, 'excerpt': args.source_excerpt},
                'change_reason': 'Initialize owner-selected project',
            })
        response = client.invoke('/v1/memory/read', {'record_id': project['project_id']})
        record = response.get('data', {})
        if (response.get('status') != 'ready' or record.get('record_id') != project['project_id']
                or record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                or record.get('project_id') is not None
                or record.get('data', {}).get('entity_type') != 'project'
                or 'archived' in record.get('tags', []) or record.get('data', {}).get('state') == 'archived'):
            raise ProjectError('selected cloud record is not the exact active universal owner project entity')
    finally:
        client.close()
    _private_directory(directory, create=True)
    ignore = _ensure_ignore(root)
    payload = (json.dumps(project, indent=2) + '\n').encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _record_integration(root, payload, ignore)
    save_project_name(root, project, record['data'].get('name'))
    return {'status': 'initialized', 'project': project}


def main(argv=None) -> int:
    try:
        return _run_cli(argv)
    except ProjectError as exc:
        print(f'{exc}. {DEFAULT_NEXT_ACTION}', file=sys.stderr)
    except Exception as exc:
        print(f'Project command stopped: {type(exc).__name__}. {DEFAULT_NEXT_ACTION}', file=sys.stderr)
    return 1


def _run_cli(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    initialize = commands.add_parser('init', help='Validate a cloud project and create private local selection')
    initialize.add_argument('--root', type=Path, required=True)
    initialize.add_argument('--project-id', required=True)
    initialize.add_argument('--env', type=Path, default=default_environment())
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
    setup = commands.add_parser('setup', help='Guide project selection with private, resumable request receipts')
    setup.add_argument('--root', type=Path, required=True)
    setup.add_argument('--env', type=Path, default=default_environment())
    setup.add_argument('--non-interactive', action='store_true', help='Use explicit choices already approved by the owner')
    selection = setup.add_mutually_exclusive_group()
    selection.add_argument('--create', action='store_true')
    selection.add_argument('--project-id')
    setup.add_argument('--name')
    setup.add_argument('--source-origin')
    setup.add_argument('--source-excerpt')
    setup.add_argument('--remote')
    setup.add_argument('--accepted-url')
    setup.add_argument('--branch')
    setup.add_argument('--check', action='append', default=[], metavar='JSON_ARGV')
    setup.add_argument('--json', action='store_true')
    git_setup = commands.add_parser('git', help='Explicitly configure optional Git publication for an enabled memory project')
    git_setup.add_argument('--root', type=Path, required=True)
    git_setup.add_argument('--env', type=Path, default=default_environment())
    git_setup.add_argument('--remote', required=True)
    git_setup.add_argument('--accepted-url', required=True)
    git_setup.add_argument('--branch', required=True)
    git_setup.add_argument('--json', action='store_true')
    ack = commands.add_parser('acknowledge-deleted-archive', help='Record intentional deletion of one absent local preservation; deletes nothing')
    ack.add_argument('--archive', type=Path, required=True)
    show = commands.add_parser('show', help='Show the explicit local project, without cloud access')
    show.add_argument('--root', type=Path, required=True)
    show.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    from tools.setup_common import SetupPending
    try:
        if args.command == 'setup':
            from tools.project_setup import setup_project
            if not args.non_interactive:
                from tools.tui import run_workflow
                from tools.project_setup import interactive_project
                return run_workflow('MAVEN project setup', [
                    'Project selection', 'Project binding',
                ], lambda ui: interactive_project(args))
            result = setup_project(args)
        elif args.command == 'acknowledge-deleted-archive':
            from tools.reconcile_redaction import acknowledge_deleted_archive
            result = acknowledge_deleted_archive(instance_home(), args.archive)
        elif args.command == 'git':
            result = configure_git(args.root, remote=args.remote, accepted_url=args.accepted_url,
                                   branch=args.branch, env=args.env)
        else:
            result = _initialize(args) if args.command == 'init' else load_project(args.root)
        print(json.dumps(result, indent=2))
        return 0
    except (EOFError, KeyboardInterrupt):
        print('Project setup paused; preserve .maven/runtime/project-setup and rerun the same --root.', file=sys.stderr)
        return 1
    except (ProjectError, SetupPending, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        if args.command == 'show':
            print('Check that --root names the enabled project folder. If its .maven directory was moved, '
                  'restore the original directory there. For a new folder, use maven-project setup --root '
                  'with the intended folder instead.', file=sys.stderr)
        else:
            print(DEFAULT_NEXT_ACTION, file=sys.stderr)
        return 1
    except Exception as exc:
        from tools.memory.client import MemoryClientError
        if not isinstance(exc, MemoryClientError):
            raise
        print(str(exc), file=sys.stderr)
        if exc.request_key:
            print('Preserved request key: ' + exc.request_key, file=sys.stderr)
        if args.command == 'setup':
            print('Preserve the private .maven/runtime/project-setup receipt and rerun setup with the same --root. Never replace IDs or choose another root to retry an unknown outcome.', file=sys.stderr)
        else:
            print(DEFAULT_NEXT_ACTION, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
