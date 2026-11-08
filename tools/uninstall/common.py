"""Dependency-free private storage and identity helpers for uninstall/recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class UninstallError(ValueError):
    """An uninstall action cannot safely proceed; preserve its receipt."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def checked_path(path, *, exists=True):
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Inspect the lexical path before normalization: '..' must not conceal a link.
    for part in reversed((candidate, *candidate.parents)):
        if part.is_symlink():
            raise UninstallError('symlink path is not an uninstall target: ' + str(part))
    candidate = Path(os.path.abspath(candidate))
    if candidate == Path(candidate.anchor):
        raise UninstallError('filesystem root is not an uninstall target')
    if exists and not candidate.exists():
        raise UninstallError('required path is unavailable: ' + str(candidate))
    return candidate


def private_directory(path):
    path = checked_path(path, exists=False)
    if not path.exists():
        parent = path.parent
        if not parent.exists():
            private_directory(parent)
        path.mkdir(mode=0o700)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise UninstallError('owner-private directory required: ' + str(path))
    return path


def _private_file(info, path):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise UninstallError('owner-private, single-link regular file required: ' + str(path))


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UninstallError('duplicate key in private JSON')
        value[key] = item
    return value


def read_private(path):
    path = checked_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        _private_file(os.fstat(stream.fileno()), path)
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise UninstallError('private JSON exceeds size limit')
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda value: _invalid_json())
    except (ValueError, UnicodeError, RecursionError):
        raise UninstallError('private JSON is malformed: ' + str(path)) from None


def _invalid_json():
    raise UninstallError('non-finite JSON value')


def save_private(path, value):
    payload = canonical(value) + b'\n'
    if len(payload) > 32 * 1024 * 1024:
        raise UninstallError('private JSON exceeds size limit; no truncated receipt was written')
    path = checked_path(path, exists=False)
    parent = private_directory(path.parent)
    if path.exists():
        _private_file(path.lstat(), path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck immediately before replacement; never follow a changed target.
        checked_path(path, exists=False)
        if path.exists():
            _private_file(path.lstat(), path)
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_digest(path):
    path = checked_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    value = hashlib.sha256()
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise UninstallError('regular file required: ' + str(path))
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                                 info.st_ctime_ns, info.st_mode)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise UninstallError('file changed while inspecting: ' + str(path))
    return value.hexdigest()
