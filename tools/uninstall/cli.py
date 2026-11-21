"""Plan and remove independently selected MAVEN project, machine, or cloud scopes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import select
import signal
import stat
import subprocess
import sys
import time
import uuid

from .common import (PauseRequested, UninstallError, canonical, checked_path, digest, file_digest,
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
    if (args.optional_resource or args.retained_resource
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
    plan.update(schema=1, tui_worker_protocol=1, operation_id=identifier, created_at=_utc(), operation_dir=str(directory),
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
        fragment = cloud.plan_cloud(cloud_home, mode=args.cloud_mode,
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
    if args.machine:
        for home in homes:
            export_target = str(Path(args.export).expanduser().absolute() / ('machine-' + digest(home)[:16])) if args.export else None
            _merge(plan, local.plan_machine(home, selected_homes=tuple(homes), offline=args.offline,
                                           export=export_target, adopt_legacy=args.adopt_legacy))
    if args.cloud:
        external_dependencies = {'AWS_CONFIG_FILE': os.environ.get('AWS_CONFIG_FILE', str(Path.home() / '.aws/config'))}
        external_dependencies.update({name: os.environ[name] for name in (
            'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE')
            if os.environ.get(name)})
        for name, value in external_dependencies.items():
            selected_path = Path(value).expanduser().absolute()
            external_dependencies[name] = str(selected_path)
            if any(_inside(location, Path(home)) for home in plan['machine_homes']
                   for location in (selected_path, selected_path.resolve())):
                raise UninstallError(name + ' is inside a removal target; configure its protected external '
                                     'location before planning cloud recovery. No configuration or credentials were copied.')
        from tools.tui.runtime import _copy_file, _matching_python, _stage_dependencies
        plan['recovery_python'] = _matching_python(plan['machine_homes'], plan['recovery_python'])
        _stage_dependencies(directory / 'cloud-runtime', plan_digest=identifier, ui=False)
        plan['cloud_recovery'] = {
            'manifest_sha256': file_digest(directory / 'cloud-runtime/manifest.json'),
            'configuration': 'private lifecycle environments and reviewed deployment identity; no login caches copied',
            'authentication': 'the selected deployment source may need a fresh private login after local cache deletion',
            'external_environment': external_dependencies,
        }
        for action in plan['actions']:
            if action['handler'] == 'cloud':
                action['details']['portable_recovery'] = True
                identity = action['details']['identity']
                cache = identity.get('MAVEN_AWS_LOGIN_CACHE' if identity.get('MAVEN_DEPLOY_MODE') == 'role'
                                     else 'MAVEN_DEPLOY_LOGIN_CACHE')
                if cache and any(_inside(Path(cache).expanduser().absolute(), Path(home))
                                 for home in plan['machine_homes']):
                    action['details']['recovery_login_cache'] = str(directory / 'authentication/deploy')
            elif action['handler'] == 'lifecycle':
                source = checked_path(Path(action['details']['home']) / '.env')
                local._private_environment(source)
                destination = directory / 'configuration' / (digest(str(source)) + '.env')
                if _copy_file(source, destination) != action['details']['environment_sha256']:
                    raise UninstallError('Lifecycle environment changed while staging recovery')
                action['details']['recovery_environment'] = str(destination)
        plan['warnings'].append('Cloud dependencies and private lifecycle settings are retained outside the installation. '
                                'Login caches are never copied. If the original deployment cache is removed, '
                                'fresh login uses the separately reviewed external recovery cache, never recreating the purged home.')
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
                                 'status', '--plan', str(directory / 'plan.json')],
            'resume_command': [plan['recovery_python'], '-B', str(directory / 'runner/run.py'),
                               'resume', '--plan', str(directory / 'plan.json'),
                               '--confirm', plan['plan_digest']],
            'resume_approval_requirements': plan['requires'],
            'resume_separate_confirmations': plan['confirmations']}


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
    return _status(plan, journal)


def _status(plan, journal):
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
        elif 'pending-provider' in states:
            scope_status[scope] = 'pending-provider'
        elif states and all(state == 'completed' for state in states):
            scope_status[scope] = 'completed'
        elif any(state != 'pending' for state in states):
            scope_status[scope] = 'in_progress'
        else:
            scope_status[scope] = 'pending'
    homes = {}
    for home in plan.get('machine_homes', []):
        purge = next((item for item in plan['actions']
                      if item['kind'] == 'local_home_purge' and item['target'] == home), None)
        homes[home] = ('removed' if purge and journal['actions'].get(purge['id'], {}).get('state') == 'completed'
                       else 'removal_pending' if purge else 'retained')
    return {'status': journal['state'], 'operation_id': plan['operation_id'],
            'plan_digest': plan['plan_digest'], 'scopes': plan['scopes'],
            'scope_status': scope_status, 'targets': plan['targets'], 'actions': actions,
            'retained': plan['retained'], 'warnings': plan['warnings'], 'home_retention': homes,
            'recovery_capability': 'cloud-dependencies-and-local-stdlib' if plan.get('cloud_recovery') else 'local-stdlib',
            'recovery_directory': plan['operation_dir'],
            'retention_basis': 'Recorded completed effects only; status does not re-inspect local or cloud state',
            'last_action': journal.get('last_action'),
            'pending': [identity for identity, entry in actions.items() if entry['state'] != 'completed'],
            'last_error': journal.get('last_error'), 'live_state_checked': False}


def _approve(plan, args):
    if args.confirm != plan['plan_digest']:
        raise UninstallError('--confirm must equal the complete reviewed plan digest')
    required = set(plan['requires'])
    if required - {'writers-quiescent', 'harnesses-stopped', 'harness-config-risk', 'purge', 'abandon-sessions'}:
        raise UninstallError('unsupported approval requirement; preserve this plan')
    if 'writers-quiescent' in required and not args.writers_quiescent:
        raise UninstallError('stop affected writers and explicitly attest --writers-quiescent')
    if 'harnesses-stopped' in required and not args.harnesses_stopped:
        raise UninstallError('close affected assistant harnesses and attest --harnesses-stopped')
    if 'harness-config-risk' in required and not getattr(args, 'harness_config_risk', False):
        raise UninstallError('acknowledge running harness configuration rewrite/reload risk with --harness-config-risk')
    confirmations = plan['confirmations']
    if args.confirm_purge != confirmations.get('purge'):
        raise UninstallError('--confirm-purge must name the exact reviewed instance, only for purge')
    if args.confirm_abandon != confirmations.get('abandon'):
        raise UninstallError('--confirm-abandon must name the exact reviewed instance, only for abandonment')


def _resume_arguments(args):
    result = ['resume', '--plan', str(Path(args.plan).absolute()), '--confirm', args.confirm]
    for flag in ('writers_quiescent', 'harnesses_stopped', 'harness_config_risk'):
        if getattr(args, flag, False):
            result.append('--' + flag.replace('_', '-'))
    for flag in ('confirm_purge', 'confirm_abandon'):
        if getattr(args, flag):
            result.extend(['--' + flag.replace('_', '-'), getattr(args, flag)])
    if getattr(args, 'wait_seconds', 0):
        result.extend(['--wait-seconds', str(args.wait_seconds)])
    return result


def _reexec_runner(path, plan, args, *, system=False):
    runner = path.parent / 'runner' / 'run.py'
    if plan.get('cloud_recovery'):
        from tools.tui.runtime import cloud_command
        command, environment = cloud_command(plan, path.parent, _resume_arguments(args))
        os.execve(command[0], command, environment)
    executable = sys.executable
    if system:
        executable = _external_python(plan['machine_homes'], plan['recovery_python'])
    environment = dict(os.environ)
    if system:
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
            environment.pop(key, None)
    os.execve(executable, [executable, '-B', str(runner), *_resume_arguments(args)], environment)


def apply(args, *, reexec=True, progress=None, should_pause=None):
    path, plan = _load_plan(args.plan)
    _approve(plan, args)
    _verify_runner(plan, path.parent)
    if plan.get('cloud_recovery'):
        from tools.tui.runtime import cloud_command
        cloud_command(plan, path.parent, [])
    if getattr(args, 'tui_worker', False):
        _verify_worker(path, plan)
        reexec = False
    # Execute exactly the reviewed implementation even if the installation was updated.
    if reexec and (Path(__file__).resolve() != path.parent / 'runner/tools/uninstall/cli.py'
                   or (plan.get('cloud_recovery') and str(path.parent / 'cloud-runtime/site') not in sys.path)):
        _reexec_runner(path, plan, args)
    journal_path = path.parent / 'journal.json'
    with _operation_lock(path.parent):
        journal = _journal(path, plan)
        last_progress_at, last_transition = 0.0, None
        def save():
            nonlocal last_progress_at, last_transition
            journal['updated_at'] = _utc()
            save_private(journal_path, journal)
            if progress is not None:
                current = journal.get('last_action', {}).get('id')
                transition = (journal['state'], current, journal['actions'].get(current, {}).get('state'))
                now = time.monotonic()
                # Durable writes remain per-effect; rendering must not reread the
                # entire inventory for each file removed from a large runtime.
                if transition != last_transition or now - last_progress_at >= 0.25:
                    progress(_status(plan, journal))
                    last_progress_at, last_transition = now, transition
        if journal['state'] == 'completed':
            return status(path)
        journal['state'] = 'applying'
        journal.pop('last_error', None)
        save()
        provider_pending = False
        for action in plan['actions']:
            entry = journal['actions'].setdefault(action['id'], {'state': 'pending', 'progress': {}})
            if entry['state'] == 'completed':
                continue
            if should_pause is not None and should_pause():
                journal['state'] = 'paused'
                save()
                return status(path)
            if action['handler'] not in HANDLERS:
                raise UninstallError('unrecognized uninstall action handler')
            # Cloud and project work run while dependencies exist. Re-enter with system
            # Python before deleting any selected environment containing this interpreter.
            if reexec and action['stage'] >= 90 and any(
                    _inside(Path(sys.prefix).absolute(), Path(home)) for home in plan['machine_homes']):
                save()
                _reexec_runner(path, plan, args, system=True)
            entry['state'] = 'started'
            journal['last_action'] = {'id': action['id'], 'kind': action['kind'],
                                      'target': action['target']}
            save()
            try:
                if provider_pending and action['handler'] != 'local':
                    raise UninstallError('Pending provider work permits only independent local actions')
                module = importlib.import_module('tools.uninstall.' + action['handler'])
                if action['handler'] == 'cloud':
                    with module.provider_wait(getattr(args, 'wait_seconds', 0), should_pause):
                        result = module.execute(action, entry['progress'], save)
                else:
                    result = module.execute(action, entry['progress'], save)
                if (isinstance(result, dict) and result.get('status') == 'pending-provider'
                        and action['handler'] == 'cloud' and plan.get('cloud_recovery')):
                    # Only the secret-window boundary, after all other cloud effects,
                    # grants local independence. Unknown failures still stop immediately.
                    if result.get('independent_local_safe') is not True:
                        raise UninstallError('Cloud recovery has not established local independence')
                    entry['result'], entry['state'] = result, 'pending-provider'
                    provider_pending = True
                    save()
                    continue
                if not isinstance(result, dict) or result.get('status') not in SUCCESS:
                    if isinstance(result, dict):
                        entry['result'] = result
                    raise UninstallError('action has not reached a verified terminal outcome; inspect status and resume')
                entry['result'] = result
                entry['state'] = 'completed'
                save()
            except PauseRequested:
                entry['state'], journal['state'] = 'pending', 'paused'
                save()
                return status(path)
            except Exception as error:
                entry['state'] = 'unresolved'
                journal['state'] = 'blocked'
                # Do not leak raw SDK, SQL, subprocess, or credential error diagnostics.
                message = str(error) if isinstance(error, UninstallError) else type(error).__name__
                journal['last_error'] = {'action_id': action['id'], 'kind': action['kind'], 'message': message}
                save()
                return status(path)
        journal['state'] = 'pending-provider' if provider_pending else 'completed'
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
        command.add_argument('--harness-config-risk', action='store_true',
                             help='Acknowledge running harnesses may rewrite removed configuration and need restart')
        command.add_argument('--confirm-purge', metavar='INSTANCE_UUID')
        command.add_argument('--confirm-abandon', metavar='INSTANCE_UUID')
        command.add_argument('--wait-seconds', type=int, choices=range(301), default=0,
                             metavar='0..300', help='Bounded cancellable provider wait per cloud action')
        command.add_argument('--tui-worker', action='store_true', help=argparse.SUPPRESS)
    inspect = commands.add_parser('status', help='Read local receipts without cloud calls')
    inspect.add_argument('--plan', required=True, type=Path)
    return root


def _verify_worker(path, plan):
    """Only the reviewed recovery snapshot may run without normal re-execution."""
    expected = path.parent / 'runner/tools/uninstall/cli.py'
    if Path(__file__).resolve() != expected:
        raise UninstallError('TUI worker must execute the exact snapshotted recovery CLI')
    locations = [Path(sys.executable).absolute(), Path(sys.executable).resolve(),
                 Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    if any(_inside(location, Path(home)) for home in plan['machine_homes'] for location in locations):
        raise UninstallError('TUI worker interpreter must be outside every selected installation home')


class _PauseInput:
    """Read control lines only at safe boundaries; EOF means the UI was lost."""

    def __init__(self):
        self.requested = False
        self.buffer = b''
        self.descriptor = sys.stdin.fileno()

    def request(self, *_):
        self.requested = True

    def __call__(self):
        while not self.requested and select.select([self.descriptor], [], [], 0)[0]:
            chunk = os.read(self.descriptor, 4096)
            if not chunk:
                self.requested = True
                break
            self.buffer += chunk
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if line.strip() == b'pause':
                    self.requested = True
            if len(self.buffer) > 4096:
                self.requested = True
        return self.requested


def _worker(args):
    control = _PauseInput()
    stream = sys.stdout

    def emit(event, data):
        try:
            stream.write(json.dumps({'event': event, 'data': data}, ensure_ascii=False,
                                    allow_nan=False) + '\n')
            stream.flush()
        except (BrokenPipeError, OSError):
            control.request()

    previous = {number: signal.signal(number, control.request)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        # Reserve stdout for the protocol, including when handlers use libraries
        # that write diagnostics. Those diagnostics are never parsed as progress.
        with redirect_stdout(sys.stderr):
            try:
                result = apply(args, progress=lambda data: emit('progress', data),
                               should_pause=control)
            except Exception as error:
                result = {'status': 'blocked',
                          'error': str(error) if isinstance(error, UninstallError) else type(error).__name__,
                          'guidance': 'Preserve the operation directory and inspect status before resuming.'}
        emit('result', result)
        return _exit_status(result)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _exit_status(result):
    return {'blocked': 2, 'paused': 3, 'pending-provider': 4}.get(result['status'], 0)


def _interactive():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise UninstallError('interactive uninstall requires a terminal; use plan/apply/status/resume for noninteractive recovery')
    from tools.tui.runtime import launch_uninstaller
    return launch_uninstaller()


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command is None:
            return _interactive()
        elif args.command == 'plan':
            result = build_plan(args)
        elif args.command == 'status':
            result = status(args.plan)
        else:
            if args.tui_worker:
                return _worker(args)
            result = apply(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return _exit_status(result)
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
