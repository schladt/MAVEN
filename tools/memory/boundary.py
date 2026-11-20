"""Private, atomic local artifacts retained for reviewed external operations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class BoundaryError(Exception):
    """Safe, content-free failure at a memory work boundary."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.is_relative_to(root) or '..' in path.parts:
        raise BoundaryError('private path escapes workspace')
    cursor = root
    for part in path.relative_to(root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise BoundaryError('private paths must not be symlinks')
    return path


def save_private(root, relative, value):
    path = private_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix='.memory-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_private(root, relative):
    path = private_path(root, relative)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BoundaryError('runtime pointer must be owner-private')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise BoundaryError('required private runtime pointer is unavailable') from None


