"""Shared private setup artifacts, prompts and authority verification."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from tools.interaction import Field, checkpoint, require


class SetupPending(Exception):
    """A safe stop, not a successful setup."""


def ask(prompt, default=None):
    values = require().form(prompt, [Field('value', prompt, default or '')])
    if values is None:
        raise SetupPending('Selection paused; existing artifacts are preserved.')
    return values['value']


def confirm(prompt):
    return require().confirm(prompt)


def approve(prompt, token, *, body='', destructive=False):
    checkpoint()
    if not require().review(prompt, body or str(token), identity=str(token),
                            destructive=destructive):
        raise SetupPending('Approval declined; no subsequent action was performed.')


def private_directory(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        private_directory(path.parent) if not path.parent.exists() else None
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise SetupPending(f'Use an owner-owned, non-symlink directory with mode 0700: {path}')
    # A private leaf underneath a symlink is not an independent installation.
    if path.absolute() != path.resolve():
        raise SetupPending(f'Select a path without symlink components: {path}')


def read_private(path):
    from tools.memory.client import MemoryClientError, read_json
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise MemoryClientError('setup_artifact_must_be_owner_private')
    finally:
        os.close(fd)
    return read_json(path, maximum=4 * 1024 * 1024)


def atomic_private(path, text):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise SetupPending(f'Existing file must be owner-private (0600), not a symlink: {path}')
    fd, temporary = tempfile.mkstemp(prefix='.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, document):
    atomic_private(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


@contextmanager
def setup_lock(directory):
    path = directory / 'lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise SetupPending('The setup lock must be an owner-private regular file.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupPending('Another setup is running for this home. Close it before resuming.') from None
        yield
    finally:
        os.close(fd)


def verify_authority(env, project_id=None, *, values=None):
    from tools.memory.environment import configured_client
    client = configured_client(env, values=values)
    try:
        scope = {'workflow': 'start-work', 'operation': 'start-work'}
        if project_id:
            scope['project_id'] = project_id
        context = client.load_context(scope)
        if not any(page.get('page_kind') == 'directive' for page in context['data']['instructions']):
            raise SetupPending('Routine cloud access succeeded, but applicable approved MAVEN authority is missing. Ask the instance owner to approve complete authority, then resume.')
        if project_id:
            record = client.invoke('/v1/memory/read', {'record_id': project_id})['data']
            if (record.get('kind') != 'entity' or record.get('data_scope') != 'owner'
                    or record.get('data', {}).get('entity_type') != 'project'):
                raise SetupPending('The selected cloud record is not an owner project entity.')
        return context['data']['authority_generation']
    finally:
        client.close()
