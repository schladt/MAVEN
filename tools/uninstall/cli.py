"""Plan and remove independently selected MAVEN project, machine, or cloud scopes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

from .common import (UninstallError, canonical, checked_path, digest, file_digest,
                     private_directory, read_private, save_private)

HANDLERS = {'local', 'cloud', 'lifecycle'}
SUCCESS = {'removed', 'retained', 'archived', 'reactivated', 'verified', 'detached',
           'completed', 'frozen', 'retired', 'purged', 'exported', 'unchanged'}
RUNNER = """import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.uninstall.cli import main
raise SystemExit(main())
"""


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _inside(path, root):
    return path == root or path.is_relative_to(root)


def _default_home():
    return str(Path(os.environ.get('MAVEN_HOME', '~/.maven')).expanduser().absolute())


def _external_python(homes, preferred=None):
    roots = [Path(home) for home in homes]
    candidates = [preferred, getattr(sys, '_base_executable', None),
                  shutil.which('python3', path='/usr/local/bin:/usr/bin:/bin')]
    for value in candidates:
        if not value or not Path(value).is_file():
            continue
        executable = Path(value).resolve()
        if any(_inside(executable, root) for root in roots):
            continue
        check = subprocess.run([str(executable), '-I', '-c',
                                'import sys; raise SystemExit(sys.version_info < (3, 11))'],
                               capture_output=True, timeout=10)
        if check.returncode == 0:
            return str(executable)
    raise UninstallError('recovery requires an external Python 3.11+ interpreter outside selected installation homes')


def _source_roots():
    import tools
    import maven_resources
    return {'tools': Path(tools.__file__).resolve().parent,
            'maven_resources': Path(maven_resources.__file__).resolve().parent}


def _snapshot(destination):
    """Keep executable recovery code outside all selected removal roots, not a venv."""
    private_directory(destination)
    manifest = {}
    for package, source in _source_roots().items():
        for path in sorted(source.rglob('*')):
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink():
                raise UninstallError('recovery source contains a symlink')
            if not path.is_file() or path.suffix not in {'.py', '.sql', '.json', '.md', '.example'}:
                continue
            relative = Path(package) / path.relative_to(source)
            target = destination / relative
            private_directory(target.parent)
            before = file_digest(path)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as output, path.open('rb') as source_file:
                shutil.copyfileobj(source_file, output)
                output.flush()
                os.fsync(output.fileno())
            if file_digest(target) != before or file_digest(path) != before:
                raise UninstallError('implementation changed during recovery snapshot')
            manifest[relative.as_posix()] = before
    target = destination / 'run.py'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(RUNNER.encode())
        stream.flush()
        os.fsync(stream.fileno())
    manifest['run.py'] = file_digest(target)
    # File fsync does not persist its directory entry. Commit the complete tree
    # bottom-up before the plan can authorize deletion of the installed runtime.
    directories = [path for path in destination.rglob('*') if path.is_dir()]
    for directory in [*sorted(directories, key=lambda path: len(path.parts), reverse=True),
                      destination, destination.parent]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return manifest


def _verify_runner(plan, directory):
    manifest = plan.get('runner')
    if not isinstance(manifest, dict) or not manifest:
        raise UninstallError('recovery implementation manifest is missing')
    root = directory / 'runner'
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise UninstallError('recovery implementation contains a symlink')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(manifest):
        raise UninstallError('recovery implementation file set changed')
    for relative, expected in manifest.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or file_digest(root / path) != expected:
            raise UninstallError('recovery implementation changed; do not apply this plan')


def _fragment():
    return {'actions': [], 'retained': [], 'warnings': [], 'targets': [], 'requires': []}


def _merge(plan, fragment):
    for key in ('actions', 'retained', 'warnings', 'targets', 'requires'):
        values = fragment.get(key, [])
        if not isinstance(values, list):
            raise UninstallError('invalid uninstall scope plan: ' + key)
        plan[key].extend(values)


def build_plan(args):
    from . import local
    if args.all_homes and not args.machine:
        raise UninstallError('--all-homes requires --machine')
    if len(args.home) > 1 and not args.machine:
        raise UninstallError('multiple --home selections require machine removal')
    candidates = args.home or ([] if args.all_homes else
                               [args.project_home or args.cloud_home or _default_home()])
    if args.all_homes:
        candidates = [*candidates, *local.discover_homes(candidates)]
    require_home = not (args.offline and not args.machine)
    homes = list(dict.fromkeys(str(checked_path(path, exists=require_home)) for path in candidates))
    if not homes:
        raise UninstallError('no installation homes discovered; select an explicit --home')
    if args.machine and any(Path(left).is_relative_to(Path(right))
                            for left in homes for right in homes if left != right):
        raise UninstallError('overlapping installation homes require separate reviewed removal plans')
    project_roots = list(dict.fromkeys(str(checked_path(root)) for root in args.project))
    reactivate = list(dict.fromkeys(str(checked_path(root)) for root in args.reactivate_project))
    if set(project_roots) & set(reactivate):
        raise UninstallError('cannot detach and reactivate the same project in one plan')
    if not (project_roots or reactivate or args.machine or args.cloud):
        raise UninstallError('select --project, --machine, --cloud, or --reactivate-project')
    if args.offline and (args.cloud or args.archive_project or reactivate):
        raise UninstallError('offline detach cannot perform cloud removal, archival, or reactivation')
    if args.archive_project and not project_roots:
        raise UninstallError('--archive-project requires --project')
    if args.adopt_legacy and not args.machine:
        raise UninstallError('--adopt-legacy requires --machine and review of its exact runtime inventory')
    if reactivate and (args.cloud or args.machine):
        raise UninstallError('reactivation cannot be combined with removal of its runtime or cloud')
    if (args.optional_resource or args.retained_resource or args.abandon_sessions
            or args.cloud_home or args.cloud_mode != 'retire') and not args.cloud:
        raise UninstallError('cloud options require --cloud')
    if args.export and not (project_roots or args.machine):
        raise UninstallError('--export requires project or machine removal')
    if args.project_home and not (project_roots or reactivate):
        raise UninstallError('--project-home requires a project selection')
    project_home = str(checked_path(args.project_home, exists=not args.offline)) if args.project_home else homes[0]
    cloud_home = str(checked_path(args.cloud_home)) if args.cloud_home else homes[0]
    identifier = str(uuid.uuid4())
    directory = checked_path(args.output or Path.home() / '.maven-uninstall' / identifier, exists=False)
    protected = [Path(root) for root in project_roots + reactivate]
    protected.extend(Path(home) for home in homes if args.machine)
    if any(_inside(directory, root) or _inside(root, directory) for root in protected):
        raise UninstallError('plan/recovery directory must be separate from every selected project and installation')
    if args.export:
        export = checked_path(args.export, exists=False)
        if _inside(directory, export) or _inside(export, directory):
            raise UninstallError('export destination and operation directory must be separate')
        if any(_inside(export, root) or _inside(root, export) for root in protected):
            raise UninstallError('export destination must be separate from selected source roots')
    if directory.exists() and any(directory.iterdir()):
        raise UninstallError('operation directory is not empty; use its original plan or a new directory')
    private_directory(directory)
    runner = _snapshot(directory / 'runner')
    plan = _fragment()
    plan.update(schema=1, operation_id=identifier, created_at=_utc(), operation_dir=str(directory),
                runner=runner, machine_homes=homes if args.machine else [],
                recovery_python=_external_python(homes if args.machine else []),
                scopes=[scope for scope, enabled in [('project', bool(project_roots or reactivate)),
                        ('machine', args.machine), ('cloud', args.cloud)] if enabled],
                offline=args.offline, confirmations={})
    for root in project_roots:
        export_target = str(Path(args.export).expanduser().absolute() / ('project-' + digest(root)[:16])) if args.export else None
        _merge(plan, local.plan_project(root, project_home, offline=args.offline,
                                       archive=args.archive_project, export=export_target))
    for root in reactivate:
        from . import lifecycle
        _merge(plan, lifecycle.plan_project(root, project_home, reactivate=True))
    if args.cloud:
        from . import cloud
        fragment = cloud.plan_cloud(cloud_home, mode=args.cloud_mode, abandon=args.abandon_sessions,
                                    optional=tuple(args.optional_resource),
                                    retained_resources=tuple(args.retained_resource))
        _merge(plan, fragment)
        instances = {target.get('instance_id') for target in fragment['targets']
                     if isinstance(target, dict) and target.get('scope') == 'cloud'} - {None}
        if len(instances) != 1:
            raise UninstallError('cloud plan must identify exactly one instance')
        instance = next(iter(instances))
        if args.cloud_mode == 'purge':
            plan['confirmations']['purge'] = instance
        if args.abandon_sessions:
            plan['confirmations']['abandon'] = instance
    if args.machine:
        for home in homes:
            export_target = str(Path(args.export).expanduser().absolute() / ('machine-' + digest(home)[:16])) if args.export else None
            _merge(plan, local.plan_machine(home, selected_homes=tuple(homes), offline=args.offline,
                                           export=export_target, adopt_legacy=args.adopt_legacy))
    plan['requires'] = sorted(set(plan['requires']))
    seen = set()
    actions = []
    project_transitions = {}
    for action in sorted(plan['actions'], key=lambda item: item['stage']):
        if (action.get('handler') not in HANDLERS or not isinstance(action.get('details'), dict)
                or not isinstance(action.get('target'), str) or not isinstance(action.get('kind'), str)):
            raise UninstallError('invalid uninstall action')
        if action['handler'] == 'lifecycle' and action['kind'] in ('project-archive', 'project-reactivate'):
            details = action['details']
            key = (details['instance_id'], action['target'])
            identity = {name: value for name, value in details.items()
                        if name not in ('root', 'binding_sha256')}
            transition = {'kind': action['kind'], 'identity': identity}
            if key in project_transitions:
                if project_transitions[key] != transition:
                    raise UninstallError('conflicting or changed cloud project transition across selected checkouts')
                plan['warnings'].append('One cloud lifecycle transition covers multiple selected checkouts of project ' + action['target'])
                continue
            project_transitions[key] = transition
        action['operation_dir'] = str(directory)
        action['id'] = digest(action)
        if action['id'] not in seen:
            actions.append(action)
            seen.add(action['id'])
    plan['actions'] = actions
    plan['warnings'].append('Only inventoried targets are covered. Retained data/resources may continue to incur charges. No live AWS restore or deletion has been verified by local tests.')
    plan['plan_digest'] = digest(plan)
    save_private(directory / 'plan.json', plan)
    save_private(directory / 'journal.json', {'schema': 1, 'operation_id': identifier,
                 'plan_digest': plan['plan_digest'], 'state': 'planned', 'actions': {}, 'created_at': _utc()})
    return {'status': 'planned', 'plan_path': str(directory / 'plan.json'), 'plan': plan,
            'recovery_command': [plan['recovery_python'], str(directory / 'runner' / 'run.py'),
                                 'status', '--plan', str(directory / 'plan.json')]}


def _load_plan(path):
    path = checked_path(path)
    plan = read_private(path)
    if not isinstance(plan, dict) or plan.get('schema') != 1:
        raise UninstallError('unsupported uninstall plan')
    expected = plan.get('plan_digest')
    if expected != digest({key: value for key, value in plan.items() if key != 'plan_digest'}):
        raise UninstallError('uninstall plan digest mismatch')
    if plan.get('operation_dir') != str(path.parent) or path.name != 'plan.json':
        raise UninstallError('plan location changed; retain the original operation directory')
    try:
        if str(uuid.UUID(plan['operation_id'])) != plan['operation_id']:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise UninstallError('invalid uninstall operation identity') from None
    return path, plan


@contextmanager
def _operation_lock(directory):
    path = checked_path(directory / 'operation.lock', exists=False)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise UninstallError('unsafe uninstall lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UninstallError('another invocation is executing this uninstall') from None
        yield
    finally:
        os.close(descriptor)


def _journal(path, plan):
    journal = read_private(path.parent / 'journal.json')
    if (not isinstance(journal, dict) or journal.get('schema') != 1
            or journal.get('plan_digest') != plan['plan_digest']
            or journal.get('operation_id') != plan['operation_id']
            or not isinstance(journal.get('actions'), dict)):
        raise UninstallError('journal does not match the approved uninstall plan')
    return journal


def status(path):
    path, plan = _load_plan(path)
    journal = _journal(path, plan)
    actions = {}
    by_scope = {scope: [] for scope in plan['scopes']}
    for action in plan['actions']:
        if action['handler'] == 'cloud' or action['kind'] == 'cloud-freeze':
            scope = 'cloud'
        elif action['handler'] == 'lifecycle' or action['details'].get('root'):
            scope = 'project'
        else:
            scope = 'machine'
        entry = journal['actions'].get(action['id'], {'state': 'pending'})
        actions[action['id']] = dict(entry, scope=scope, kind=action['kind'], target=action['target'])
        by_scope.setdefault(scope, []).append(entry['state'])
    scope_status = {}
    for scope, states in by_scope.items():
        if 'unresolved' in states:
            scope_status[scope] = 'blocked'
        elif states and all(state == 'completed' for state in states):
            scope_status[scope] = 'completed'
        elif any(state != 'pending' for state in states):
            scope_status[scope] = 'in_progress'
        else:
            scope_status[scope] = 'pending'
    return {'status': journal['state'], 'operation_id': plan['operation_id'],
            'plan_digest': plan['plan_digest'], 'scopes': plan['scopes'],
            'scope_status': scope_status, 'targets': plan['targets'], 'actions': actions,
            'retained': plan['retained'], 'warnings': plan['warnings'],
            'last_error': journal.get('last_error'), 'live_state_checked': False}


def _approve(plan, args):
    if args.confirm != plan['plan_digest']:
        raise UninstallError('--confirm must equal the complete reviewed plan digest')
    required = set(plan['requires'])
    if required - {'writers-quiescent', 'harnesses-stopped', 'purge', 'abandon-sessions'}:
        raise UninstallError('unsupported approval requirement; preserve this plan')
    if 'writers-quiescent' in required and not args.writers_quiescent:
        raise UninstallError('stop affected writers and explicitly attest --writers-quiescent')
    if 'harnesses-stopped' in required and not args.harnesses_stopped:
        raise UninstallError('close affected assistant harnesses and attest --harnesses-stopped')
    confirmations = plan['confirmations']
    if args.confirm_purge != confirmations.get('purge'):
        raise UninstallError('--confirm-purge must name the exact reviewed instance, only for purge')
    if args.confirm_abandon != confirmations.get('abandon'):
        raise UninstallError('--confirm-abandon must name the exact reviewed instance, only for abandonment')


def _resume_arguments(args):
    result = ['resume', '--plan', str(Path(args.plan).absolute()), '--confirm', args.confirm]
    for flag in ('writers_quiescent', 'harnesses_stopped'):
        if getattr(args, flag):
            result.append('--' + flag.replace('_', '-'))
    for flag in ('confirm_purge', 'confirm_abandon'):
        if getattr(args, flag):
            result.extend(['--' + flag.replace('_', '-'), getattr(args, flag)])
    return result


def _reexec_runner(path, plan, args, *, system=False):
    runner = path.parent / 'runner' / 'run.py'
    executable = sys.executable
    if system:
        executable = _external_python(plan['machine_homes'], plan['recovery_python'])
    environment = dict(os.environ)
    if system:
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
            environment.pop(key, None)
    os.execve(executable, [executable, '-B', str(runner), *_resume_arguments(args)], environment)


def apply(args, *, reexec=True):
    path, plan = _load_plan(args.plan)
    _approve(plan, args)
    _verify_runner(plan, path.parent)
    # Execute exactly the reviewed implementation even if the installation was updated.
    if reexec and Path(__file__).resolve() != path.parent / 'runner/tools/uninstall/cli.py':
        _reexec_runner(path, plan, args)
    journal_path = path.parent / 'journal.json'
    with _operation_lock(path.parent):
        journal = _journal(path, plan)
        def save():
            journal['updated_at'] = _utc()
            save_private(journal_path, journal)
        if journal['state'] == 'completed':
            return status(path)
        journal['state'] = 'applying'
        journal.pop('last_error', None)
        save()
        for action in plan['actions']:
            entry = journal['actions'].setdefault(action['id'], {'state': 'pending', 'progress': {}})
            if entry['state'] == 'completed':
                continue
            if action['handler'] not in HANDLERS:
                raise UninstallError('unrecognized uninstall action handler')
            # Cloud and project work run while dependencies exist. Re-enter with system
            # Python before deleting any selected environment containing this interpreter.
            if reexec and action['stage'] >= 90 and any(
                    _inside(Path(sys.prefix).absolute(), Path(home)) for home in plan['machine_homes']):
                save()
                _reexec_runner(path, plan, args, system=True)
            entry['state'] = 'started'
            save()
            try:
                module = importlib.import_module('tools.uninstall.' + action['handler'])
                result = module.execute(action, entry['progress'], save)
                if not isinstance(result, dict) or result.get('status') not in SUCCESS:
                    if isinstance(result, dict):
                        entry['result'] = result
                    raise UninstallError('action has not reached a verified terminal outcome; inspect status and resume')
                entry['result'] = result
                entry['state'] = 'completed'
                save()
            except Exception as error:
                entry['state'] = 'unresolved'
                journal['state'] = 'blocked'
                # Do not leak raw SDK, SQL, subprocess, or credential error diagnostics.
                message = str(error) if isinstance(error, UninstallError) else type(error).__name__
                journal['last_error'] = {'action_id': action['id'], 'kind': action['kind'], 'message': message}
                save()
                return status(path)
        journal['state'] = 'completed'
        save()
    return status(path)


def _plan_arguments(parser):
    parser.add_argument('--project', action='append', default=[], metavar='ROOT')
    parser.add_argument('--reactivate-project', action='append', default=[], metavar='ROOT',
                        help='Plan reversible cloud project reactivation; does not detach it')
    parser.add_argument('--project-home', metavar='HOME', help='Installation used for selected project cloud operations')
    parser.add_argument('--home', action='append', default=[], metavar='HOME')
    parser.add_argument('--machine', action='store_true')
    parser.add_argument('--all-homes', action='store_true', help='Select all discovered current-user installation homes')
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--cloud-home', metavar='HOME')
    parser.add_argument('--cloud-mode', choices=('retire', 'purge'), default='retire')
    parser.add_argument('--archive-project', action='store_true')
    parser.add_argument('--offline', action='store_true', help='Local detachment only; preserve unresolved cloud evidence')
    parser.add_argument('--export', metavar='PRIVATE_DIRECTORY')
    parser.add_argument('--optional-resource', action='append', default=[], metavar='SELECTOR',
                        help='Explicit dedicated s3:BUCKET, ssm:/prefix/, or secret:ARN')
    parser.add_argument('--retained-resource', action='append', default=[], metavar='ARN')
    parser.add_argument('--abandon-sessions', action='store_true')
    parser.add_argument('--output', metavar='PRIVATE_DIRECTORY')
    parser.add_argument('--adopt-legacy', action='store_true',
                        help='Explicitly inventory a recognized legacy MAVEN runtime without installer ownership receipts')


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command')
    _plan_arguments(commands.add_parser('plan', help='Inspect exact targets; never mutate selected targets'))
    for name in ('apply', 'resume'):
        command = commands.add_parser(name, help='Execute/reconcile the exact reviewed plan')
        command.add_argument('--plan', required=True, type=Path)
        command.add_argument('--confirm', required=True, help='Full reviewed plan SHA-256')
        command.add_argument('--writers-quiescent', action='store_true')
        command.add_argument('--harnesses-stopped', action='store_true')
        command.add_argument('--confirm-purge', metavar='INSTANCE_UUID')
        command.add_argument('--confirm-abandon', metavar='INSTANCE_UUID')
    inspect = commands.add_parser('status', help='Read local receipts without cloud calls')
    inspect.add_argument('--plan', required=True, type=Path)
    return root


def _yes(question):
    while True:
        value = input(question + ' [y/N]: ').strip().lower()
        if value in ('', 'n', 'no'):
            return False
        if value in ('y', 'yes'):
            return True
        print('Enter yes or no.')


def _guided(root):
    if not sys.stdin.isatty():
        raise UninstallError('guided uninstall requires a terminal; use plan/apply for explicit noninteractive choices')
    print('Select independent scopes. Nothing is removed until you review and approve a plan.')
    scopes = input('Scopes (project machine cloud, separated by spaces): ').split()
    if not scopes or set(scopes) - {'project', 'machine', 'cloud'}:
        raise UninstallError('select project, machine, cloud, or a combination')
    argv = ['plan']
    home = input('MAVEN installation home [' + _default_home() + ']: ').strip() or _default_home()
    argv.extend(['--home', home])
    if 'project' in scopes:
        while True:
            selected = input('Exact project root (blank ends selection): ').strip()
            if not selected:
                break
            argv.extend(['--project', selected])
        if '--project' not in argv:
            raise UninstallError('project scope requires an explicit project root')
        if _yes('Archive these cloud projects too (affects all their checkouts)?'):
            argv.append('--archive-project')
    if 'machine' in scopes:
        argv.append('--machine')
        if _yes('Select all discovered current-user installations instead of only this home?'):
            argv.append('--all-homes')
        else:
            while True:
                selected = input('Additional installation home (blank ends selection): ').strip()
                if not selected:
                    break
                argv.extend(['--home', selected])
        if _yes('Include recognized legacy MAVEN runtimes without ownership receipts in the exact reviewed inventory?'):
            argv.append('--adopt-legacy')
    if 'cloud' in scopes:
        argv.append('--cloud')
        mode = input('Cloud mode [retire] (retire/purge): ').strip() or 'retire'
        argv.extend(['--cloud-mode', mode])
        if _yes('Plan administrative abandonment of unreachable cloud sessions?'):
            argv.append('--abandon-sessions')
        for flag, prompt in [('--optional-resource', 'Optional dedicated resource selector'),
                             ('--retained-resource', 'Previously retained resource ARN')]:
            while True:
                selected = input(prompt + ' (blank ends selection): ').strip()
                if not selected:
                    break
                argv.extend([flag, selected])
    elif '--archive-project' not in argv and _yes('Detach locally without cloud access?'):
        argv.append('--offline')
    if 'project' in scopes or 'machine' in scopes:
        export = input('Private artifact export directory (blank preserves in place): ').strip()
        if export:
            argv.extend(['--export', export])
    output = input('Private plan/recovery directory (blank uses ~/.maven-uninstall/<UUID>): ').strip()
    if output:
        argv.extend(['--output', output])
    result = build_plan(root.parse_args(argv))
    print(json.dumps(result, indent=2))
    print('Read the full plan above, including retained resources and warnings.')
    confirmation = input('Type the complete plan digest to apply, or Enter to leave it planned: ').strip()
    if not confirmation:
        return result
    argv = ['apply', '--plan', result['plan_path'], '--confirm', confirmation]
    required = result['plan']['requires']
    if 'writers-quiescent' in required and _yes('Have all affected local writers actually stopped?'):
        argv.append('--writers-quiescent')
    if 'harnesses-stopped' in required and _yes('Are affected assistant harnesses closed?'):
        argv.append('--harnesses-stopped')
    for name in ('purge', 'abandon'):
        if name in result['plan']['confirmations']:
            value = input('Type the exact instance UUID to separately approve ' + name + ': ').strip()
            argv.extend(['--confirm-' + name, value])
    return apply(root.parse_args(argv))


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command is None:
            result = _guided(root)
        elif args.command == 'plan':
            result = build_plan(args)
        elif args.command == 'status':
            result = status(args.plan)
        else:
            result = apply(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 2 if result['status'] == 'blocked' else 0
    except (UninstallError, OSError, ValueError, ImportError) as error:
        message = str(error) if isinstance(error, UninstallError) else type(error).__name__
        print(json.dumps({'status': 'blocked', 'error': message,
                          'guidance': 'Preserve the operation directory. No success is implied; inspect status before retrying.'}),
              file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print(json.dumps({'status': 'interrupted', 'guidance': 'Preserve the plan and use status/resume.'}), file=sys.stderr)
        return 130
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error': type(error).__name__,
                          'guidance': 'Preserve the operation directory and reconcile before retrying.'}),
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
