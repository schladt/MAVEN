"""Stdlib-only, inventory-bound local detach and installed-runtime removal.

Ownership is never inferred from a directory name. Recovery data, shared caches,
source checkouts and changed or unowned artifacts remain in place.
"""
from __future__ import annotations

import contextlib
import fcntl
import configparser
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile
from uuid import UUID

from .common import (UninstallError, canonical, checked_path, digest, file_digest,
                     private_directory, read_private, save_private)

MANIFEST = 'installation.json'
REGISTRY = '.maven-installations/installations.json'


def _exists(path):
    return os.path.lexists(path)


def _private_environment(path):
    checked_path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise UninstallError(f'Private single-link environment required: {path}')


def _entry(path):
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise UninstallError(f'Unowned artifact preserved: {path}')
    result = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
              'dev': info.st_dev, 'ino': info.st_ino}
    if stat.S_ISLNK(info.st_mode):
        result.update(type='symlink', link=os.readlink(path))
    elif stat.S_ISDIR(info.st_mode):
        result.update(type='directory')
    elif stat.S_ISREG(info.st_mode):
        result.update(type='file', size=info.st_size, sha256=file_digest(path))
    else:
        raise UninstallError(f'Nonregular artifact preserved: {path}')
    return result


def inventory(path, *, exclude=()):
    """Exact lstat inventory; never descend into symlinks or mounted trees."""
    path = checked_path(path)
    result = {}
    device = path.lstat().st_dev

    def visit(current, relative):
        if relative and any(relative == name or relative.startswith(name + '/') for name in exclude):
            return
        item = _entry(current)
        if item['dev'] != device:
            raise UninstallError(f'Mount boundary preserved: {current}')
        result[relative] = item
        if item['type'] == 'directory':
            for child in sorted(current.iterdir()):
                if child.name == '.git':
                    raise UninstallError(f'Source checkout preserved: {current}')
                visit(child, str(Path(relative) / child.name) if relative else child.name)
    visit(path, '')
    return result


def _registry_path():
    return Path.home() / REGISTRY


def discover_homes(extra=()):
    """Known default, environment, explicit and installer-registered homes only."""
    candidates = [Path.home() / '.maven', *extra]
    if os.environ.get('MAVEN_HOME'):
        candidates.append(os.environ['MAVEN_HOME'])
    registry = _registry_path()
    if _exists(registry):
        value = read_private(registry)
        if value.get('schema') != 1 or not isinstance(value.get('homes'), list):
            raise UninstallError('Malformed installer discovery registry; preserve for review')
        candidates.extend(value['homes'])
    result = set()
    explicit = {str(checked_path(item, exists=False)) for item in extra}
    for candidate in candidates:
        path = checked_path(candidate, exists=False)
        if path.is_dir() and (str(path) in explicit or _installation_active(path)):
            result.add(str(path))
    return sorted(result)


@contextlib.contextmanager
def _lock(path):
    private_directory(path.parent)
    checked_path(path, exists=False)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UninstallError(f'Unsafe coordination lock: {path}')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UninstallError(f'Another local writer holds {path}') from None
        yield
    finally:
        os.close(fd)


def record_installation(home, source, owned_paths, skill_references, *, cache_roots=(), integrations=()):
    """Installer receipt; only newly created or explicitly upgraded owned paths."""
    home = checked_path(home)
    target = home / MANIFEST
    prior = read_private(target) if _exists(target) else {}
    if prior and (prior.get('schema') != 1 or prior.get('home') != str(home)):
        raise UninstallError('Existing installer ownership receipt is invalid')
    owned = dict(prior.get('owned', {})) if prior.get('active', True) else {}
    for path in owned_paths:
        path = checked_path(path)
        owned[str(path)] = inventory(path)
    previous_references = prior.get('skill_references', []) if prior.get('active', True) else []
    references = sorted(set(previous_references) | {str(p) for p in skill_references})
    caches = dict(prior.get('cache_roots', {}))
    for path in cache_roots:
        path = checked_path(path)
        if path not in (home / 'runtime/aws-login-cache', home / 'runtime/aws-deploy-login-cache'):
            raise UninstallError('Only installer-created dedicated cache directories can be owned')
        caches[str(path)] = _entry(path)
    previous_integrations = prior.get('integrations', []) if prior.get('active', True) else []
    integration_map = {(item['path'], item['format'], item['key']): item
                       for item in previous_integrations}
    for item in integrations:
        key = (item['path'], item['format'], item['key'])
        previous = integration_map.get(key)
        if previous and previous.get('owned', True) and item.get('owned') is False:
            left, right = previous['value'], item['value']
            if item['format'] == 'text-block':
                left, right = left.lstrip('\n'), right.lstrip('\n')
            if left == right:
                continue  # Keep the original exact ownership/removal receipt.
        integration_map[key] = item
    save_private(target, {'schema': 1, 'home': str(home), 'source': str(source),
                          'owned': owned, 'skill_references': references, 'cache_roots': caches,
                          'integrations': list(integration_map.values()), 'active': True})
    registry = _registry_path()
    with _lock(registry.with_suffix('.lock')):
        value = read_private(registry) if _exists(registry) else {'schema': 1, 'homes': []}
        if value.get('schema') != 1 or not isinstance(value.get('homes'), list):
            raise UninstallError('Malformed installer discovery registry')
        save_private(registry, {'schema': 1, 'homes': sorted(set(value['homes']) | {str(home)})})


def _fragment():
    return {'actions': [], 'retained': [], 'warnings': [], 'targets': [],
            'requires': ['writers-quiescent', 'harnesses-stopped']}


def _action(kind, target, stage, **details):
    return {'handler': 'local', 'kind': kind, 'target': str(target),
            'stage': stage, 'details': details}


def _quiet(home, root=None, *, allow_sessions=False):
    registry = home / 'runtime/sessions.json'
    if not _exists(registry):
        return
    value = read_private(registry)
    if (value.get('schema') != 1 or not isinstance(value.get('sessions'), dict)
            or not isinstance(value.get('reservations'), dict)):
        raise UninstallError('Malformed local session registry; recover it before uninstall')
    for session in value['sessions'].values():
        if not isinstance(session, dict) or not isinstance(session.get('root'), str):
            raise UninstallError('Malformed local session entry; recover it before uninstall')
        if not allow_sessions and (root is None or session['root'] == str(root)):
            raise UninstallError('Stop/recover registered local sessions before uninstall; no liveness guesses are made')
    if not allow_sessions and any(root is None or key == str(root) for key in value['reservations']):
        raise UninstallError('Release local repository reservations before uninstall')


def _guard(home, root=None, *, offline=False):
    _quiet(home, root, allow_sessions=offline)
    state = _registry_state(home)
    affected = {key: item for key, item in state['sessions'].items()
                if root is None or item['root'] == str(root)}
    return _action('local_barrier', home, 5, home=str(home), root=str(root) if root else None,
                   offline=offline, registry_before=digest(state), affected_sessions=sorted(affected),
                   registry_sessions={key: digest(item) for key, item in affected.items()},
                   registry_reservations={key: digest(item) for key, item in state['reservations'].items()
                                          if root is None or key == str(root)})


def _export(fragment, source, destination, home, root=None, *, stage=80, move=True, exclude=()):
    if destination is None or not _exists(source):
        return
    destination = checked_path(destination, exists=False)
    protected_roots = (source, home, root) if root else (source, home)
    if any(destination == parent or parent in destination.parents for parent in protected_roots):
        raise UninstallError('Export destination must be outside selected home/project/source trees')
    entries = inventory(source, exclude=exclude)
    name = f'{source.name}-{digest(str(source))[:16]}.zip'
    output = destination / name
    if _exists(output) or _exists(output.with_suffix('.partial')):
        raise UninstallError(f'Export destination already exists; no overwrite allowed: {output}')
    fragment['actions'].append(_action('local_export', source, stage, home=str(home),
        root=str(root) if root else None, inventory=entries, destination=str(output),
        move=move, exclude=list(exclude)))
    fragment['targets'].append({'scope': 'export', 'path': str(output)})
    if not move:
        fragment['retained'].append({'path': str(source),
            'reason': 'Verified recovery copy exported; original retained for unresolved evidence or minimal reactivation binding'})


def plan_project(root, home, *, offline=False, archive=False, export=None):
    root, home = checked_path(root), checked_path(home, exists=not offline)
    if offline and archive:
        raise UninstallError('Offline project detach cannot archive cloud memory')
    config = root / '.maven/project.json'
    project = read_private(config)
    if project.get('schema') != 1 or project.get('root') != str(root) or not project.get('project_id'):
        raise UninstallError('Project identity does not match the explicit checkout')
    fragment = _fragment()
    if not offline:
        from . import lifecycle
        cloud = lifecycle.plan_project(root, home, archive=archive)
        for key in fragment:
            fragment[key].extend(cloud.get(key, []))
    else:
        fragment['warnings'].append('Offline detach does not verify cloud sessions or claim cloud closure.')
    fragment['actions'].append(_guard(home, root, offline=offline))
    entries = inventory(config)
    destination = config.with_name('detached-project-' + entries['']['sha256'] + '.json')
    if _exists(destination):
        raise UninstallError(f'Detached configuration already exists; preserve it for review: {destination}')
    fragment['actions'].append(_action('local_project_detach', config, 60, home=str(home),
        root=str(root), inventory=entries, destination=str(destination)))
    if export is not None:
        _export(fragment, config, export, home, root, stage=59, move=False)
        _project_exports(fragment, root, home, export)
    fragment['actions'].append(_action('local_project_release', root, 61,
        home=str(home), root=str(root)))
    fragment['targets'].append({'scope': 'project', 'root': str(root), 'home': str(home),
                                'project_id': project['project_id']})
    fragment['retained'].append({'path': str(root / '.maven'),
        'reason': 'Configuration is exported or renamed in place; receipts/history and .gitignore protection remain.'})
    receipt = root / '.maven/integration.json'
    if not _exists(receipt):
        fragment['warnings'].append('Legacy project integration: no ownership receipt; ignore rules and other integration files are preserved.')
    return fragment


def _shared_skills(home, selected_homes):
    protected = set()
    unknown = False
    selected = {str(checked_path(p, exists=False)) for p in selected_homes} | {str(home)}
    for candidate in discover_homes(selected_homes):
        if candidate in selected:
            continue
        receipt = Path(candidate) / MANIFEST
        if not _exists(receipt):
            unknown = True
            continue
        manifest = read_private(receipt)
        if manifest.get('schema') != 1 or manifest.get('home') != candidate:
            raise UninstallError(f'Invalid unselected installation receipt: {receipt}')
        protected.update(manifest.get('skill_references', []))
    return protected, unknown


def plan_machine(home, *, selected_homes=(), offline=False, export=None, adopt_legacy=False):
    home = checked_path(home)
    fragment = _fragment()
    fragment['actions'].append(_guard(home, offline=offline))
    fragment['targets'].append({'scope': 'machine', 'home': str(home)})
    fragment['warnings'].append('Discovery covers only default, MAVEN_HOME, explicit and installer-registered homes; external shared caches are never removed.')
    if offline:
        fragment['warnings'].append('Offline approval invalidates only selected local registrations after writer attestation and locks; original evidence is retained and remote sessions/tokens are not closed or revoked.')
    receipt = home / MANIFEST
    manifest = read_private(receipt) if _exists(receipt) else {}
    if manifest and (manifest.get('schema') != 1 or manifest.get('home') != str(home)
                     or not isinstance(manifest.get('owned'), dict)):
        raise UninstallError('Installer ownership manifest does not match this home')
    shared, unknown = _shared_skills(home, selected_homes)
    removable = set()
    for name, expected in manifest.get('owned', {}).items():
        try:
            path = checked_path(name, exists=False)
        except UninstallError as exc:
            fragment['retained'].append({'path': str(Path(name).expanduser().absolute()),
                                         'reason': f'Changed or unsafe owned path preserved: {exc}'})
            continue
        reason = None
        internal = path in (home / 'venv', home / 'activate')
        skill = str(path) in manifest.get('skill_references', []) and path.name == 'SKILL.md'
        source = Path(manifest.get('source', '/'))
        if not internal and not skill:
            reason = 'Unrecognized ownership target; retained'
        elif source == path or path in source.parents:
            reason = 'Source checkout; never removed'
        elif skill and (str(path) in shared or unknown):
            reason = 'Shared skill or uncertain unselected installation ownership'
        elif not _exists(path):
            continue
        else:
            try:
                if inventory(path) != expected:
                    reason = 'Modified since installer ownership receipt'
            except (OSError, ValueError) as exc:
                reason = f'Unsafe artifact preserved: {exc}'
        if reason:
            fragment['retained'].append({'path': str(path), 'reason': reason})
        else:
            removable.add(str(path))
            fragment['actions'].append(_action('local_remove', path, 90, home=str(home),
                root=None, inventory=expected, source=str(source), manifest=file_digest(receipt),
                skill=skill, selected_homes=[str(checked_path(p, exists=False)) for p in selected_homes]))
    if adopt_legacy and str(home / 'venv') not in manifest.get('owned', {}):
        runtime, expected = _legacy_runtime(home)
        removable.add(str(runtime))
        fragment['actions'].append(_action('local_remove', runtime, 90, home=str(home),
            root=None, inventory=expected, manifest=None, legacy=True, skill=False))
        fragment['warnings'].append('Explicit legacy adoption approves the complete reviewed dedicated MAVEN virtual-environment inventory; no other legacy artifact is claimed.')
    elif not manifest:
        fragment['warnings'].append('Legacy/unowned runtime retained. Explicit --adopt-legacy can review and remove a verified dedicated MAVEN virtual environment.')
    _plan_integrations(fragment, home, manifest, selected_homes)
    _plan_caches(fragment, home, manifest, selected_homes)
    for child in sorted(home.iterdir()):
        if export is not None and child.name == '.env':
            _private_environment(child)
            _export(fragment, child, export, home)
        elif str(child) not in removable:
            fragment['retained'].append({'path': str(child), 'reason': 'Recovery/coordination evidence, shared caches, ownership receipt, modified or unowned artifact; not exported or removed'})
    if export is not None and _exists(home / 'runtime'):
        _export(fragment, home / 'runtime', export, home, move=False,
                exclude=_runtime_exclusions(home / 'runtime'))
    fragment['actions'].append(_action('local_machine_finalize', home, 100, home=str(home),
        root=None, manifest_before=digest(manifest) if manifest else None,
        removed_targets=[item['target'] for item in fragment['actions']
                         if item['kind'] == 'local_remove'
                         or item['kind'] == 'local_export' and item['details'].get('move')]))
    return fragment


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_remaining(path, expected, progress, order):
    current = inventory(path) if _exists(path) else {}
    cursor = progress.get('cursor', 0)
    if type(cursor) is not int or not 0 <= cursor <= len(order):
        raise UninstallError('Invalid removal journal cursor')
    done = set(order[:cursor])
    intent = progress.get('intent')
    if intent is not None and (cursor == len(order) or intent != order[cursor]):
        raise UninstallError('Removal journal intent does not match its approved order')
    for name in done:
        if name in current:
            raise UninstallError(f'Removed artifact reappeared; approval invalid: {path / name}')
    remaining = {name: value for name, value in expected.items() if name not in done}
    if intent in remaining and intent not in current:
        remaining.pop(intent)
    if current != remaining:
        raise UninstallError(f'Local inventory changed; create a new approved plan: {path}')
    return current


def _remove(action, progress, save):
    path = Path(action['target'])
    operation = action.get('operation_dir')
    if operation and (Path(operation) == path or path in Path(operation).parents):
        raise UninstallError('Recovery operation directory overlaps the deletion target')
    for running in (Path(sys.prefix), Path(sys.executable).absolute(), Path(__file__).absolute()):
        if running == path or path in running.parents:
            raise UninstallError('Use the external recovery zipapp with a non-target system Python before removing this runtime')
    expected = action['details']['inventory']
    order = sorted(expected, key=lambda value: (len(Path(value).parts), value), reverse=True)
    current = _check_remaining(path, expected, progress, order)
    for index in range(progress.get('cursor', 0), len(order)):
        name = order[index]
        progress['intent'] = name
        save()
        target = path / name if name else path
        if name in current:
            checked_path(target.parent)
            if _entry(target) != expected[name]:
                raise UninstallError(f'Artifact changed during removal: {target}')
            if expected[name]['type'] == 'directory':
                target.rmdir()
            else:
                target.unlink()
            _sync(target.parent)
        progress['cursor'] = index + 1
        progress.pop('intent', None)
        save()
    return {'status': 'removed', 'path': str(path), 'entries': len(expected),
            'token_revocation_claimed': False}


def _detach(action, progress, save):
    source, destination = Path(action['target']), Path(action['details']['destination'])
    expected = action['details']['inventory']
    if _exists(destination):
        if not progress.get('intent') or inventory(destination) != expected:
            raise UninstallError('Detached destination is not this journaled configuration; no overwrite allowed')
        if _exists(source):
            if inventory(source) != expected or not os.path.samefile(source, destination):
                raise UninstallError('Project configuration changed during detach')
            source.unlink()
            _sync(source.parent)
    else:
        if inventory(source) != expected:
            raise UninstallError('Project configuration changed; approval invalid')
        progress['intent'] = 'detach'
        save()
        # Exclusive hard link plus unlink is resumable and never overwrites evidence.
        os.link(source, destination, follow_symlinks=False)
        _sync(destination.parent)
        source.unlink()
        _sync(source.parent)
    progress['complete'] = True
    save()
    return {'status': 'detached', 'configuration_retained': str(destination),
            'cloud_closure_claimed': False}


def _export_entries(path, expected, exclude=()):
    if inventory(path, exclude=exclude) != expected:
        raise UninstallError(f'Export source changed; approval invalid: {path}')


def _verify_archive(path, expected):
    with zipfile.ZipFile(path) as archive:
        names = [name for name, item in expected.items() if item['type'] != 'directory']
        expected_names = {'inventory.json'} | {'data/' + (name or 'content') for name in names}
        if len(archive.namelist()) != len(expected_names) or set(archive.namelist()) != expected_names:
            raise UninstallError('Recovery archive member inventory mismatch')
        if archive.read('inventory.json') != canonical(expected):
            raise UninstallError('Recovery archive manifest mismatch')
        for name in names:
            item = expected[name]
            hasher = hashlib.sha256()
            with archive.open('data/' + (name or 'content')) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
            wanted = item.get('sha256') if item['type'] == 'file' else hashlib.sha256(os.fsencode(item['link'])).hexdigest()
            if hasher.hexdigest() != wanted:
                raise UninstallError('Recovery archive content verification failed')


def _export_execute(action, progress, save):
    source, output = Path(action['target']), Path(action['details']['destination'])
    expected = action['details']['inventory']
    private_directory(output.parent)
    checked_path(output, exists=False)
    if _exists(output):
        if not progress.get('intent'):
            raise UninstallError('Export destination already exists; no destructive overwrite allowed')
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UninstallError('Recovery archive is not an owner-private regular file')
        _verify_archive(output, expected)
        staging = output.with_suffix('.partial')
        if _exists(staging):
            if not os.path.samefile(staging, output):
                raise UninstallError('Unrelated export staging file preserved')
            staging.unlink()
            _sync(output.parent)
    else:
        _export_entries(source, expected, _export_exclusions(action))
        staging = output.with_suffix('.partial')
        if not progress.get('intent'):
            if _exists(staging):
                raise UninstallError(f'Unowned export staging file preserved: {staging}')
            progress['intent'] = 'export'
            save()
        if _exists(staging):
            info = staging.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise UninstallError(f'Unsafe interrupted export staging: {staging}')
            staging.unlink()
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr('inventory.json', canonical(expected))
                    for name, item in sorted(expected.items()):
                        if item['type'] == 'directory':
                            continue
                        target = source / name if name else source
                        if _entry(target) != item:
                            raise UninstallError(f'Export artifact changed: {target}')
                        member = 'data/' + (name or 'content')
                        if item['type'] == 'symlink':
                            archive.writestr(member, os.fsencode(item['link']))
                        else:
                            archive.write(target, member)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if _exists(staging):
                progress['staging'] = _entry(staging)
                save()
        _export_entries(source, expected, _export_exclusions(action))
        _verify_archive(staging, expected)
        os.link(staging, output, follow_symlinks=False)
        _sync(output.parent)
        staging.unlink()
        _sync(output.parent)
    if not action['details'].get('move', True):
        _export_entries(source, expected, _export_exclusions(action))
    progress['verified'] = file_digest(output)
    save()
    if action['details'].get('move', True):
        removal = progress.setdefault('removal', {})
        _remove(action, removal, save)
    progress['complete'] = True
    save()
    return {'status': 'verified', 'archive': str(output), 'sha256': progress['verified'],
            'originals_retained': not action['details'].get('move', True), 'live_restore_verified': False}


def execute(action, progress, save):
    details = action['details']
    home = checked_path(details['home'], exists=False)
    root = details.get('root')
    with _execution_locks(home, root):
        _quiet(home, root, allow_sessions=action['kind'] == 'local_barrier' and details.get('offline', False))
        marker_path = home / 'runtime/uninstall.json'
        marker = read_private(marker_path) if _exists(marker_path) else {
            'schema': 1, 'whole_home': False, 'project_roots': []}
        if (marker.get('schema') != 1 or type(marker.get('whole_home')) is not bool
                or not isinstance(marker.get('project_roots'), list)):
            raise UninstallError('Malformed removal marker; preserve it for recovery')
        kind = action['kind']
        if kind == 'local_barrier':
            progress['intent'] = 'barrier'
            save()
            marker['whole_home'] = marker['whole_home'] or root is None
            if root:
                marker['project_roots'] = sorted(set(marker['project_roots']) | {root})
            marker.setdefault('actions', {})[digest(action)] = {'root': root}
            save_private(marker_path, marker)
            if details.get('offline'):
                _detach_registry(action, progress, save)
            return {'status': 'verified', 'local_writer_barrier': str(marker_path)}
        if kind == 'local_project_release':
            progress['intent'] = 'release'
            save()
            if not marker['whole_home']:
                marker['project_roots'] = [item for item in marker['project_roots'] if item != root]
                marker['actions'] = {key: value for key, value in marker.get('actions', {}).items()
                                     if value.get('root') != root}
                save_private(marker_path, marker)
            return {'status': 'detached', 'project_barrier_released': not marker['whole_home']}
        if not marker['whole_home'] and root not in marker['project_roots']:
            raise UninstallError('Persistent local removal barrier is missing')
        if kind == 'local_machine_finalize':
            return _finalize_machine(action, progress, save)
        if kind == 'local_integration':
            return _remove_integration(action, progress, save)
        if kind == 'local_remove':
            if details.get('manifest') is not None and file_digest(home / MANIFEST) != details['manifest']:
                raise UninstallError('Installer ownership changed; approval invalid')
            if details.get('skill') or details.get('cache'):
                with _sharing_locks(home, details.get('selected_homes', [])):
                    inspect = _shared_skills if details.get('skill') else _cache_references
                    shared, unknown = inspect(home, details.get('selected_homes', []))
                    if unknown or action['target'] in shared:
                        raise UninstallError('Shared artifact dependencies changed; approval invalid')
                    return _remove(action, progress, save)
            return _remove(action, progress, save)
        if kind == 'local_project_detach':
            return _detach(action, progress, save)
        if kind == 'local_export':
            return _export_execute(action, progress, save)
        raise UninstallError(f'Unsupported local uninstall action: {kind}')


def _registry_state(home):
    path = home / 'runtime/sessions.json'
    return read_private(path) if _exists(path) else {'schema': 1, 'sessions': {}, 'reservations': {}}


@contextlib.contextmanager
def _execution_locks(home, root):
    """Hold the real cooperating command/setup locks, not process-liveness guesses."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(_lock(home / 'runtime/registry.lock'))
        setup = home / 'runtime/setup'
        if setup.is_dir():
            stack.enter_context(_lock(setup / 'lock'))
        state = _registry_state(home)
        roots = {str(root)} if root else set()
        held_commands = set()
        for identifier, session in state.get('sessions', {}).items():
            if root is not None and session.get('root') != str(root):
                continue
            try:
                if str(UUID(identifier)) != identifier:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise UninstallError('Malformed session identity; preserve registry for recovery') from None
            checkout = checked_path(session['root'], exists=False)
            roots.add(str(checkout))
            command = checkout / '.maven/runtime/sessions' / identifier / 'command.lock'
            if _exists(command):
                stack.enter_context(_lock(command))
                held_commands.add(str(command))
        for checkout in sorted(roots):
            runtime = checked_path(checkout, exists=False) / '.maven/runtime'
            if _exists(runtime / 'cloud-workspace.lock'):
                raise UninstallError('Workspace writer lock exists; resolve its owner before uninstall')
            if (runtime / 'project-setup').is_dir():
                stack.enter_context(_lock(runtime / 'project-setup/lock'))
            sessions = runtime / 'sessions'
            if sessions.is_dir():
                for directory in sorted(sessions.iterdir()):
                    command = directory / 'command.lock'
                    if str(command) not in held_commands and _exists(command):
                        stack.enter_context(_lock(command))
        yield


def _detach_registry(action, progress, save):
    """Offline local invalidation preserves exact old entries in a private receipt."""
    home = Path(action['details']['home'])
    root = action['details'].get('root')
    state = _registry_state(home)
    current = digest(state)
    if current == progress.get('registry_after'):
        if digest(read_private(progress['offline_evidence'])) != progress['evidence_sha256']:
            raise UninstallError('Offline session evidence changed; preserve registry for recovery')
        return
    _approved_registry_subset(action, state)
    selected = {key: copy.deepcopy(item) for key, item in state['sessions'].items()
                if root is None or item['root'] == root}
    reservations = {key: item for key, item in state['reservations'].items()
                    if root is None or key == root}
    if not selected and not reservations:
        return
    original = {'sessions': copy.deepcopy(selected), 'reservations': copy.deepcopy(reservations)}
    for item in selected.values():
        item['original_state'] = item.get('state')
        item['state'] = 'detached'
        item['access_revoked'] = True
    evidence = {'schema': 1, 'action': digest(action), 'cloud_closure_claimed': False,
                'sessions': selected, 'reservations': reservations, 'original_registry': original}
    destination = home / 'runtime' / ('uninstall-sessions-' + digest(action) + '.json')
    updated = copy.deepcopy(state)
    for key in selected:
        del updated['sessions'][key]
    for key in reservations:
        del updated['reservations'][key]
    progress['registry_after'] = digest(updated)
    progress['offline_evidence'] = str(destination)
    progress['evidence_sha256'] = digest(evidence)
    save()
    if _exists(destination):
        if read_private(destination) != evidence:
            raise UninstallError('Offline session evidence conflicts; nothing overwritten')
    else:
        save_private(destination, evidence)
    save_private(home / 'runtime/sessions.json', updated)
    progress['registry_detached'] = True
    save()


def _runtime_exclusions(runtime):
    excluded = ['registry.lock', 'uninstall.json', 'sessions.json',
                'aws-login-cache', 'aws-deploy-login-cache', 'setup/lock', 'project-setup/lock']
    sessions = runtime / 'sessions'
    if sessions.is_dir():
        excluded.extend('sessions/' + item.name + '/command.lock' for item in sessions.iterdir())
    excluded.extend(item.name for item in runtime.iterdir() if item.name.startswith('uninstall-sessions-'))
    caches = _environment_caches(runtime.parent)
    registry = runtime / 'sessions.json'
    if _exists(registry):
        state = read_private(registry)
        for session in state.get('sessions', {}).values():
            for cache in session.get('caches', []):
                caches.add(str(checked_path(cache['cache'], exists=False)))
    for cache in caches:
        path = Path(cache)
        if runtime in path.parents:
            excluded.append(str(path.relative_to(runtime)))
    return excluded


def _project_exports(fragment, root, home, destination):
    private = root / '.maven'
    runtime = private / 'runtime'
    pending = _exists(private / 'pending-memory.json')
    pending = pending or any(item.get('writers') or item.get('state') in ('starting', 'cleanup_pending')
                             for item in _registry_state(home)['sessions'].values()
                             if item.get('root') == str(root))
    if _exists(runtime):
        entries = inventory(runtime, exclude=_runtime_exclusions(runtime))
        pending = pending or any(Path(name).name == 'pending-memory.json' for name in entries)
        for name, item in entries.items():
            if item['type'] == 'file' and Path(name).name == 'approval.json':
                pending = pending or read_private(runtime / name).get('status') not in ('closed', 'cancelled')
            if item['type'] == 'file' and name == 'project-setup/state.json':
                pending = pending or read_private(runtime / name).get('complete') is not True
            if item['type'] == 'file' and Path(name).name == 'session.json':
                terminal = runtime / Path(name).parent / 'approval.json'
                pending = pending or not _exists(terminal)
        _export(fragment, runtime, destination, home, root, stage=59, move=False,
                exclude=_runtime_exclusions(runtime))
    for name in ('sensitive', 'planning'):
        target = private / name
        if _exists(target):
            _export(fragment, target, destination, home, root, stage=59, move=not pending)
    for name in ('incoming', 'integration.json', 'pending-memory.json'):
        target = private / name
        if _exists(target):
            _export(fragment, target, destination, home, root, stage=59, move=False)
    if pending:
        fragment['warnings'].append('Unresolved project evidence exists: sensitive originals/planning are copied to verified export but remain in place for exact recovery.')


def _legacy_runtime(home):
    """Explicitly approved adoption requires an actual dedicated MAVEN venv."""
    info = home.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UninstallError('Legacy adoption requires an owner-private dedicated installation home')
    if _exists(home / '.git') or _exists(home / 'pyproject.toml'):
        raise UninstallError('Legacy adoption refuses source checkouts')
    runtime = checked_path(home / 'venv')
    configuration = checked_path(runtime / 'pyvenv.cfg')
    settings = {}
    for line in configuration.read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            settings[key.strip()] = value.strip()
    if not settings.get('home') or settings.get('include-system-site-packages', '').lower() != 'false':
        raise UninstallError('Legacy adoption requires an isolated virtual environment')
    libraries = checked_path(runtime / 'lib')
    metadata = list(libraries.glob('python*/site-packages/maven_cloud_memory-*.dist-info/METADATA'))
    if len(metadata) != 1:
        raise UninstallError('Legacy adoption requires exactly one installed MAVEN distribution')
    distribution = checked_path(metadata[0])
    if 'Name: maven-cloud-memory' not in distribution.read_text().splitlines():
        raise UninstallError('Installed distribution identity is not MAVEN')
    direct = distribution.parent / 'direct_url.json'
    if _exists(direct) and json.loads(checked_path(direct).read_text()).get('dir_info', {}).get('editable'):
        raise UninstallError('Editable/source installations are never adopted for deletion')
    entries = configparser.ConfigParser(interpolation=None)
    entries.read_string(checked_path(distribution.parent / 'entry_points.txt').read_text())
    required = {'maven-memory': 'tools.memory.cli:main', 'maven-project': 'tools.project:main',
                'maven-workflow': 'tools.workflow:main'}
    for name, binding in required.items():
        if entries.get('console_scripts', name, fallback=None) != binding:
            raise UninstallError('Legacy MAVEN entry point identity mismatch')
        script = checked_path(runtime / 'bin' / name)
        body = script.read_text()
        module, function = binding.split(':')
        if not body.startswith('#!' + str(runtime / 'bin') + '/') or f'from {module} import {function}' not in body:
            raise UninstallError('Legacy MAVEN executable is not bound to the selected venv')
    return runtime, inventory(runtime)


def _approved_registry_subset(action, state):
    details = action['details']
    root = details.get('root')
    expected = {'sessions': details['registry_sessions'], 'reservations': details['registry_reservations']}
    current = {
        'sessions': {key: digest(item) for key, item in state['sessions'].items()
                     if root is None or item['root'] == root},
        'reservations': {key: digest(item) for key, item in state['reservations'].items()
                         if root is None or key == root}}
    missing = {}
    for kind in expected:
        if any(expected[kind].get(key) != value for key, value in current[kind].items()):
            raise UninstallError('Selected local sessions changed after approval; replan')
        missing[kind] = {key: value for key, value in expected[kind].items() if key not in current[kind]}
    if not any(missing.values()):
        return
    home = Path(details['home'])
    marker = read_private(home / 'runtime/uninstall.json')
    for identifier in marker.get('actions', {}):
        receipt = home / 'runtime' / ('uninstall-sessions-' + identifier + '.json')
        if not _exists(receipt):
            continue
        evidence = read_private(receipt)
        if evidence.get('action') != identifier:
            raise UninstallError('Offline registry evidence identity mismatch')
        for kind in missing:
            for key, item in evidence.get('original_registry', {}).get(kind, {}).items():
                if missing[kind].get(key) == digest(item):
                    del missing[kind][key]
    if any(missing.values()):
        raise UninstallError('Selected sessions disappeared without approved detach evidence')


def _cache_references(home, selected_homes):
    selected = {str(home)} | {str(checked_path(p, exists=False)) for p in selected_homes}
    references = set()
    uncertain = False
    for candidate in discover_homes(selected_homes):
        if candidate in selected:
            continue
        other = Path(candidate)
        if not _exists(other / MANIFEST):
            uncertain = True
        try:
            references.update(_environment_caches(other))
        except (OSError, ValueError):
            uncertain = True
        registry = other / 'runtime/sessions.json'
        if _exists(registry):
            state = read_private(registry)
            if not isinstance(state.get('sessions'), dict):
                uncertain = True
                continue
            for item in state['sessions'].values():
                if not isinstance(item, dict) or not isinstance(item.get('caches', []), list):
                    uncertain = True
                    continue
                for cache in item.get('caches', []):
                    try:
                        references.add(str(checked_path(cache['cache'], exists=False)))
                    except (KeyError, TypeError, OSError, ValueError):
                        uncertain = True
    return references, uncertain


def _plan_caches(fragment, home, manifest, selected_homes):
    references, uncertain = _cache_references(home, selected_homes)
    for path in (home / 'runtime/aws-login-cache', home / 'runtime/aws-deploy-login-cache'):
        if not _exists(path):
            continue
        reason = None
        if str(path) not in manifest.get('cache_roots', {}):
            reason = 'No installer ownership proof for credential cache; preserved'
        elif str(path) in references or uncertain:
            reason = 'Credential cache is shared or another installation reference is uncertain'
        elif _entry(path) != manifest['cache_roots'][str(path)]:
            reason = 'Credential cache root identity changed; preserved'
        else:
            entries = inventory(path)
            if any(item['type'] == 'symlink' or item['mode'] & 0o077 for item in entries.values()):
                reason = 'Credential cache is not an entirely owner-private regular tree'
        if reason:
            fragment['retained'].append({'path': str(path), 'reason': reason})
        else:
            fragment['actions'].append(_action('local_remove', path, 85, home=str(home), root=None,
                inventory=entries, manifest=file_digest(home / MANIFEST), cache=True,
                selected_homes=[str(checked_path(p, exists=False)) for p in selected_homes]))


def _export_exclusions(action):
    excluded = list(action['details'].get('exclude', ()))
    source = Path(action['target'])
    home = Path(action['details']['home'])
    if source == home / 'runtime':
        marker = read_private(home / 'runtime/uninstall.json')
        for identifier in marker.get('actions', {}):
            path = source / ('uninstall-sessions-' + identifier + '.json')
            if _exists(path):
                evidence = read_private(path)
                if evidence.get('action') != identifier:
                    raise UninstallError('Generated offline evidence identity mismatch')
                excluded.append(path.name)
    return excluded


def _installation_active(home):
    manifest = home / MANIFEST
    if _exists(manifest) and read_private(manifest).get('active') is False:
        return False
    completed = home / 'runtime/uninstall-complete.json'
    if _exists(completed):
        receipt = read_private(completed)
        if receipt.get('home') != str(home) or receipt.get('schema') != 1:
            raise UninstallError('Completed uninstall receipt identity mismatch')
        return False
    return True


def _finalize_machine(action, progress, save):
    home = Path(action['target'])
    for target in action['details'].get('removed_targets', []):
        if _exists(target):
            raise UninstallError('Approved removal/export is incomplete; cannot finalize this installation')
    manifest_path = home / MANIFEST
    manifest = read_private(manifest_path) if _exists(manifest_path) else None
    before = action['details'].get('manifest_before')
    if manifest is not None:
        current = digest(manifest)
        if current != before and current != progress.get('manifest_after'):
            raise UninstallError('Installation ownership changed before finalization')
        if current == before:
            manifest['active'] = False
            manifest['uninstalled_by'] = digest(action)
            progress['manifest_after'] = digest(manifest)
            progress['intent'] = 'finalize'
            save()
            save_private(manifest_path, manifest)
    elif before is not None:
        raise UninstallError('Installation ownership receipt disappeared before finalization')
    registry = _registry_path()
    with _lock(registry.with_suffix('.lock')):
        if _exists(registry):
            state = read_private(registry)
            if state.get('schema') != 1 or not isinstance(state.get('homes'), list):
                raise UninstallError('Malformed discovery registry; preserve it for recovery')
            progress['intent'] = 'unregister'
            save()
            state['homes'] = [item for item in state['homes'] if item != str(home)]
            save_private(registry, state)
    completed = home / 'runtime/uninstall-complete.json'
    marker = read_private(home / 'runtime/uninstall.json')
    receipt = {'schema': 1, 'home': str(home), 'action': digest(action),
               'marker_sha256': digest(marker), 'token_revocation_claimed': False}
    progress['intent'] = 'complete'
    save()
    if _exists(completed) and read_private(completed) != receipt:
        raise UninstallError('A different completion receipt exists; preserve it for recovery')
    save_private(completed, receipt)
    return {'status': 'detached', 'installation_active': False, 'home': str(home),
            'retained_artifacts_remain': True, 'token_revocation_claimed': False}


def reinstall_marker(home):
    """Installer preflight, called under registry.lock; pending removal never bypassed."""
    marker = home / 'runtime/uninstall.json'
    if not _exists(marker):
        completed = home / 'runtime/uninstall-complete.json'
        if not _exists(completed):
            return None
        receipt = read_private(completed)
        if receipt.get('schema') != 1 or receipt.get('home') != str(home):
            raise UninstallError('Completed uninstall receipt identity mismatch')
        return receipt['marker_sha256']
    state = read_private(marker)
    if not state.get('whole_home') and not state.get('project_roots'):
        return None
    completed = home / 'runtime/uninstall-complete.json'
    if not _exists(completed):
        raise UninstallError('Pending uninstall must be resumed before reinstalling this home')
    receipt = read_private(completed)
    if (receipt.get('schema') != 1 or receipt.get('home') != str(home)
            or receipt.get('marker_sha256') != digest(state)):
        raise UninstallError('Uninstall completion does not match the current writer barrier')
    return digest(state)


def finish_reinstallation(home, expected_marker):
    if expected_marker is None:
        return
    marker = home / 'runtime/uninstall.json'
    if _exists(marker) and digest(read_private(marker)) != expected_marker:
        raise UninstallError('Removal marker changed while installing; keep all evidence')
    completed = home / 'runtime/uninstall-complete.json'
    receipt = read_private(completed)
    if receipt.get('marker_sha256') != expected_marker:
        raise UninstallError('Completion receipt changed while installing')
    if read_private(home / MANIFEST).get('active') is not True:
        raise UninstallError('Reinstallation ownership receipt must be durable before clearing its barrier')
    history = completed.with_name('uninstall-completed-history-' + receipt['action'] + '.json')
    if _exists(history) and read_private(history) != receipt:
        raise UninstallError('Uninstall history conflicts; no evidence overwritten')
    save_private(history, receipt)
    marker.unlink(missing_ok=True)
    completed.unlink()
    _sync(marker.parent)


def _environment_caches(home):
    path = home / '.env'
    if not _exists(path):
        return set()
    _private_environment(path)
    if path.stat().st_size > 65536:
        raise UninstallError('Oversized environment; cache sharing cannot be proven')
    result = set()
    for line in path.read_text().splitlines():
        key, separator, value = line.strip().partition('=')
        if key not in ('MAVEN_AWS_LOGIN_CACHE', 'MAVEN_DEPLOY_LOGIN_CACHE'):
            continue
        if not separator:
            raise UninstallError('Malformed credential cache selection')
        value = value.strip()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif any(char.isspace() for char in value) or any(char in value for char in "'\"`$"):
            raise UninstallError('Credential cache selection must be a literal path')
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise UninstallError('Credential cache path must be absolute')
        result.add(str(checked_path(value, exists=False)))
    return result


@contextlib.contextmanager
def _sharing_locks(home, selected_homes):
    """Prevent cooperating installs/session starts from acquiring a shared target."""
    selected = {str(home)} | {str(checked_path(item, exists=False)) for item in selected_homes}
    with contextlib.ExitStack() as stack:
        stack.enter_context(_lock(_registry_path().with_suffix('.lock')))
        for candidate in discover_homes(selected_homes):
            if candidate in selected:
                continue
            other = Path(candidate)
            stack.enter_context(_lock(other / 'runtime/registry.lock'))
            stack.enter_context(_lock(other / 'runtime/setup/lock'))
        yield


def _integration_references(home, selected_homes):
    selected = {str(home), *(str(checked_path(item, exists=False)) for item in selected_homes)}
    references, uncertain = set(), False
    for candidate in discover_homes(selected):
        if candidate in selected:
            continue
        try:
            manifest = read_private(Path(candidate) / MANIFEST)
            if manifest.get('active', True):
                references.update(item['path'] for item in manifest.get('integrations', []))
        except (OSError, ValueError, UninstallError):
            uncertain = True
    return references, uncertain


def _plan_integrations(fragment, home, manifest, selected_homes):
    from tools.harness import removal_content
    shared, uncertain = _integration_references(home, selected_homes)
    for receipt in manifest.get('integrations', []):
        path = receipt['path']
        if not receipt.get('owned', True):
            fragment['retained'].append({'path': path, 'reason': 'Referenced existing integration; not owned by this installation'})
            continue
        try:
            if uncertain or path in shared:
                raise UninstallError('Integration is shared or unselected installation ownership is uncertain')
            after = removal_content(receipt)
            if after is None:
                continue
            expected = inventory(Path(path))
        except (OSError, ValueError, UninstallError) as error:
            fragment['retained'].append({'path': path, 'reason': str(error)})
            continue
        fragment['actions'].append(_action('local_integration', path, 85,
            home=str(home), root=None, receipt=receipt, inventory=expected,
            after_sha256=hashlib.sha256(after).hexdigest(),
            manifest=file_digest(home / MANIFEST),
            selected_homes=[str(checked_path(item, exists=False)) for item in selected_homes]))


def _remove_integration(action, progress, save):
    from tools.harness import removal_content, remove_integration
    details = action['details']
    home, path = Path(details['home']), Path(action['target'])
    with _sharing_locks(home, details['selected_homes']):
        shared, uncertain = _integration_references(home, details['selected_homes'])
        if uncertain or str(path) in shared:
            raise UninstallError('Integration acquired a shared dependency; preserve and re-plan')
        if file_digest(home / MANIFEST) != details['manifest']:
            raise UninstallError('Integration ownership changed; approval invalid')
        current = inventory(path) if _exists(path) else {}
        if current != details['inventory']:
            # Reconciliation verifies the exact reviewed result, including edits
            # performed by another selected home's identical removal action.
            empty_owned_file = (details['receipt']['created']
                                and details['after_sha256'] == hashlib.sha256(b'').hexdigest())
            if ((_exists(path) and file_digest(path) == details['after_sha256'])
                    or (not _exists(path) and empty_owned_file)):
                return {'status': 'removed', 'unrelated_content_preserved': True,
                        'already_at_reviewed_result': True}
            raise UninstallError('Integration changed after review; no unrelated content removed')
        after = removal_content(details['receipt'])
        if after is None or hashlib.sha256(after).hexdigest() != details['after_sha256']:
            raise UninstallError('Selective integration removal differs from reviewed content')
        progress['intent'] = 'remove exact integration'
        save()
        result = remove_integration(details['receipt'])
        return {'status': 'removed', **result}
