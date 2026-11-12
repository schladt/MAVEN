"""`maven-statusline`: one short MAVEN state line for a harness status line.

Claude Code runs this command with its statusLine JSON on stdin. It reads only the
private local state that MAVEN writes for status lines (`tools.home.write_active`):
no network, no cloud client import, no project or Home validation. Any failure prints
a safe default instead of an error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

from tools.home import ACTIVE_DIRECTORY, _active_name, _enabled_root, active_line

UNAVAILABLE = 'MAVEN: status unavailable'
NOT_STARTED = 'MAVEN: not started (ask)'
OFF = 'MAVEN: off'
STATE_LIMIT = 64 * 1024
INPUT_LIMIT = 1024 * 1024
LINE_LIMIT = 120


def _private_json(path: Path) -> dict | None:
    """An owner-private (0600, single regular file, no symlink) JSON object, else None."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > STATE_LIMIT):
            return None
        raw = os.read(descriptor, STATE_LIMIT + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def read_state(home: Path, *, harness_session: str | None, folder: str | None) -> dict | None:
    """The same lookup as `tools.home.read_active` (session first, then folder), stdlib only."""
    directory = Path(home).absolute() / ACTIVE_DIRECTORY
    for name in (_active_name(harness_session, None), _active_name(None, folder)):
        if name:
            value = _private_json(directory / name)
            if value is not None:
                return value
    return None


def _clean(line: str) -> str:
    """One printable line: harness status bars render raw terminal text."""
    text = ''.join(character if character.isprintable() else ' ' for character in line)
    return ' '.join(text.split())[:LINE_LIMIT]


def status_line(home: Path, payload: dict) -> str:
    workspace = payload.get('workspace')
    folder = (workspace.get('current_dir') if isinstance(workspace, dict) else None) or payload.get('cwd')
    folder = folder if isinstance(folder, str) and folder else None
    session = payload.get('session_id')
    session = session if isinstance(session, str) and session else None
    state = read_state(home, harness_session=session, folder=folder)
    if state is not None:
        line = state.get('line')
        return _clean(line if isinstance(line, str) and line.strip() else active_line(state))
    if folder is None:
        return OFF
    cwd = Path(folder).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        return OFF
    # tools.home.start_hint returns None exactly when the folder is inside an enabled
    # project; outside one it always asks. Its full Home validation is too slow here.
    root = _enabled_root(cwd.resolve())
    if root is None:
        return NOT_STARTED
    return _clean(f'MAVEN: project {root.name} (not started)')


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1) if sys.stdin is not None else b''
        payload = json.loads(raw) if raw.strip() and len(raw) <= INPUT_LIMIT else {}
        if not isinstance(payload, dict):
            payload = {}
        home = Path(os.environ.get('MAVEN_HOME') or '~/.maven').expanduser()
        line = status_line(home, payload)
    except Exception:
        line = UNAVAILABLE
    try:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
