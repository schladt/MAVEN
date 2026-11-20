"""Owner-private file locks and reviewed project/removal barriers."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import time

from tools.memory.boundary import BoundaryError, private_path, read_private


class CoordinationError(BoundaryError):
    pass


LOCK_TIMEOUT = 10.0


@contextlib.contextmanager
def private_lock(root, relative, *, blocking=True, shared=False, timeout=None):
    """Owner-private flock. A timeout bounds contention, not the lifetime of an owner."""
    path = private_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CoordinationError('coordination lock must be owner-private')
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if timeout is None:
            try:
                fcntl.flock(descriptor, mode | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise CoordinationError('another operation holds this lock') from None
        else:
            deadline = time.monotonic() + timeout
            delay = 0.005
            while True:
                try:
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CoordinationError('resource is busy; retry shortly') from None
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.1)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def project_binding_lock(home, root, *, shared=True):
    """Serialize rebinding against project access and all access against removal."""
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
    with private_lock(home, 'runtime/registry.lock', shared=True, timeout=LOCK_TIMEOUT), \
            private_lock(home, f'runtime/project-bindings/{key}.lock', shared=shared, timeout=LOCK_TIMEOUT):
        yield


def ensure_available(home, root, *, switch_operation=None):
    """Reject access across an interrupted switch or reviewed removal barrier."""
    home = Path(home)
    from tools.project_switch import barrier_path
    barrier = barrier_path(home, root)
    barrier = private_path(home, barrier.relative_to(home))
    if barrier.exists() and (switch_operation is None or
            read_private(home, barrier.relative_to(home)) != {
                'schema': 1, 'root': str(Path(root).resolve()), 'operation_id': switch_operation}):
        raise CoordinationError('project switch is unfinished; resume its exact reviewed administration operation before starting work')
    path = private_path(home, 'runtime/uninstall.json')
    if not path.exists():
        return
    marker = read_private(home, 'runtime/uninstall.json')
    roots = marker.get('project_roots')
    actions = marker.get('actions', {})
    if (set(marker) - {'schema', 'whole_home', 'project_roots', 'actions'}
            or type(marker.get('schema')) is not int or marker['schema'] != 1
            or type(marker.get('whole_home')) is not bool or not isinstance(roots, list)
            or any(not isinstance(value, str) or not Path(value).is_absolute()
                   or str(Path(value).resolve()) != value for value in roots)):
        raise CoordinationError('uninstall marker is malformed; preserve it for recovery')
    if (not isinstance(actions, dict) or any(
            not isinstance(key, str) or len(key) != 64
            or any(char not in '0123456789abcdef' for char in key)
            or not isinstance(value, dict) or set(value) != {'root'}
            or (value['root'] is not None and
                (not isinstance(value['root'], str) or value['root'] not in roots))
            or (value['root'] is None and not marker['whole_home'])
            for key, value in actions.items())):
        raise CoordinationError('uninstall action identity is malformed; preserve it for recovery')
    if marker['whole_home'] or str(Path(root).resolve()) in roots:
        raise CoordinationError('uninstall barrier prohibits new work; status and recovery remain available')


