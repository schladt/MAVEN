"""MAVEN Home selection, the session start choice and its private local log.

`~/.maven/home.json` names one enabled project as the owner's Home. Outside an enabled
project every session start asks: Start Home, Create Project or Read Only. Answers are
appended to a private local log; nothing here contacts the cloud.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import uuid

from tools.setup_common import SetupPending, atomic_private, private_directory, read_private, save_json

HOME_FILE = 'home.json'
CHOICE_LOG = 'runtime/start-choices.jsonl'
ACTIVE_DIRECTORY = 'runtime/active'
CHOICE_LOG_LIMIT = 1024 * 1024
HOME_CHOICES = ('home', 'project', 'read_only')
RECORDED_CHOICES = ('home', 'project', 'read_only', 'off')
QUESTION = 'MAVEN: no project here — Start Home, Create Project, or Read Only?'
QUESTION_WITHOUT_HOME = 'MAVEN: no project here — Create Project, or Read Only?'


class HomeError(ValueError):
    """An invalid Home selection; the message names the problem without secrets."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkout(home: Path) -> Path | None:
    from tools.uninstall.local import MANIFEST
    manifest = home / MANIFEST
    if not manifest.exists():
        return None
    try:
        source = read_private(manifest).get('source')
    except Exception:
        return None
    return Path(source).resolve() if source else None


def _inside(path: Path, parent: Path | None) -> bool:
    return parent is not None and (path == parent or parent in path.parents)


def validate_root(home: Path, root: Path) -> Path:
    """A Home root is an absolute folder outside the MAVEN home and checkout."""
    home = Path(home).absolute()
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise HomeError('Home folder must be an absolute path')
    root = root.resolve()
    if not root.is_dir():
        raise HomeError('Home folder does not exist')
    if root in (Path('/'), Path.home().resolve()):
        raise HomeError('Home folder cannot be / or the user home directory itself')
    if _inside(root, home.resolve()) or _inside(root, _checkout(home)):
        raise HomeError('Home folder cannot be inside the MAVEN installation or its source checkout')
    return root


def load_home(home: Path) -> dict | None:
    """The validated Home selection, None when not configured, or {'invalid': reason}."""
    from tools.memory.client import MemoryClientError
    from tools.memory.environment import load_environment
    from tools.project import ProjectError, load_project
    home = Path(home).absolute()
    path = home / HOME_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink():
            raise HomeError('home.json must not be a symlink')
        value = read_private(path)
        if value.get('schema') != 1 or not isinstance(value.get('label'), str) or not value['label'].strip():
            raise HomeError('home.json has an unsupported schema')
        root = validate_root(home, Path(value['root']))
        if str(root) != value['root']:
            raise HomeError('home.json root must be its resolved absolute path')
        project = load_project(root)
        if project['project_id'] != value.get('project_id'):
            raise HomeError('Home folder belongs to a different MAVEN project')
        values = load_environment(home / '.env') if (home / '.env').exists() else {}
        if values.get('MAVEN_MEMORY_INSTANCE_ID') != value.get('instance_id'):
            raise HomeError('Home belongs to a different MAVEN instance')
        consent = value.get('consent')
        if (not isinstance(consent, dict) or not str(consent.get('origin', '')).strip()
                or not isinstance(consent.get('excerpt'), str)):
            raise HomeError('home.json lacks the recorded owner consent')
    except (HomeError, ProjectError, SetupPending) as error:
        return {'invalid': str(error)}
    except (OSError, ValueError, KeyError, TypeError, MemoryClientError):
        return {'invalid': 'home.json is unreadable or malformed'}
    return value


def save_home(home: Path, *, root: Path, project_id: str, instance_id: str, label: str,
              consent: dict) -> dict:
    home = Path(home).absolute()
    root = validate_root(home, root)
    record = {'schema': 1, 'root': str(root), 'project_id': str(uuid.UUID(project_id)),
              'instance_id': instance_id, 'label': label.strip(), 'enabled_at': _now(),
              'consent': {'origin': consent['origin'], 'excerpt': consent['excerpt']}}
    if not record['label']:
        raise HomeError('Home label is required')
    private_directory(home)
    save_json(home / HOME_FILE, record)
    loaded = load_home(home)
    if not loaded or 'invalid' in loaded:
        (home / HOME_FILE).unlink(missing_ok=True)
        raise HomeError((loaded or {}).get('invalid', 'Home selection could not be verified'))
    return loaded


def remove_home(home: Path) -> bool:
    path = Path(home).absolute() / HOME_FILE
    if not path.exists() and not path.is_symlink():
        return False
    path.unlink()
    return True


def _enabled_root(cwd: Path) -> Path | None:
    return next((candidate for candidate in (cwd, *cwd.parents)
                 if (candidate / '.maven/project.json').is_file()), None)


def _log_path(home: Path) -> Path:
    return Path(home).absolute() / CHOICE_LOG


def record_choice(home: Path, cwd: Path, choice: str, *, harness: str | None = None,
                  harness_session: str | None = None) -> dict:
    """Append one start choice to the private local log; never a cloud write."""
    if choice not in RECORDED_CHOICES:
        raise HomeError('choice must be one of ' + ', '.join(RECORDED_CHOICES))
    entry = {'at': _now(), 'folder': str(Path(cwd).expanduser().resolve()), 'choice': choice,
             'harness': harness, 'harness_session': harness_session}
    path = _log_path(home)
    private_directory(path.parent)
    if path.exists() and path.stat().st_size > CHOICE_LOG_LIMIT:
        previous = path.with_name(path.name + '.1')
        previous.unlink(missing_ok=True)
        os.replace(path, previous)
    if path.is_symlink():
        raise HomeError('start-choice log must not be a symlink')
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise HomeError('start-choice log must be owner-private (0600)')
        os.write(descriptor, (json.dumps(entry, sort_keys=True) + '\n').encode())
    finally:
        os.close(descriptor)
    return entry


def last_choice(home: Path, cwd: Path) -> str | None:
    folder = str(Path(cwd).expanduser().resolve())
    found = None
    for path in (_log_path(home).with_name(Path(CHOICE_LOG).name + '.1'), _log_path(home)):
        if not path.is_file() or path.is_symlink():
            continue
        for line in path.read_text(errors='replace').splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get('folder') == folder:
                found = entry.get('choice')
    return found


def start_hint(home: Path, cwd: Path) -> dict | None:
    """None inside an enabled project; otherwise the start question for this folder."""
    cwd = Path(cwd).expanduser().resolve()
    if _enabled_root(cwd) is not None:
        return None
    selected = load_home(home)
    configured = bool(selected) and 'invalid' not in selected
    previous = last_choice(home, cwd)
    question = QUESTION if configured else QUESTION_WITHOUT_HOME
    if previous:
        question += f' (last time here: {previous.replace("_", " ")})'
    hint = {'mode': 'choose', 'folder': str(cwd),
            'options': list(HOME_CHOICES) if configured else ['project', 'read_only'],
            'last_choice': previous, 'question': question, 'default': 'read_only',
            'next_action': ('Ask the owner this one-line question every time. Start Home: maven_initialize '
                            'with home=true. Create Project: maven_project for this folder. Read Only or '
                            'no answer: maven_start with read_only; consult only on request. Print the '
                            'returned active line.')}
    if configured:
        hint.update(label=selected['label'], project_id=selected['project_id'])
    elif selected:
        hint['home_invalid'] = selected['invalid']
    return hint


def active_line(active: dict) -> str:
    mode = active.get('mode')
    if mode == 'home':
        return f"MAVEN: Home ({active.get('label')})"
    if mode == 'project':
        return f"MAVEN: project {active.get('label')}"
    if mode == 'read_only':
        return 'MAVEN: read-only'
    return 'MAVEN: off'


def active(mode: str, *, label: str | None = None, project_id: str | None = None) -> dict:
    value = {'mode': mode, 'label': label, 'project_id': project_id}
    value['line'] = active_line(value)
    return value


def _active_name(harness_session: str | None, folder: str | Path | None) -> str | None:
    if harness_session:
        return 'session-' + uuid.uuid5(uuid.NAMESPACE_URL, 'maven-harness-session:' + harness_session).hex + '.json'
    if folder:
        resolved = str(Path(folder).expanduser().resolve())
        return 'folder-' + uuid.uuid5(uuid.NAMESPACE_URL, 'maven-folder:' + resolved).hex + '.json'
    return None


def write_active(home: Path, value: dict, *, harness_session: str | None = None,
                 folder: str | Path | None = None) -> None:
    """Private state for status lines, keyed by harness session and by workspace folder."""
    names = {name for name in (_active_name(harness_session, None), _active_name(None, folder)) if name}
    if not names:
        return
    directory = Path(home).absolute() / ACTIVE_DIRECTORY
    private_directory(directory)
    payload = json.dumps({**value, 'harness_session': harness_session,
                          'folder': str(Path(folder).expanduser().resolve()) if folder else None,
                          'at': _now()}, sort_keys=True) + '\n'
    for name in names:
        atomic_private(directory / name, payload)


def read_active(home: Path, *, harness_session: str | None = None,
                folder: str | Path | None = None) -> dict | None:
    """Session state first, then the latest state recorded for the folder."""
    for name in (_active_name(harness_session, None), _active_name(None, folder)):
        path = Path(home).absolute() / ACTIVE_DIRECTORY / name if name else None
        if path is None or not path.is_file() or path.is_symlink():
            continue
        try:
            return read_private(path)
        except Exception:
            continue
    return None
