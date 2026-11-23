"""Claude statusLine: show only locally configured project scope, never cloud freshness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from tools.reconcile import select_project

UNAVAILABLE = 'MAVEN: status unavailable; ask MAVEN to inspect the local project selection without changing it'
OFF = 'MAVEN: off'
INPUT_LIMIT = 1024 * 1024
LINE_LIMIT = 120


def _clean(line: str) -> str:
    """One printable line: harness status bars render raw terminal text."""
    text = ''.join(character if character.isprintable() else ' ' for character in line)
    return ' '.join(text.split())[:LINE_LIMIT]


def status_line(home: Path, payload: dict) -> str:
    workspace = payload.get('workspace')
    folder = (workspace.get('current_dir') if isinstance(workspace, dict) else None) or payload.get('cwd')
    if not isinstance(folder, str) or not Path(folder).is_absolute() or not Path(folder).is_dir():
        return OFF
    selected = select_project(folder, home)
    if selected is None:
        return OFF
    return _clean(f'MAVEN: {selected["mode"]} {selected["label"]} (local)')


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
